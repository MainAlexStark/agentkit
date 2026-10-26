"""Превью серверов песочницы: человек открывает то, что агент поднял на компьютере агента.

Агент запускает сервер в контейнере разговора (run_bash background=true), вызывает
sandbox_preview(port) и получает ссылку /sandbox/p/<подпись>/. Подпись (HMAC) —
вместо входа: страницу открывают в новой вкладке, где нет Bearer-токена чата,
а ссылка сама ограничена одним разговором, одним портом и сроком.

Страница песочницы отдаётся с того же адреса, что и чат, поэтому:
* Content-Security-Policy: sandbox без allow-same-origin — у страницы «пустой»
  origin, её JS не прочитает токен чата из localStorage и не вызовет /api от
  имени человека;
* Cookie и Authorization человека наверх не уходят, Set-Cookie сверху — отбрасывается.

Дорога: local — прямо на 127.0.0.1:<порт>; docker — через egress (порт 3129,
токен песочницы), единственный, кто подключён к сети каждого разговора.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from starlette.background import BackgroundTask

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
       "transfer-encoding", "upgrade", "host", "cookie", "authorization", "content-length"}
RESP_DROP = {"connection", "keep-alive", "transfer-encoding", "set-cookie", "content-security-policy",
             "x-frame-options"}
CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
TTL = 8 * 3600
MAX_BODY = 25 * 2**20


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SandboxPreview:
    def __init__(self, secret: bytes, *, public_url: str, sandbox_url: str, preview_url: str = "",
                 token: str = "", client: httpx.AsyncClient | None = None):
        self.secret, self.public_url = secret, public_url.rstrip("/")
        self.sandbox_url, self.preview_url, self.token = sandbox_url, preview_url.rstrip("/"), token
        self.client = client

    @classmethod
    async def create(cls, settings: Any, store: Any) -> SandboxPreview:
        # Секрет в базе, а не в памяти: ссылка, выданная одним процессом, должна открываться в любом
        # и после перезапуска сервиса.
        raw = await store.kv_get("config", "preview_secret")
        if not raw:
            await store.kv_set("config", "preview_secret", secrets.token_hex(32))
            raw = await store.kv_get("config", "preview_secret")
        return cls(bytes.fromhex(raw), public_url=settings.public_url, sandbox_url=settings.sandbox_url,
                   preview_url=settings.sandbox_preview_url, token=settings.sandbox_token)

    @property
    def available(self) -> bool:
        return self.sandbox_url == "local" or bool(self.preview_url)

    # ── подпись ──
    def sign(self, session: str, port: int, ttl: int = TTL) -> str:
        body = _b64(json.dumps({"s": session, "p": port, "e": int(time.time()) + ttl}, separators=(",", ":")).encode())
        mac = _b64(hmac.new(self.secret, body.encode(), hashlib.sha256).digest()[:18])
        return f"{body}.{mac}"

    def verify(self, sig: str) -> tuple[str, int] | None:
        body, _, mac = sig.partition(".")
        good = _b64(hmac.new(self.secret, body.encode(), hashlib.sha256).digest()[:18])
        if not mac or not hmac.compare_digest(mac, good):
            return None
        try:
            d = json.loads(_unb64(body))
        except ValueError:
            return None
        if d.get("e", 0) < time.time():
            return None
        return str(d["s"]), int(d["p"])

    def link(self, session: str, port: int, path: str = "/") -> str:
        return f"{self.public_url}/sandbox/p/{self.sign(session, port)}/{path.lstrip('/')}"

    # ── дорога к серверу ──
    def target(self, session: str, port: int) -> tuple[str, dict[str, str]]:
        if self.sandbox_url == "local":
            return f"http://127.0.0.1:{port}", {}
        return self.preview_url, {"X-Sandbox-Token": self.token, "X-Sandbox-Target": f"ak-sb-{session}:{port}"}

    async def _send(self, method: str, url: str, headers: dict[str, str], body: bytes) -> httpx.Response:
        if self.client is None:  # один на процесс: соединения переиспользуются, ничего не течёт
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))
        req = self.client.build_request(method, url, headers=headers, content=body)
        return await self.client.send(req, stream=True)

    async def check(self, session: str, port: int) -> str | None:
        """None — сервер отвечает; иначе — что сказать модели."""
        base, hdrs = self.target(session, port)
        try:
            r = await self._send("GET", base + "/", hdrs, b"")
            await r.aclose()
        except httpx.HTTPError as e:
            return f"на порту {port} ничего не отвечает ({type(e).__name__})"
        if r.status_code == 502:
            return f"на порту {port} ничего не слушает"
        return None

    async def proxy(self, sig: str, method: str, path: str, query: str, headers: dict[str, str], body: bytes):
        """→ (статус, заголовки, поток байт, закрыть) или (код ошибки, текст)."""
        who = self.verify(sig)
        if who is None:
            return 403, "Ссылка устарела или неверна — попросите агента дать новую (sandbox_preview)."
        session, port = who
        base, extra = self.target(session, port)
        fwd = {k: v for k, v in headers.items() if k.lower() not in HOP and not k.lower().startswith("x-sandbox-")}
        url = f"{base}/{path}" + (f"?{query}" if query else "")
        try:
            r = await self._send(method, url, {**fwd, **extra}, body)
        except httpx.HTTPError:
            return 502, f"На порту {port} компьютера агента ничего не отвечает: сервер остановлен или ещё стартует."
        prefix = f"/sandbox/p/{sig}"
        out = {k: v for k, v in r.headers.items() if k.lower() not in RESP_DROP}
        if (loc := out.get("location", "")).startswith("/"):
            out["location"] = prefix + loc  # редирект на «/login» — внутрь превью, а не в чат
        out["content-security-policy"] = CSP
        out["access-control-allow-origin"] = "*"  # страница с пустым origin зовёт своё API — это кросс-доменно
        out["cache-control"] = out.get("cache-control", "no-store")
        return r.status_code, out, r.aiter_raw(), r.aclose


def register(app: Any, svc: Any) -> None:
    @app.api_route("/sandbox/p/{sig}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                   include_in_schema=False)
    async def sandbox_preview(sig: str, path: str, request: Request):
        pv: SandboxPreview | None = getattr(svc, "preview", None)
        if pv is None or not pv.available:
            return PlainTextResponse("Превью серверов песочницы выключено.", status_code=404)
        body = await request.body()
        if len(body) > MAX_BODY:
            return PlainTextResponse("Слишком большой запрос.", status_code=413)
        res = await pv.proxy(sig, request.method, path, request.url.query, dict(request.headers), body)
        if len(res) == 2:
            return PlainTextResponse(res[1], status_code=res[0], headers={"content-security-policy": CSP})
        status, headers, stream, close = res
        return StreamingResponse(stream, status_code=status, headers=headers, background=BackgroundTask(close))
