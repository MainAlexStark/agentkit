"""Задачи по расписанию: агент запускается сам по cron и пишет итог в сессию.

Расписание хранится в базе, а не в процессе: перезапуск сервиса его не
теряет, а пропущенный за время простоя запуск выполняется один раз при
старте (не N раз подряд).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

from agentkit.agent import Agent, RunOutcome
from agentkit.store.base import ScheduleRec, Store
from agentkit.tools.base import User

log = logging.getLogger("agentkit.scheduler")


def next_run(cron: str, tz: str, after: float | None = None) -> float:
    base = datetime.fromtimestamp(after or time.time(), ZoneInfo(tz))
    return croniter(cron, base).get_next(datetime).timestamp()


def validate_cron(cron: str) -> bool:
    return croniter.is_valid(cron)


class Scheduler:
    def __init__(
        self,
        store: Store,
        agent_for: Callable[[str], Agent],  # имя агента → Agent
        user_for: Callable[[str], Awaitable[User] | User],
        *,
        tz: str = "Europe/Moscow",
        interval: float = 30.0,
        on_done: Callable[[ScheduleRec, RunOutcome], Awaitable[None]] | None = None,
        # Как запускать: по умолчанию agent.run_to_end; сервис подставляет запуск через RunManager.
        runner: Callable[[ScheduleRec, str, User], Awaitable[RunOutcome]] | None = None,
    ):
        self.runner = runner
        self.store = store
        self.agent_for = agent_for
        self.user_for = user_for
        self.tz = tz
        self.interval = interval
        self.on_done = on_done
        self._task: asyncio.Task | None = None
        self._running: set[str] = set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.interval)

    async def tick(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        fired = []
        for s in await self.store.schedule_list():
            if not s.enabled or s.id in self._running:
                continue
            if s.next_run_at is None:
                s.next_run_at = next_run(s.cron, self.tz, now)
                await self.store.schedule_upsert(s)
                continue
            if s.next_run_at <= now:
                new_next = next_run(s.cron, self.tz, now)  # от «сейчас»: пропуски не догоняем
                # Процессов сервиса может быть несколько, и каждый тикает сам:
                # запуск достаётся тому, кто первым сдвинул next_run_at.
                if not await self.store.claim_schedule(s.id, s.next_run_at, new_next, now):
                    continue
                s.last_run_at, s.next_run_at = now, new_next
                fired.append(s.id)
                self._running.add(s.id)
                asyncio.create_task(self._fire(s))
        return fired

    async def _fire(self, s: ScheduleRec) -> None:
        try:
            user = self.user_for(s.user_id)
            if asyncio.iscoroutine(user):
                user = await user
            agent = self.agent_for(s.agent)
            sid = s.session_id
            if not sid or await self.store.get_session(sid) is None:
                sess = await self.store.create_session(s.user_id, title=f"⏰ {s.name}", agent=s.agent)
                sid = sess.id
            if self.runner is not None:
                outcome = await self.runner(s, sid, user)
            else:
                outcome = await agent.run_to_end(sid, user, s.prompt, trigger="schedule")
            if self.on_done:
                await self.on_done(s, outcome)
        except Exception:  # noqa: BLE001
            log.exception("schedule %s failed", s.id)
        finally:
            self._running.discard(s.id)
