"""OAuth 2.1 клиент для MCP-серверов (порт pkg/oauthclient из OpenSeller).

Сервис агента — публичный OAuth-клиент: проводит администратора через экран
согласия MCP-сервера (authorization code + PKCE S256), хранит токены в базе
и сам обновляет их. Стандарты: RFC 9728 (protected-resource metadata),
RFC 8414 + OpenID Discovery (метаданные сервера авторизации), RFC 7591
(динамическая регистрация клиента), RFC 8707 (resource), RFC 7009 (отзыв).

Поток:
    POST /api/mcp/<сервер>/connect  → discover → DCR (если нужно) → адрес согласия
    человек вводит пароль на MCP-сервере → редирект на /oauth/callback?code&state
    /oauth/callback                  → обмен кода на токены → все процессы перечитывают MCP

Пароль от MCP-сервера агент никогда не видит. Токены — в ak_kv (ns="oauth"),
верификатор PKCE — там же по state, поэтому callback может прийти в любой
процесс сервиса. Обновление токена — под общей блокировкой Bus: refresh
ротируется, и два одновременных обновления оставили бы один процесс без токена.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from agentkit.bus import Bus, LocalBus
from agentkit.store.base import Store

NS = "oauth"
PENDING_TTL = 900  # секунд на прохождение экрана согласия


class OAuthError(Exception):
    pass


class NoRegistration(OAuthError):
    """Сервер не умеет DCR — нужен client_id из его консоли (oauth.client_id в mcp.json)."""


@dataclass
class Endpoints:
    authorization: str = ""
    token: str = ""
    registration: str = ""
    revocation: str = ""
    resource: str = ""
    issuer: str = ""


@dataclass
class OAuthRecord:
    """Всё, что известно про подключение к одному серверу."""

    server_url: str = ""
    endpoints: Endpoints = field(default_factory=Endpoints)
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    manual_client: bool = False  # client_id задан вручную — DCR не повторять
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0
    scope: str = ""
    connected_by: str = ""
    connected_at: float = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> OAuthRecord:
        if not d:
            return cls()
        d = dict(d)
        d["endpoints"] = Endpoints(**(d.get("endpoints") or {}))
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in fields})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_www_authenticate(header: str) -> str:
    marker = 'resource_metadata="'
    i = header.find(marker)
    if i < 0:
        return ""
    rest = header[i + len(marker):]
    j = rest.find('"')
    return rest[:j] if j >= 0 else ""


async def _get_json(http: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    try:
        r = await http.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def discover(http: httpx.AsyncClient, endpoint: str) -> tuple[Endpoints, bool]:
    """Ищет сервер авторизации MCP-сервера. Возвращает (эндпоинты, нужен_ли_вход).

    1. initialize без токена: 401 + WWW-Authenticate resource_metadata;
    2. иначе protected-resource metadata по well-known адресу ресурса;
    3. иначе сам хост как сервер авторизации.
    """
    u = urlparse(endpoint)
    if not u.scheme or not u.netloc:
        raise OAuthError(f"адрес {endpoint!r} не разобран")
    origin = f"{u.scheme}://{u.netloc}"
    probe = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "agentkit", "version": "0.1"}}}
    try:
        r = await http.post(endpoint, json=probe, headers={
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"})
    except httpx.HTTPError as e:
        raise OAuthError(f"запрос к {endpoint}: {e}") from e
    needs_auth = r.status_code in (401, 403)

    prm = None
    if meta := _parse_www_authenticate(r.headers.get("www-authenticate", "")):
        prm = await _get_json(http, meta)
    if prm is None:
        path = u.path.rstrip("/")
        for cand in (f"{origin}/.well-known/oauth-protected-resource{path}",
                     f"{origin}/.well-known/oauth-protected-resource"):
            m = await _get_json(http, cand)
            if m and m.get("authorization_servers"):
                prm = m
                break
    ep = Endpoints()
    issuer = origin
    if prm is not None:
        ep.resource = prm.get("resource") or ""
        servers = prm.get("authorization_servers") or []
        if servers and servers[0]:
            issuer = servers[0]
        needs_auth = True
    if not needs_auth:
        return ep, False
    ep.issuer = issuer
    iu = urlparse(issuer)
    iorigin, ipath = f"{iu.scheme}://{iu.netloc}", iu.path.rstrip("/")
    asm = None
    for cand in (f"{iorigin}/.well-known/oauth-authorization-server{ipath}",
                 f"{iorigin}/.well-known/openid-configuration{ipath}",
                 f"{issuer.rstrip('/')}/.well-known/openid-configuration"):
        m = await _get_json(http, cand)
        if m and m.get("authorization_endpoint"):
            asm = m
            break
    if asm is None:
        raise OAuthError(f"сервер требует входа, но метаданные OAuth у {issuer} не найдены")
    ep.authorization = asm.get("authorization_endpoint") or ""
    ep.token = asm.get("token_endpoint") or ""
    ep.registration = asm.get("registration_endpoint") or ""
    ep.revocation = asm.get("revocation_endpoint") or ""
    if not ep.authorization or not ep.token:
        raise OAuthError(f"у {issuer} нет адресов authorize/token")
    return ep, True


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@dataclass
class OAuthOptions:
    """Настройки сервера из mcp.json: "oauth": {"scope": …, "client_id": …, "client_secret": …, "auth_params": {…}}."""

    scope: str = ""
    client_id: str = ""
    client_secret: str = ""
    auth_params: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, v: Any) -> OAuthOptions | None:
        if not v:
            return None
        if v is True:
            return cls()
        return cls(**{k: x for k, x in dict(v).items() if k in cls.__dataclass_fields__})


class OAuthBroker:
    """Подключения к MCP-серверам по OAuth для всего сервиса (токен общий на сервер)."""

    def __init__(self, store: Store, redirect_uri: str, *, bus: Bus | None = None,
                 http: httpx.AsyncClient | None = None, client_name: str = "agentkit"):
        self.store = store
        self.redirect_uri = redirect_uri
        self.bus = bus or LocalBus()
        self.http = http or httpx.AsyncClient(timeout=30, follow_redirects=True)
        self.client_name = client_name

    async def record(self, server: str) -> OAuthRecord:
        return OAuthRecord.from_dict(await self.store.kv_get(NS, server))

    async def _save(self, server: str, rec: OAuthRecord) -> None:
        await self.store.kv_set(NS, server, rec.to_dict())

    async def start(self, server: str, url: str, opts: OAuthOptions | None = None, *, user_id: str = "") -> str:
        """Адрес экрана согласия. Каждый «Подключить» заново ищет метаданные — сервер мог переехать."""
        opts = opts or OAuthOptions()
        rec = await self.record(server)
        ep, needs = await discover(self.http, url)
        if not needs:
            raise OAuthError(f"сервер {server} не требует входа — OAuth не нужен")
        if rec.server_url != url or rec.endpoints.authorization != ep.authorization:
            # Другой сервер авторизации — прежняя регистрация клиента там не существует.
            rec = OAuthRecord()
        rec.server_url, rec.endpoints = url, ep
        await self._ensure_client(rec, opts)
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(24)
        await self.store.kv_set(NS + ":pending", state, {
            "server": server, "verifier": verifier, "user_id": user_id, "expires": time.time() + PENDING_TTL})
        await self._save(server, rec)
        params = {
            "response_type": "code", "client_id": rec.client_id, "redirect_uri": self.redirect_uri,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        }
        if opts.scope:
            params["scope"] = opts.scope
        if ep.resource:
            params["resource"] = ep.resource
        params.update(opts.auth_params)
        sep = "&" if "?" in ep.authorization else "?"
        return ep.authorization + sep + urlencode(params)

    async def _ensure_client(self, rec: OAuthRecord, opts: OAuthOptions) -> None:
        if opts.client_id:
            rec.client_id, rec.client_secret, rec.manual_client = opts.client_id, opts.client_secret, True
            rec.redirect_uri = self.redirect_uri
            return
        # Сменился адрес сервиса — прежняя регистрация знает только старый redirect_uri.
        if rec.client_id and (rec.manual_client or rec.redirect_uri == self.redirect_uri):
            return
        if not rec.endpoints.registration:
            raise NoRegistration("сервер не поддерживает динамическую регистрацию клиента — "
                                 "укажите oauth.client_id (и client_secret) из его консоли в mcp.json")
        meta = {
            "client_name": self.client_name, "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        if opts.scope:
            meta["scope"] = opts.scope
        r = await self.http.post(rec.endpoints.registration, json=meta)
        if r.status_code not in (200, 201):
            raise OAuthError(f"регистрация клиента отклонена: HTTP {r.status_code} {r.text[:300]}")
        out = r.json()
        if not out.get("client_id"):
            raise OAuthError("сервер не выдал client_id")
        rec.client_id = out["client_id"]
        rec.client_secret = out.get("client_secret") or ""
        rec.redirect_uri = self.redirect_uri
        rec.manual_client = False

    async def finish(self, state: str, code: str) -> str:
        """Обмен кода на токены. Возвращает имя сервера."""
        pending = await self.store.kv_get(NS + ":pending", state)
        await self.store.kv_delete(NS + ":pending", state)  # state одноразовый
        if not pending or pending.get("expires", 0) < time.time():
            raise OAuthError("ссылка подключения устарела или уже использована — начните заново")
        server = pending["server"]
        rec = await self.record(server)
        form = {"grant_type": "authorization_code", "code": code, "client_id": rec.client_id,
                "redirect_uri": self.redirect_uri, "code_verifier": pending["verifier"]}
        tok = await self._token_request(rec, form)
        self._apply(rec, tok)
        rec.connected_by, rec.connected_at = pending.get("user_id", ""), time.time()
        await self._save(server, rec)
        return server

    async def _token_request(self, rec: OAuthRecord, form: dict[str, str]) -> dict[str, Any]:
        if rec.client_secret:
            form["client_secret"] = rec.client_secret
        if rec.endpoints.resource:
            form["resource"] = rec.endpoints.resource
        r = await self.http.post(rec.endpoints.token, data=form, headers={"Accept": "application/json"})
        if r.status_code != 200:
            raise OAuthError(f"token_endpoint: HTTP {r.status_code} {r.text[:300]}")
        out = r.json()
        if not out.get("access_token"):
            raise OAuthError("сервер не выдал access_token")
        return out

    @staticmethod
    def _apply(rec: OAuthRecord, tok: dict[str, Any]) -> None:
        rec.access_token = tok["access_token"]
        # Refresh ротируется: новый — из ответа; не прислали — старый остаётся в силе.
        rec.refresh_token = tok.get("refresh_token") or rec.refresh_token
        rec.expires_at = time.time() + float(tok["expires_in"]) if tok.get("expires_in") else 0
        rec.scope = tok.get("scope") or rec.scope

    async def token(self, server: str) -> str | None:
        """Действующий access-токен, при необходимости обновлённый. None — сервер не подключён."""
        rec = await self.record(server)
        if not rec.access_token:
            return None
        if not rec.expires_at or rec.expires_at - time.time() > 300:
            return rec.access_token
        async with self.bus.lock(f"oauth:{server}", ttl=30):
            rec = await self.record(server)  # пока ждали блокировку, мог обновить другой процесс
            if rec.expires_at - time.time() > 300:
                return rec.access_token
            if not rec.refresh_token:
                return None
            form = {"grant_type": "refresh_token", "refresh_token": rec.refresh_token, "client_id": rec.client_id}
            try:
                tok = await self._token_request(rec, form)
            except OAuthError:
                return None  # refresh отозван — нужно подключить заново
            self._apply(rec, tok)
            await self._save(server, rec)
            return rec.access_token

    async def force_refresh(self, server: str) -> str | None:
        """После 401 от сервера: токен могли отозвать раньше срока."""
        rec = await self.record(server)
        if rec.access_token:
            rec.expires_at = 1
            await self._save(server, rec)
        return await self.token(server)

    async def disconnect(self, server: str) -> None:
        rec = await self.record(server)
        tok = rec.refresh_token or rec.access_token
        if tok and rec.endpoints.revocation:
            form = {"token": tok, "client_id": rec.client_id}
            if rec.client_secret:
                form["client_secret"] = rec.client_secret
            try:
                await self.http.post(rec.endpoints.revocation, data=form)
            except httpx.HTTPError:
                pass  # отзыв — вежливость; локально токены забываем в любом случае
        rec.access_token = rec.refresh_token = ""
        rec.expires_at = 0
        await self._save(server, rec)

    async def status(self, server: str) -> dict[str, Any]:
        rec = await self.record(server)
        return {"connected": bool(rec.access_token), "scope": rec.scope, "connected_by": rec.connected_by,
                "connected_at": rec.connected_at or None, "expires_at": rec.expires_at or None}
