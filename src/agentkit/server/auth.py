"""Кто говорит с агентом.

Два способа, оба — Bearer:
  * личный токен пользователя (AGENT_TOKENS) — для веб-чата и скриптов;
  * сервисный токен хост-приложения (AGENT_SERVICE_TOKEN) + заголовки
    X-Agent-User-Id / X-Agent-User-Name / X-Agent-Roles — когда агент встроен
    в другое приложение, у которого своя авторизация (EI_doc, OpenSeller).
    Хост сам проверил пользователя и говорит агенту, от чьего имени работать.

Своя система пользователей сюда не встроена намеренно: у каждого продукта
она уже есть, и агент должен жить в ней, а не рядом.
"""

from __future__ import annotations

import hmac
from urllib.parse import unquote

from fastapi import HTTPException, Request

from agentkit import User


def parse_tokens(spec: str) -> dict[str, User]:
    users: dict[str, User] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) < 2:
            raise ValueError(f"AGENT_TOKENS: ожидалось id:токен[:роли], получено «{item}»")
        uid, token = parts[0], parts[1]
        roles = [r for r in (parts[2].split("+") if len(parts) > 2 else []) if r]
        users[token] = User(id=uid, name=uid, roles=roles)
    return users


class Auth:
    def __init__(self, tokens: str, service_token: str):
        self.users = parse_tokens(tokens)
        self.service_token = service_token

    def user_from_token(self, token: str, headers) -> User | None:
        if not token:
            return None
        if self.service_token and hmac.compare_digest(token, self.service_token):
            uid = headers.get("x-agent-user-id")
            if not uid:
                raise HTTPException(400, "сервисный токен требует заголовок X-Agent-User-Id")
            roles = [r.strip() for r in (headers.get("x-agent-roles") or "").split(",") if r.strip()]
            return User(id=uid, name=unquote(headers.get("x-agent-user-name") or uid), roles=roles)
        for t, u in self.users.items():
            if hmac.compare_digest(token, t):
                return u
        return None

    async def __call__(self, request: Request) -> User:
        h = request.headers.get("authorization", "")
        # Токен — только в заголовке: адрес попадает в журналы прокси и историю браузера.
        # Поэтому веб-чат читает SSE через fetch(), а не EventSource (тот не умеет заголовки).
        token = h[7:] if h.lower().startswith("bearer ") else ""
        user = self.user_from_token(token, request.headers)
        if user is None:
            raise HTTPException(401, "нужен токен: Authorization: Bearer <токен>")
        return user


def require_admin(user: User) -> None:
    if not user.has_role("admin"):
        raise HTTPException(403, "нужна роль admin")
