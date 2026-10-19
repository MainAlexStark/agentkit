"""Шина между процессами: блокировка прогона, раздача событий, отмена, общие команды.

Один процесс — LocalBus (всё в памяти). Несколько процессов uvicorn или
несколько контейнеров — RedisBus. Остальной код видит только интерфейс Bus
и не знает, сколько процессов работает.

Что делает шина и почему этого нельзя достичь одной базой:
  * блокировка «в сессии идёт прогон» с TTL — умерший процесс не держит
    сессию вечно (база такого не умеет без опроса);
  * живые события, включая куски текста (их в базе нет — слишком часто);
  * отмена прогона, который идёт в другом процессе;
  * общие команды («перечитай MCP» после подключения OAuth в одном процессе);
  * короткие блокировки (обновление OAuth-токена: ротация refresh не
    переживает двух одновременных обновлений).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("agentkit.bus")

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class Subscription:
    async def get(self, timeout: float) -> dict[str, Any] | None:
        """Следующее событие или None, если за timeout ничего не пришло."""
        ...

    async def close(self) -> None: ...


class _QueueSub(Subscription):
    def __init__(self, q: asyncio.Queue, on_close: Callable[[], None]):
        self.q, self._on_close = q, on_close

    async def get(self, timeout):
        try:
            return await asyncio.wait_for(self.q.get(), timeout)
        except TimeoutError:
            return None

    async def close(self):
        self._on_close()


class _RedisSub(Subscription):
    def __init__(self, ps):
        self.ps = ps

    async def get(self, timeout):
        # get_message возвращает None и на служебных сообщениях (подтверждение подписки),
        # не дожидаясь timeout, — поэтому ждём до дедлайна сами.
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while (left := end - loop.time()) > 0:
            msg = await self.ps.get_message(ignore_subscribe_messages=True, timeout=left)
            if msg and msg.get("type") == "message":
                return json.loads(msg["data"])
        return None

    async def close(self):
        with contextlib.suppress(Exception):
            await self.ps.unsubscribe()
            await self.ps.aclose()


class Bus:
    """Интерфейс. Значение блокировки прогона — run_id (или «starting» до его появления)."""

    async def try_lock(self, session_id: str, value: str, ttl: float) -> bool: ...
    async def set_lock(self, session_id: str, value: str, ttl: float) -> None: ...
    async def unlock(self, session_id: str) -> None: ...
    async def active(self, session_id: str) -> str | None: ...

    async def publish(self, session_id: str, event: dict[str, Any]) -> None: ...
    async def open(self, session_id: str) -> Subscription:
        """Подписка уже действует, когда open вернул управление, — события после этого не теряются."""
        ...

    async def append_partial(self, session_id: str, text: str) -> None: ...
    async def clear_partial(self, session_id: str) -> None: ...
    async def partial(self, session_id: str) -> str: ...

    async def broadcast(self, message: dict[str, Any]) -> None: ...
    def on_broadcast(self, handler: Handler) -> None: ...

    def lock(self, key: str, ttl: float = 30.0) -> contextlib.AbstractAsyncContextManager: ...

    async def start(self) -> None: ...
    async def close(self) -> None: ...


class LocalBus(Bus):
    def __init__(self) -> None:
        self._locks: dict[str, str] = {}
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._partial: dict[str, list[str]] = defaultdict(list)
        self._handlers: list[Handler] = []
        self._named: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def try_lock(self, session_id, value, ttl):
        if session_id in self._locks:
            return False
        self._locks[session_id] = value
        return True

    async def set_lock(self, session_id, value, ttl):
        self._locks[session_id] = value

    async def unlock(self, session_id):
        self._locks.pop(session_id, None)

    async def active(self, session_id):
        return self._locks.get(session_id)

    async def publish(self, session_id, event):
        for q in list(self._subs[session_id]):
            q.put_nowait(event)

    async def open(self, session_id):
        q: asyncio.Queue = asyncio.Queue()
        self._subs[session_id].add(q)
        return _QueueSub(q, lambda: self._subs[session_id].discard(q))

    async def append_partial(self, session_id, text):
        self._partial[session_id].append(text)

    async def clear_partial(self, session_id):
        self._partial.pop(session_id, None)

    async def partial(self, session_id):
        return "".join(self._partial.get(session_id, []))

    async def broadcast(self, message):
        for h in list(self._handlers):
            await h(message)

    def on_broadcast(self, handler):
        self._handlers.append(handler)

    def lock(self, key, ttl=30.0):
        return self._named[key]

    async def start(self):
        return None

    async def close(self):
        return None


_UNLOCK = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class _RedisLock:
    def __init__(self, r, key: str, ttl: float):
        self.r, self.key, self.ttl = r, key, ttl
        self.token = uuid.uuid4().hex

    async def __aenter__(self):
        # Ждём освобождения: держатель либо закончит, либо его ключ истечёт по TTL.
        while not await self.r.set(self.key, self.token, nx=True, px=int(self.ttl * 1000)):
            await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc):
        await self.r.eval(_UNLOCK, 1, self.key, self.token)


class RedisBus(Bus):
    """Redis: ключи ak:run:<сессия>, каналы ak:ev:<сессия> и ak:broadcast, ключи ak:partial:<сессия>."""

    def __init__(self, url: str, prefix: str = "ak"):
        import redis.asyncio as aioredis  # noqa: PLC0415 — зависимость нужна только с Redis

        self.r = aioredis.from_url(url, decode_responses=True)
        self.p = prefix
        self._handlers: list[Handler] = []
        self._task: asyncio.Task | None = None
        # Отличаем свои рассылки от чужих: команда, посланная этим процессом,
        # выполняется им сразу, а не по возвращению из Redis.
        self.node = uuid.uuid4().hex

    def _k(self, *parts: str) -> str:
        return ":".join((self.p, *parts))

    async def try_lock(self, session_id, value, ttl):
        return bool(await self.r.set(self._k("run", session_id), value, nx=True, px=int(ttl * 1000)))

    async def set_lock(self, session_id, value, ttl):
        await self.r.set(self._k("run", session_id), value, px=int(ttl * 1000))

    async def unlock(self, session_id):
        await self.r.delete(self._k("run", session_id))

    async def active(self, session_id):
        return await self.r.get(self._k("run", session_id))

    async def publish(self, session_id, event):
        await self.r.publish(self._k("ev", session_id), json.dumps(event, ensure_ascii=False, default=str))

    async def open(self, session_id):
        ps = self.r.pubsub()
        await ps.subscribe(self._k("ev", session_id))
        # Ждём подтверждения: после него Redis гарантированно доставит каждую публикацию.
        for _ in range(50):
            msg = await ps.get_message(timeout=0.1)
            if msg and msg.get("type") == "subscribe":
                break
        return _RedisSub(ps)

    async def append_partial(self, session_id, text):
        k = self._k("partial", session_id)
        await self.r.append(k, text)
        await self.r.expire(k, 3600)

    async def clear_partial(self, session_id):
        await self.r.delete(self._k("partial", session_id))

    async def partial(self, session_id):
        return await self.r.get(self._k("partial", session_id)) or ""

    async def broadcast(self, message):
        await self.r.publish(self._k("broadcast"), json.dumps({**message, "_node": self.node}, ensure_ascii=False))
        for h in list(self._handlers):
            await h(message)

    def on_broadcast(self, handler):
        self._handlers.append(handler)

    def lock(self, key, ttl=30.0):
        return _RedisLock(self.r, self._k("lock", key), ttl)

    async def start(self):
        await self.r.ping()
        self._task = asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        while True:
            ps = self.r.pubsub()
            try:
                await ps.subscribe(self._k("broadcast"))
                async for msg in ps.listen():
                    if msg.get("type") != "message":
                        continue
                    data = json.loads(msg["data"])
                    if data.pop("_node", None) == self.node:
                        continue
                    for h in list(self._handlers):
                        try:
                            await h(data)
                        except Exception:  # noqa: BLE001
                            log.exception("broadcast handler failed")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — Redis перезапустился: переподключаемся
                log.warning("redis broadcast: переподключение", exc_info=True)
                await asyncio.sleep(1)
            finally:
                with contextlib.suppress(Exception):
                    await ps.aclose()

    async def close(self):
        if self._task:
            self._task.cancel()
        await self.r.aclose()


def make_bus(redis_url: str = "") -> Bus:
    return RedisBus(redis_url) if redis_url else LocalBus()
