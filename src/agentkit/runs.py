"""Менеджер прогонов: фоновые задачи, раздача событий подписчикам, один прогон на сессию.

Прогон живёт отдельно от HTTP-запроса: запрос только запускает его и сразу
возвращает run_id. Смотреть за ним можно сколько угодно раз и с любого
устройства (SSE или опрос) — закрытая вкладка не обрывает работу агента.
Пропущенное восстанавливается из базы по курсору seq; недописанный текст
текущего шага отдаётся снимком (text_snapshot).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from agentkit.events import Event, EventType
from agentkit.store.base import Store

log = logging.getLogger("agentkit.runs")


@dataclass
class _Active:
    run_id: str | None = None
    session_id: str = ""
    task: asyncio.Task | None = None
    partial: list[str] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: Callable[[str], bool] | None = None


class Busy(Exception):
    """В сессии уже идёт прогон."""


class RunManager:
    def __init__(self, store: Store):
        self.store = store
        self.by_session: dict[str, _Active] = {}

    def active_run(self, session_id: str) -> str | None:
        a = self.by_session.get(session_id)
        return a.run_id if a else None

    async def start(self, session_id: str, gen_factory: Callable[[], AsyncIterator[Event]],
                    cancel: Callable[[str], bool] | None = None) -> str:
        """Запускает прогон в фоне и возвращает его id, как только тот известен."""
        if session_id in self.by_session:
            raise Busy(session_id)
        a = _Active(session_id=session_id, cancel=cancel)
        self.by_session[session_id] = a

        async def consume() -> None:
            try:
                async for ev in gen_factory():
                    if a.run_id is None and ev.run_id:
                        a.run_id = ev.run_id
                        a.ready.set()
                    if ev.type == EventType.TEXT_DELTA and not ev.agent:
                        a.partial.append(ev.data.get("text", ""))
                    elif ev.type == EventType.MESSAGE and not ev.agent:
                        a.partial.clear()
                    for q in list(a.subscribers):
                        q.put_nowait(ev)
            except Exception:  # noqa: BLE001
                log.exception("run in session %s crashed", session_id)
            finally:
                self.by_session.pop(session_id, None)
                a.ready.set()
                for q in list(a.subscribers):
                    q.put_nowait(None)

        a.task = asyncio.create_task(consume())
        await a.ready.wait()
        if a.run_id is None:
            raise RuntimeError("прогон не стартовал")
        return a.run_id

    def cancel(self, session_id: str) -> bool:
        a = self.by_session.get(session_id)
        if a and a.run_id and a.cancel:
            return a.cancel(a.run_id)
        return False

    async def subscribe(self, session_id: str, after_seq: int = 0, *, follow: bool = True) -> AsyncIterator[Event]:
        """Сначала всё сохранённое после курсора, затем живые события текущего прогона."""
        a = self.by_session.get(session_id)
        q: asyncio.Queue = asyncio.Queue()
        if a and follow:
            a.subscribers.add(q)  # подписываемся ДО чтения базы, дубли отсечём по seq
        try:
            last = after_seq
            for ev in await self.store.list_events(session_id=session_id, after_seq=after_seq):
                last = max(last, ev.seq)
                yield ev
            if not (a and follow):
                return
            if a.partial:
                yield Event(EventType.TEXT_DELTA, {"text": "".join(a.partial), "snapshot": True},
                            run_id=a.run_id, session_id=session_id)
            while True:
                ev = await q.get()
                if ev is None:
                    return
                if ev.seq and ev.seq <= last:
                    continue
                last = max(last, ev.seq)
                yield ev
        finally:
            if a:
                a.subscribers.discard(q)

    async def wait(self, session_id: str) -> None:
        a = self.by_session.get(session_id)
        if a and a.task:
            with contextlib.suppress(Exception):
                await a.task

    async def shutdown(self) -> None:
        for a in list(self.by_session.values()):
            if a.run_id and a.cancel:
                a.cancel(a.run_id)
        await asyncio.gather(*(a.task for a in self.by_session.values() if a.task), return_exceptions=True)
