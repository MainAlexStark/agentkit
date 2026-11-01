"""MCP-клиент: Streamable HTTP и stdio, без SDK.

Свой клиент, а не официальный SDK: SDK между версиями 1.x и 2.x менял API
целиком, а нужна нам малая часть протокола — initialize, tools/list,
tools/call. ~250 строк проще держать под контролем, чем догонять SDK
(тот же выбор сделан в OpenSeller, pkg/mcpclient).

Инструменты сервера становятся инструментами агента с именем
mcp__<сервер>__<инструмент>; подсказки readOnlyHint / destructiveHint из
аннотаций идут прямо в слой прав.

Сервер хоста с ``per_user: true`` — это инструменты приложения, в которое
встроен агент (магазин, CRM): у каждого пользователя свой набор и свои данные.
Список берётся отдельно для каждого разговора (с кэшем): каждый запрос несёт
X-Agent-User-Id / X-Agent-Roles / X-Agent-Session-Id, вызов — ещё X-Agent-Run-Id.
Разговор, а не только пользователь — потому что хост сам решает по сессии, что
это за разговор: общий чат магазина или работа над одним товаром, в каком
магазине, — и отдаёт соответствующие инструменты и инструкции.

Список кэшируется (per_user_ttl) и сбрасывается после изменяющего вызова — или
после любого, в результате которого хост поставил ``_meta["agentkit/refresh"]``:
так хост говорит «я поменял твой контекст» про действие, которое для прав
агента — чтение (например, «запомни» без подтверждения меняет инструкции).

Ещё два ключа ``_meta`` — для инструментов, которые меняют данные хоста без
подтверждения человека (агент карточки товара: человек подтверждает только
публикацию):

* в описании инструмента ``"agentkit/concurrencySafe": false`` — не выполнять
  параллельно с соседними вызовами, хоть он и «чтение» для прав: два изменения
  одной карточки разом затёрли бы друг друга;
* в результате ``"agentkit/stop": "done"`` (или ``"waiting_input"``) — прогон
  заканчивается после этого шага: инструмент сам подвёл итог (``finish``). Верить этим заголовкам хост может только вместе с токеном
сервера (``token``): без него любой в сети представился бы чужим пользователем.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from agentkit.redact import remember
from agentkit.tools.base import Tool, ToolContext, ToolResult
from agentkit.tools.registry import match_any

if TYPE_CHECKING:
    from agentkit.agent import TurnContext
    from agentkit.tools.base import User

log = logging.getLogger("agentkit.mcp")

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "agentkit", "version": "0.2.0"}


# Ключ _meta результата tools/call: хост просит перечитать список и инструкции (см. docstring).
REFRESH_META = "agentkit/refresh"
CONCURRENCY_META = "agentkit/concurrencySafe"
STOP_META = "agentkit/stop"


class MCPError(Exception):
    pass


class MCPAuthRequired(MCPError):
    """Сервер ответил 401: нужен токен или вход по OAuth."""


TokenProvider = Callable[[], Awaitable[str | None]]


@dataclass
class MCPServerConfig:
    name: str
    url: str | None = None  # Streamable HTTP
    command: str | None = None  # stdio
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    token: str | None = None  # Bearer
    include: list[str] = field(default_factory=list)  # шаблоны имён инструментов сервера; пусто — все
    exclude: list[str] = field(default_factory=list)
    # None — решить автоматически (много инструментов → отложенные); True/False — принудительно.
    deferred: bool | None = None
    # Шаблоны инструментов, которые считать read-only, даже если сервер не дал аннотаций.
    read_only: list[str] = field(default_factory=list)
    # Доверять ли readOnlyHint сервера. Для чужих серверов разумно False:
    # подсказка — это заявление сервера, а не проверка.
    trust_annotations: bool = True
    timeout: float = 120.0
    enabled: bool = True
    # OAuth 2.1: true или {"scope": …, "client_id": …, "client_secret": …, "auth_params": {…}}.
    # Без этого поля OAuth всё равно предложится, если сервер ответил 401.
    oauth: Any = None
    # Инструменты хоста на пользователя: см. docstring модуля.
    per_user: bool = False
    # Сколько секунд держать личный список инструментов. Изменяющий вызов сбрасывает его сразу:
    # после «подключи Gmail» новые инструменты нужны на следующем шаге, а не через минуту.
    per_user_ttl: float = 60.0
    # Приставка имён инструментов; None — «mcp__<сервер>__». "" — имена как у сервера: для сервера
    # хоста, чьи инструменты — предметные инструменты агента (промпт хоста и его страница знают их
    # по голым именам). Совпавшее со встроенным имя отбрасывается — встроенный не подменить.
    prefix: str | None = None
    # Вход по OAuth у каждого пользователя свой (магазин у каждого продавца свой): общий процесс
    # такой сервер не подключает, его инструменты появляются в прогонах пользователя после его входа.
    # Не путать с per_user: там список инструментов даёт хост по заголовкам X-Agent-User-Id, а здесь
    # сервер общий, а токены — у каждого свои. В API настроек чата и в базе поле зовётся per_user
    # (так его сохраняли до появления per_user у сервера хоста) — mcp_from_dict переводит.
    personal_login: bool = False
    # Адрес ввёл не админ (личный сервер): подключаться только к публичным адресам — netguard.
    public_only: bool = False


class _HTTPTransport:
    def __init__(self, cfg: MCPServerConfig, client: httpx.AsyncClient | None = None,
                 token_provider: TokenProvider | None = None, on_unauthorized: TokenProvider | None = None):
        self.cfg = cfg
        self.token_provider = token_provider
        self.on_unauthorized = on_unauthorized
        self._own = client is None
        timeout = httpx.Timeout(cfg.timeout, connect=10.0)
        if client is None and cfg.public_only:
            from agentkit.netguard import public_client  # noqa: PLC0415

            client = public_client(timeout=timeout)
        # Явно переданный клиент главнее public_only — так тесты подставляют MockTransport.
        self.client = client or httpx.AsyncClient(timeout=timeout)
        self.session_id: str | None = None
        self.protocol: str | None = None

    async def request(self, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any] | None:
        try:
            return await self._request(payload, await self._token(), headers)
        except MCPAuthRequired:
            # Токен могли отозвать раньше срока: обновляем один раз и повторяем.
            if self.on_unauthorized is None:
                raise
            token = await self.on_unauthorized()
            if not token:
                raise
            return await self._request(payload, token, headers)

    async def _token(self) -> str | None:
        if self.token_provider is not None:
            return await self.token_provider()
        return self.cfg.token

    async def _request(self, payload: dict[str, Any], token: str | None,
                       extra: dict[str, str] | None = None) -> dict[str, Any] | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.cfg.headers,
            **(extra or {}),
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        async with self.client.stream("POST", self.cfg.url, headers=headers, json=payload) as r:
            if sid := r.headers.get("mcp-session-id"):
                self.session_id = sid
            if r.status_code == 401:
                raise MCPAuthRequired(f"{self.cfg.name}: 401 — нужен вход (OAuth) или токен")
            if r.status_code >= 400:
                body = (await r.aread()).decode("utf-8", "replace")[:500]
                raise MCPError(f"{self.cfg.name}: HTTP {r.status_code}: {body}")
            if "id" not in payload or r.status_code == 202:
                return None
            ctype = r.headers.get("content-type", "")
            if "text/event-stream" in ctype:
                async for line in r.aiter_lines():
                    if line.startswith("data:"):
                        try:
                            msg = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        if msg.get("id") == payload["id"]:
                            return msg
                raise MCPError(f"{self.cfg.name}: поток закрылся без ответа")
            return json.loads(await r.aread())

    async def close(self) -> None:
        if self._own:
            await self.client.aclose()


class _StdioTransport:
    def __init__(self, cfg: MCPServerConfig):
        self.cfg = cfg
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            self.cfg.command, *self.cfg.args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **self.cfg.env},
            limit=16 * 1024 * 1024,
        )
        asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for line in self.proc.stderr:
            log.debug("[%s] %s", self.cfg.name, line.decode("utf-8", "replace").rstrip())

    async def request(self, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any] | None:
        if self.proc is None:
            await self.start()
        assert self.proc and self.proc.stdin and self.proc.stdout
        async with self.lock:  # stdio — один поток, запросы по очереди
            self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            await self.proc.stdin.drain()
            if "id" not in payload:
                return None
            while True:
                line = await asyncio.wait_for(self.proc.stdout.readline(), self.cfg.timeout)
                if not line:
                    raise MCPError(f"{self.cfg.name}: процесс сервера завершился")
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue  # мусор в stdout — частая ошибка stdio-серверов
                if msg.get("id") == payload["id"] and ("result" in msg or "error" in msg):
                    return msg
                if "method" in msg and "id" in msg:  # запрос сервера к клиенту (ping и т. п.)
                    reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {}} if msg["method"] == "ping" else {
                        "jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}}
                    self.proc.stdin.write((json.dumps(reply) + "\n").encode())
                    await self.proc.stdin.drain()

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()


class MCPClient:
    def __init__(self, cfg: MCPServerConfig, http_client: httpx.AsyncClient | None = None,
                 token_provider: TokenProvider | None = None, on_unauthorized: TokenProvider | None = None):
        self.cfg = cfg
        if cfg.url:
            self.t: _HTTPTransport | _StdioTransport = _HTTPTransport(cfg, http_client, token_provider,
                                                                      on_unauthorized)
        elif cfg.command:
            self.t = _StdioTransport(cfg)
        else:
            raise MCPError(f"{cfg.name}: нужен url или command")
        self._id = 0
        self.server_info: dict[str, Any] = {}
        self.instructions: str = ""
        self.initialized = False

    async def _rpc(self, method: str, params: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> Any:
        self._id += 1
        msg = await self.t.request({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}},
                                   headers)
        if msg is None:
            raise MCPError(f"{self.cfg.name}: пустой ответ на {method}")
        if err := msg.get("error"):
            raise MCPError(f"{self.cfg.name}: {method}: {err.get('message')} ({err.get('code')})")
        return msg.get("result")

    async def initialize(self) -> None:
        res = await self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
        self.server_info = res.get("serverInfo", {})
        self.instructions = res.get("instructions") or ""
        if isinstance(self.t, _HTTPTransport):
            self.t.protocol = res.get("protocolVersion", PROTOCOL_VERSION)
        await self.t.request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.initialized = True

    async def list_tools(self) -> list[dict[str, Any]]:
        if not self.initialized:
            await self.initialize()
        tools: list[dict[str, Any]] = []
        cursor = None
        while True:
            res = await self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(res.get("tools", []))
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict[str, Any],
                        headers: dict[str, str] | None = None) -> dict[str, Any]:
        if not self.initialized:
            await self.initialize()
        try:
            return await self._rpc("tools/call", {"name": name, "arguments": arguments}, headers)
        except MCPError as e:
            # Сессия на сервере могла истечь (перезапуск сервера) — один раз переподключаемся.
            if "session" in str(e).lower() or "404" in str(e):
                self.initialized = False
                if isinstance(self.t, _HTTPTransport):
                    self.t.session_id = None
                await self.initialize()
                return await self._rpc("tools/call", {"name": name, "arguments": arguments}, headers)
            raise

    async def close(self) -> None:
        await self.t.close()


def _safe(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)


def mcp_tool_name(server: str, tool: str, prefix: str | None = None) -> str:
    pre = f"mcp__{_safe(server)}__" if prefix is None else _safe(prefix) if prefix else ""
    return f"{pre}{_safe(tool)}"[:64]


def render_content(result: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """content[] сервера → текст для модели + вложения (картинки) для UI."""
    parts: list[str] = []
    attachments: list[dict[str, Any]] = []
    for c in result.get("content") or []:
        t = c.get("type")
        if t == "text":
            parts.append(c.get("text", ""))
        elif t == "image":
            attachments.append({"type": "image", "mime": c.get("mimeType"), "data": c.get("data")})
            parts.append(f"[картинка {c.get('mimeType', '')}]")
        elif t == "resource":
            r = c.get("resource") or {}
            parts.append(r.get("text") or f"[ресурс {r.get('uri', '')}]")
        elif t == "resource_link":
            parts.append(f"[ссылка на ресурс {c.get('uri', '')}: {c.get('name', '')}]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return "\n".join(parts), attachments


@dataclass
class ServerStatus:
    name: str
    ok: bool
    tools: int = 0
    error: str = ""
    server_info: dict[str, Any] = field(default_factory=dict)
    instructions: str = ""
    needs_auth: bool = False  # можно подключить по OAuth (кнопка «Подключить»)
    oauth: bool = False  # подключён по OAuth
    per_user: bool = False  # инструменты хоста: список у каждого пользователя свой


def user_headers(user: User) -> dict[str, str]:
    """Кто работает с агентом — для MCP-сервера хоста. Значения в percent-encoding:
    заголовок HTTP — latin-1, а id и роли хоста бывают не только ASCII."""
    h = {"X-Agent-User-Id": quote(str(user.id), safe="@._-:")}
    if user.roles:
        h["X-Agent-Roles"] = quote(",".join(user.roles), safe=",._-:")
    return h


@dataclass
class _UserTools:
    """Личный список инструментов сервера хоста для одного разговора пользователя."""

    tools: list[Tool]
    instructions: str
    error: str
    at: float


class MCPManager:
    """Держит подключения к серверам и превращает их инструменты в Tool."""

    AUTO_DEFER_THRESHOLD = 15  # больше инструментов на сервере — отложенные
    MAX_USERS = 2000  # личных списков (разговоров) в памяти процесса; старые вытесняются
    ERROR_TTL = 10.0  # недоступный сервер хоста пробуем снова через столько секунд, а не через per_user_ttl

    def __init__(self, configs: list[MCPServerConfig], http_client: httpx.AsyncClient | None = None,
                 oauth: Any = None, *, owner: str = "", connect_timeout: float | None = None):
        self.configs = [c for c in configs if c.enabled]
        self.http_client = http_client
        self.oauth = oauth  # OAuthBroker: токены серверов, подключённых через вход
        # Чьи OAuth-токены брать: "" — общие (подключал admin), id пользователя — его личные.
        self.owner = owner
        # Ожидание списка инструментов при подключении. Личные серверы подключаются на первом шаге
        # прогона — мёртвый сервер не должен держать ответ человеку cfg.timeout (120 с).
        self.connect_timeout = connect_timeout
        self.clients: dict[str, MCPClient] = {}
        self.status: dict[str, ServerStatus] = {}
        self.per_user = [c for c in self.configs if c.per_user]
        self._users: dict[tuple[str, str, str], _UserTools] = {}
        self._user_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._user_http: httpx.AsyncClient | None = None

    async def connect(self) -> list[Tool]:
        """Общие серверы — сразу; серверы хоста (per_user) — при первом ходе каждого пользователя."""
        results = await asyncio.gather(*(self._connect_one(c) for c in self.configs if not c.per_user))
        for c in self.per_user:
            if not c.url:
                self.status[c.name] = ServerStatus(c.name, ok=False, per_user=True,
                                                   error="per_user — только для HTTP-сервера: укажите url в mcp.json")
            else:
                self.status[c.name] = ServerStatus(c.name, ok=True, per_user=True)
        return [t for tools in results for t in tools]

    # ── серверы хоста: инструменты каждого пользователя ─────────────────

    async def turn_context(self, user: User, session_id: str) -> TurnContext | None:
        """Поставщик контекста для Agent: личные инструменты и инструкции серверов хоста."""
        if not self.per_user:
            return None
        from agentkit.agent import TurnContext

        out = TurnContext()
        texts = []
        for cfg in self.per_user:
            if not cfg.url:
                continue
            got = await self._user_tools(cfg, user, session_id)
            out.tools.extend(got.tools)
            if got.error:
                # Модели — текстом, а не исключением: она скажет человеку, что сервис недоступен,
                # вместо того чтобы молча отвечать без половины инструментов.
                texts.append(f"### {cfg.name}\nИнструменты «{cfg.name}» сейчас недоступны ({got.error}). "
                             "Если они нужны для ответа — скажи об этом человеку.")
            elif got.instructions.strip():
                texts.append(f"### {cfg.name}\n{got.instructions.strip()}")
        out.text = "\n\n".join(texts)
        return out

    def forget_user(self, user_id: str, server: str | None = None) -> None:
        """Сбросить личные списки во всех разговорах пользователя: следующий ход возьмёт их у хоста
        заново. Во всех, а не в одном: подключённый в одном разговоре сервис нужен и в соседнем."""
        for key in [k for k in self._users if k[1] == user_id and (server is None or k[0] == server)]:
            self._users.pop(key, None)

    async def _user_tools(self, cfg: MCPServerConfig, user: User, session_id: str) -> _UserTools:
        key = (cfg.name, str(user.id), session_id)
        lock = self._user_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cur = self._users.get(key)
            ttl = self.ERROR_TTL if cur and cur.error else cfg.per_user_ttl
            if cur and time.monotonic() - cur.at < ttl:
                return cur
            # Новый клиент на каждое обновление: инструкции сервера (контекст пользователя)
            # приходят в initialize, а старый клиент дослужит вызовам, начатым на нём.
            if self.http_client is None and self._user_http is None:
                self._user_http = httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout, connect=10.0))
            headers = {**cfg.headers, **user_headers(user), "X-Agent-Session-Id": quote(session_id or "")}
            client = MCPClient(replace(cfg, headers=headers),
                               self.http_client or self._user_http)
            try:
                raw = await asyncio.wait_for(client.list_tools(), cfg.timeout)
            except Exception as e:  # noqa: BLE001 — хост недоступен: агент работает без его инструментов
                log.warning("MCP %s (пользователь %s): %s", cfg.name, user.id, e)
                got = _UserTools([], "", str(e)[:300], time.monotonic())
            else:
                raw = [t for t in raw if (not cfg.include or match_any(t["name"], cfg.include))
                       and not match_any(t["name"], cfg.exclude)]
                deferred = cfg.deferred if cfg.deferred is not None else len(raw) > self.AUTO_DEFER_THRESHOLD
                got = _UserTools([self._wrap(cfg, client, t, deferred, user_id=str(user.id)) for t in raw],
                                 client.instructions, "", time.monotonic())
            self._users[key] = got
            if len(self._users) > self.MAX_USERS:
                oldest = min(self._users, key=lambda k: self._users[k].at)
                self._users.pop(oldest, None)
                self._user_locks.pop(oldest, None)
            return got

    async def _connect_one(self, cfg: MCPServerConfig) -> list[Tool]:
        # Токен и заголовки сервера — секреты: сервер может вернуть их в ответе или ошибке
        # (эхо запроса, отладочный вывод), а оттуда они ушли бы модели и в журнал.
        remember(cfg.token, f"токен MCP {cfg.name}")
        for k, v in {**cfg.headers, **cfg.env}.items():
            remember(v, f"{k} MCP {cfg.name}")
        provider = refresh = None
        use_oauth = False
        if self.oauth is not None and cfg.url and not cfg.token:
            use_oauth = bool(await self.oauth.token(cfg.name, self.owner)) or bool(cfg.oauth)
            if use_oauth:
                async def provider(name=cfg.name):
                    return await self.oauth.token(name, self.owner)

                async def refresh(name=cfg.name):
                    return await self.oauth.force_refresh(name, self.owner)

                if not await provider():
                    self.status[cfg.name] = ServerStatus(cfg.name, ok=False, needs_auth=True,
                                                         error="нужно подключить: войдите через OAuth")
                    return []
        try:
            client = MCPClient(cfg, self.http_client, provider, refresh)
            raw = await asyncio.wait_for(client.list_tools(), self.connect_timeout or cfg.timeout)
        except MCPAuthRequired as e:
            log.warning("MCP %s: %s", cfg.name, e)
            self.status[cfg.name] = ServerStatus(cfg.name, ok=False, error=str(e),
                                                 needs_auth=self.oauth is not None and bool(cfg.url))
            return []
        except Exception as e:  # noqa: BLE001 — один упавший сервер не должен ронять агента
            log.warning("MCP %s: %s", cfg.name, e)
            # У TimeoutError текст пустой — в UI стояло бы «ошибка» без объяснения.
            why = f"не ответил за {self.connect_timeout or cfg.timeout:.0f} с" if isinstance(e, TimeoutError) else str(e)
            self.status[cfg.name] = ServerStatus(cfg.name, ok=False, error=why or type(e).__name__)
            return []
        self.clients[cfg.name] = client
        raw = [t for t in raw if (not cfg.include or match_any(t["name"], cfg.include))
               and not match_any(t["name"], cfg.exclude)]
        deferred = cfg.deferred if cfg.deferred is not None else len(raw) > self.AUTO_DEFER_THRESHOLD
        tools = [self._wrap(cfg, client, t, deferred) for t in raw]
        self.status[cfg.name] = ServerStatus(cfg.name, ok=True, tools=len(tools), server_info=client.server_info,
                                             instructions=client.instructions, oauth=use_oauth)
        return tools

    def _wrap(self, cfg: MCPServerConfig, client: MCPClient, spec: dict[str, Any], deferred: bool,
              user_id: str | None = None) -> Tool:
        ann = spec.get("annotations") or {}
        remote = spec["name"]
        read_only = match_any(remote, cfg.read_only) or (cfg.trust_annotations and bool(ann.get("readOnlyHint")))
        destructive = cfg.trust_annotations and bool(ann.get("destructiveHint")) and not read_only

        async def handler(ctx: ToolContext, **args: Any) -> ToolResult:
            headers = None
            if user_id is not None:
                # Сессия — из вызова, а не из списка: субагент работает в своей дочерней сессии.
                headers = {"X-Agent-Session-Id": quote(ctx.session_id or ""), "X-Agent-Run-Id": quote(ctx.run_id or "")}
                if not read_only:
                    # Изменение у хоста могло поменять сам набор инструментов или контекст
                    # (подключил сервис, запомнил правило) — следующий шаг возьмёт их заново.
                    self.forget_user(user_id, cfg.name)
            res = await client.call_tool(remote, args, headers)
            meta = res.get("_meta") or {}
            if user_id is not None and meta.get(REFRESH_META):
                self.forget_user(user_id, cfg.name)
            text, attachments = render_content(res)
            ui = [{"type": "attachments", "items": attachments}] if attachments else []
            stop = meta.get(STOP_META) if meta.get(STOP_META) in ("done", "waiting_input") else None
            return ToolResult(content=text or "(пустой ответ)", is_error=bool(res.get("isError")),
                              data=res.get("structuredContent"), ui=ui, stop=stop)

        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        desc = spec.get("description") or spec.get("title") or remote
        safe = (spec.get("_meta") or {}).get(CONCURRENCY_META)
        return Tool(
            title=spec.get("title") or ann.get("title") or "",
            name=mcp_tool_name(cfg.name, remote, cfg.prefix),
            description=f"[{cfg.name}] {desc}",
            parameters=schema,
            handler=handler,
            read_only=read_only,
            destructive=destructive,
            deferred=deferred,
            concurrency_safe=safe if isinstance(safe, bool) else None,
            timeout=cfg.timeout,
            group=f"mcp:{cfg.name}",
            source="mcp",
        )

    def config(self, name: str) -> MCPServerConfig | None:
        return next((c for c in self.configs if c.name == name), None)

    def instructions(self) -> str:
        parts = [f"### {s.name}\n{s.instructions.strip()}" for s in self.status.values() if s.ok and s.instructions]
        return "\n\n".join(parts)

    async def close(self) -> None:
        for c in self.clients.values():
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                pass
        if self._user_http is not None:
            await self._user_http.aclose()
            self._user_http = None
