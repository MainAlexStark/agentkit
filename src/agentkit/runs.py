"""Менеджер прогонов: фоновые задачи, раздача событий, один прогон на сессию — в любом числе процессов.

Прогон живёт отдельно от HTTP-запроса: запрос только запускает его и сразу
возвращает run_id. Смотреть за ним можно сколько угодно раз, с любого
устройства и через любой процесс сервиса (SSE или опрос) — закрытая вкладка
не обрывает работу агента.

Координация между процессами — через Bus (bus.py): блокировка сессии с TTL
и продлением, публикация событий, отмена прогона из другого процесса.
Пропущенное восстанавливается из базы по курсору seq, недописанный текст
текущего шага — снимком.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agentkit.bus import Bus, LocalBus
from agentkit.events import Event, EventType
from agentkit.store.base import Store

log = logging.getLogger("agentkit.runs")

STARTING = "starting"


@dataclass
class _Local:
    session_id: str
    run_id: str | None = None
    task: asyncio.Task | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: Callable[[str], bool] | None = None
    cancel_requested: bool = False


class Busy(Exception):
    """В сессии уже идёт прогон."""


class RunManager:
    def __init__(self, store: Store, bus: Bus | None = None, *, lock_ttl: float = 30.0):
        self.store = store
        self.bus = bus or LocalBus()
        self.lock_ttl = lock_ttl
        self.local: dict[str, _Local] = {}
        # Вызывается, когда прогон в сессии закончился (сервис проверяет очередь входящих).
        self.on_idle: Callable[[str], Awaitable[None]] | None = None
        self.bus.on_broadcast(self._on_broadcast)

    async def _on_broadcast(self, msg: dict[str, Any]) -> None:
        if msg.get("cmd") == "cancel":
            self._cancel_local(msg.get("session_id", ""))

    async def active_run(self, session_id: str) -> str | None:
        return await self.bus.active(session_id)

    async def start(self, session_id: str, gen_factory: Callable[[], AsyncIterator[Event]],
                    cancel: Callable[[str], bool] | None = None) -> str:
        """Запускает прогон в фоне и возвращает его id, как только тот известен."""
        if not await self.bus.try_lock(session_id, STARTING, self.lock_ttl):
            raise Busy(session_id)
        loc = _Local(session_id=session_id, cancel=cancel)
        self.local[session_id] = loc

        async def heartbeat() -> None:
            # Продлеваем блокировку, пока процесс жив; умер — ключ истечёт сам.
            while True:
                await asyncio.sleep(self.lock_ttl / 3)
                await self.bus.set_lock(session_id, loc.run_id or STARTING, self.lock_ttl)

        async def consume() -> None:
            hb = asyncio.create_task(heartbeat())
            try:
                async for ev in gen_factory():
                    if loc.run_id is None and ev.run_id:
                        loc.run_id = ev.run_id
                        await self.bus.set_lock(session_id, ev.run_id, self.lock_ttl)
                        loc.ready.set()
                        if loc.cancel_requested and loc.cancel:
                            loc.cancel(ev.run_id)
                    if ev.type == EventType.TEXT_DELTA and not ev.agent:
                        await self.bus.append_partial(session_id, ev.data.get("text", ""))
                    elif ev.type == EventType.MESSAGE and not ev.agent:
                        await self.bus.clear_partial(session_id)
                    await self.bus.publish(session_id, ev.to_dict())
            except Exception:  # noqa: BLE001
                log.exception("run in session %s crashed", session_id)
            finally:
                hb.cancel()
                self.local.pop(session_id, None)
                await self.bus.clear_partial(session_id)
                await self.bus.unlock(session_id)
                loc.ready.set()
                if self.on_idle:
                    try:
                        await self.on_idle(session_id)
                    except Exception:  # noqa: BLE001
                        log.exception("on_idle failed for %s", session_id)

        loc.task = asyncio.create_task(consume())
        await loc.ready.wait()
        if loc.run_id is None:
            raise RuntimeError("прогон не стартовал")
        return loc.run_id

    def _cancel_local(self, session_id: str) -> bool:
        loc = self.local.get(session_id)
        if loc is None:
            return False
        loc.cancel_requested = True
        if loc.run_id and loc.cancel:
            return loc.cancel(loc.run_id)
        return True

    async def cancel(self, session_id: str) -> bool:
        if self._cancel_local(session_id):
            return True
        if await self.bus.active(session_id):
            # Прогон идёт в другом процессе — просим его остановиться.
            await self.bus.broadcast({"cmd": "cancel", "session_id": session_id})
            return True
        return False

    async def subscribe(self, session_id: str, after_seq: int = 0, *, follow: bool = True,
                        grace: float = 1.5) -> AsyncIterator[dict[str, Any]]:
        """Сначала всё сохранённое после курсора, затем живые события; конец — когда сессия затихла.

        После run_finished ждём grace секунд: если в сессии ждали входящие
        сообщения, следующий прогон стартует сразу, и поток не обрывается.
        """
        sub = await self.bus.open(session_id) if follow else None
        try:
            last = after_seq
            for ev in await self.store.list_events(session_id=session_id, after_seq=after_seq):
                last = max(last, ev.seq)
                yield ev.to_dict()
            if sub is None:
                return
            if not await self.bus.active(session_id):
                return
            if text := await self.bus.partial(session_id):
                yield Event(EventType.TEXT_DELTA, {"text": text, "snapshot": True}, session_id=session_id).to_dict()
            idle_since: float | None = None
            loop = asyncio.get_running_loop()
            while True:
                ev = await sub.get(timeout=0.5)
                if ev is None:
                    if await self.bus.active(session_id):
                        idle_since = None
                        continue
                    idle_since = idle_since or loop.time()
                    if loop.time() - idle_since >= grace:
                        # Сдаём хвост из базы: публикация могла разминуться с подпиской.
                        for e in await self.store.list_events(session_id=session_id, after_seq=last):
                            last = max(last, e.seq)
                            yield e.to_dict()
                        return
                    continue
                seq = ev.get("seq") or 0
                if seq and seq <= last:
                    continue
                last = max(last, seq)
                yield ev
        finally:
            if sub is not None:
                await sub.close()

    async def wait(self, session_id: str, timeout: float | None = None) -> None:
        """Дождаться, пока сессия освободится (в т. ч. прогон в другом процессе)."""
        loc = self.local.get(session_id)
        if loc and loc.task:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(loc.task), timeout)
            return
        loop = asyncio.get_running_loop()
        end = loop.time() + (timeout or 3600)
        while await self.bus.active(session_id) and loop.time() < end:
            await asyncio.sleep(0.2)

    async def shutdown(self) -> None:
        for loc in list(self.local.values()):
            if loc.run_id and loc.cancel:
                loc.cancel(loc.run_id)
        await asyncio.gather(*(loc.task for loc in self.local.values() if loc.task), return_exceptions=True)
