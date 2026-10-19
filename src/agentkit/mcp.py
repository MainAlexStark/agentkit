"""MCP-клиент: Streamable HTTP и stdio, без SDK.

Свой клиент, а не официальный SDK: SDK между версиями 1.x и 2.x менял API
целиком, а нужна нам малая часть протокола — initialize, tools/list,
tools/call. ~250 строк проще держать под контролем, чем догонять SDK
(тот же выбор сделан в OpenSeller, pkg/mcpclient).

Инструменты сервера становятся инструментами агента с именем
mcp__<сервер>__<инструмент>; подсказки readOnlyHint / destructiveHint из
аннотаций идут прямо в слой прав.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from agentkit.tools.base import Tool, ToolContext, ToolResult
from agentkit.tools.registry import match_any

log = logging.getLogger("agentkit.mcp")

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "agentkit", "version": "0.1.0"}


class MCPError(Exception):
    pass


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


class _HTTPTransport:
    def __init__(self, cfg: MCPServerConfig, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._own = client is None
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout, connect=10.0))
        self.session_id: str | None = None
        self.protocol: str | None = None

    async def request(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.cfg.headers,
        }
        if self.cfg.token:
            headers["Authorization"] = f"Bearer {self.cfg.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        async with self.client.stream("POST", self.cfg.url, headers=headers, json=payload) as r:
            if sid := r.headers.get("mcp-session-id"):
                self.session_id = sid
            if r.status_code == 401:
                raise MCPError(f"{self.cfg.name}: 401 — нужен токен (Authorization) или OAuth")
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

    async def request(self, payload: dict[str, Any]) -> dict[str, Any] | None:
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
    def __init__(self, cfg: MCPServerConfig, http_client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        if cfg.url:
            self.t: _HTTPTransport | _StdioTransport = _HTTPTransport(cfg, http_client)
        elif cfg.command:
            self.t = _StdioTransport(cfg)
        else:
            raise MCPError(f"{cfg.name}: нужен url или command")
        self._id = 0
        self.server_info: dict[str, Any] = {}
        self.instructions: str = ""
        self.initialized = False

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._id += 1
        msg = await self.t.request({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
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

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.initialized:
            await self.initialize()
        try:
            return await self._rpc("tools/call", {"name": name, "arguments": arguments})
        except MCPError as e:
            # Сессия на сервере могла истечь (перезапуск сервера) — один раз переподключаемся.
            if "session" in str(e).lower() or "404" in str(e):
                self.initialized = False
                if isinstance(self.t, _HTTPTransport):
                    self.t.session_id = None
                await self.initialize()
                return await self._rpc("tools/call", {"name": name, "arguments": arguments})
            raise

    async def close(self) -> None:
        await self.t.close()


def _safe(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)


def mcp_tool_name(server: str, tool: str) -> str:
    name = f"mcp__{_safe(server)}__{_safe(tool)}"
    return name[:64]


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


class MCPManager:
    """Держит подключения к серверам и превращает их инструменты в Tool."""

    AUTO_DEFER_THRESHOLD = 15  # больше инструментов на сервере — отложенные

    def __init__(self, configs: list[MCPServerConfig], http_client: httpx.AsyncClient | None = None):
        self.configs = [c for c in configs if c.enabled]
        self.http_client = http_client
        self.clients: dict[str, MCPClient] = {}
        self.status: dict[str, ServerStatus] = {}

    async def connect(self) -> list[Tool]:
        results = await asyncio.gather(*(self._connect_one(c) for c in self.configs))
        return [t for tools in results for t in tools]

    async def _connect_one(self, cfg: MCPServerConfig) -> list[Tool]:
        try:
            client = MCPClient(cfg, self.http_client)
            raw = await asyncio.wait_for(client.list_tools(), cfg.timeout)
        except Exception as e:  # noqa: BLE001 — один упавший сервер не должен ронять агента
            log.warning("MCP %s: %s", cfg.name, e)
            self.status[cfg.name] = ServerStatus(cfg.name, ok=False, error=str(e))
            return []
        self.clients[cfg.name] = client
        raw = [t for t in raw if (not cfg.include or match_any(t["name"], cfg.include))
               and not match_any(t["name"], cfg.exclude)]
        deferred = cfg.deferred if cfg.deferred is not None else len(raw) > self.AUTO_DEFER_THRESHOLD
        tools = [self._wrap(cfg, client, t, deferred) for t in raw]
        self.status[cfg.name] = ServerStatus(cfg.name, ok=True, tools=len(tools), server_info=client.server_info,
                                             instructions=client.instructions)
        return tools

    def _wrap(self, cfg: MCPServerConfig, client: MCPClient, spec: dict[str, Any], deferred: bool) -> Tool:
        ann = spec.get("annotations") or {}
        remote = spec["name"]
        read_only = match_any(remote, cfg.read_only) or (cfg.trust_annotations and bool(ann.get("readOnlyHint")))
        destructive = cfg.trust_annotations and bool(ann.get("destructiveHint")) and not read_only

        async def handler(ctx: ToolContext, **args: Any) -> ToolResult:
            res = await client.call_tool(remote, args)
            text, attachments = render_content(res)
            ui = [{"type": "attachments", "items": attachments}] if attachments else []
            return ToolResult(content=text or "(пустой ответ)", is_error=bool(res.get("isError")),
                              data=res.get("structuredContent"), ui=ui)

        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        desc = spec.get("description") or spec.get("title") or remote
        return Tool(
            name=mcp_tool_name(cfg.name, remote),
            description=f"[{cfg.name}] {desc}",
            parameters=schema,
            handler=handler,
            read_only=read_only,
            destructive=destructive,
            deferred=deferred,
            timeout=cfg.timeout,
            group=f"mcp:{cfg.name}",
            source="mcp",
        )

    def instructions(self) -> str:
        parts = [f"### {s.name}\n{s.instructions.strip()}" for s in self.status.values() if s.ok and s.instructions]
        return "\n\n".join(parts)

    async def close(self) -> None:
        for c in self.clients.values():
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                pass
