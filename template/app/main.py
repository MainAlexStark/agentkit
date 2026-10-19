"""Точка входа сервиса: uvicorn app.main:app

Своя логика HTTP (вебхуки, интеграции) — в extra_routers, предметная часть — в app/domain.
"""

from __future__ import annotations

import logging

from agentkit.server.app import build_app
from agentkit.server.service import AgentService
from agentkit.server.settings import Settings

from app.domain import build_domain

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = Settings()
service = AgentService(settings, build_domain(settings))
app = build_app(service)
