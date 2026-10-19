"""Предметная часть агента: промпт, инструменты, хуки, субагенты, правила.

Всё остальное (цикл, модели, MCP, права, память, API, веб-чат, расписание)
приходит из пакета agentkit.
"""

from __future__ import annotations

from pathlib import Path

from agentkit import DEFAULT_SUBAGENTS, AgentSpec, User
from agentkit.server.service import Domain
from agentkit.server.settings import Settings

from app.domain.hooks import hooks
from app.domain.tools import TOOLS

HERE = Path(__file__).parent


def rules(user: User) -> str:
    """Правила, зависящие от пользователя/компании (можно читать из БД хоста)."""
    if user.has_role("admin"):
        return "Пользователь — администратор: ему можно показывать служебные подробности."
    return ""


def build_domain(settings: Settings) -> Domain:
    return Domain(
        spec=AgentSpec(
            name="main",
            prompt=(HERE / "prompt.md").read_text(encoding="utf-8"),
            model="main",
        ),
        tools=TOOLS,
        hooks=hooks,
        subagents=dict(DEFAULT_SUBAGENTS),
        rules=rules,
        deps={"notes_path": str(settings.data_dir / "notes.json")},
        description="Находит, записывает и приводит в порядок ваши заметки.",
        suggestions=[
            "Что у меня в заметках про воду?",
            "Запиши: позвонить метрологу в пятницу",
            "Сделай еженедельный обзор заметок",
            "Найди похожие заметки и предложи, что объединить",
        ],
    )
