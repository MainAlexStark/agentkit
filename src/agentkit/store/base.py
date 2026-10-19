"""Хранилище агента: сессии, сообщения, прогоны, события, подтверждения, память.

Один интерфейс, одна SQL-реализация (SQLite для разработки и тестов,
PostgreSQL в проде). Хост-приложение может подставить свою реализацию —
например, на Django ORM в EI_doc — и ядро этого не заметит.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentkit.events import Event
from agentkit.messages import Message


@dataclass
class SessionRec:
    id: str
    user_id: str
    title: str = ""
    agent: str = "main"
    parent_id: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class RunRec:
    id: str
    session_id: str
    status: str
    model: str = ""
    steps: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at: float = 0.0
    finished_at: float | None = None
    trigger: str = "user"  # user | approval | schedule | api


@dataclass
class ApprovalRec:
    id: str
    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    args: dict[str, Any]
    view: list[list[str]] = field(default_factory=list)
    reason: str = ""
    status: str = "pending"  # pending | approved | rejected | expired
    decided_by: str | None = None
    decided_at: float | None = None
    comment: str = ""
    created_at: float = 0.0


@dataclass
class MemoryRec:
    id: str
    scope: str  # "global" | "user:<id>" | любая другая область (магазин, объект)
    text: str
    created_by: str = ""
    created_at: float = 0.0


@dataclass
class ScheduleRec:
    id: str
    name: str
    cron: str
    prompt: str
    user_id: str
    agent: str = "main"
    enabled: bool = True
    session_id: str | None = None  # None — каждый запуск в новой сессии
    last_run_at: float | None = None
    next_run_at: float | None = None


class Store:
    """Абстрактный интерфейс; реализация — agentkit.store.sql.SQLStore."""

    # сессии
    async def create_session(self, user_id: str, *, title: str = "", agent: str = "main",
                             parent_id: str | None = None, state: dict | None = None) -> SessionRec: ...
    async def get_session(self, session_id: str) -> SessionRec | None: ...
    async def list_sessions(self, user_id: str, *, limit: int = 50, include_children: bool = False) -> list[SessionRec]: ...
    async def update_session(self, session_id: str, **fields: Any) -> None: ...
    async def delete_session(self, session_id: str) -> None: ...
    async def get_state(self, session_id: str) -> dict[str, Any]: ...
    async def set_state(self, session_id: str, state: dict[str, Any]) -> None: ...

    # сообщения
    async def append_message(self, session_id: str, msg: Message, run_id: str | None = None) -> int: ...
    async def list_messages(self, session_id: str, *, after_seq: int = 0) -> list[tuple[int, Message]]: ...
    async def delete_messages_after(self, session_id: str, seq: int) -> int: ...

    # прогоны и события
    async def create_run(self, session_id: str, *, model: str = "", trigger: str = "user") -> RunRec: ...
    async def get_run(self, run_id: str) -> RunRec | None: ...
    async def update_run(self, run_id: str, **fields: Any) -> None: ...
    async def list_runs(self, session_id: str, *, limit: int = 50) -> list[RunRec]: ...
    async def list_running_runs(self) -> list[RunRec]: ...
    async def fail_runs(self, run_ids: list[str], reason: str) -> int: ...
    async def add_event(self, ev: Event) -> int: ...
    async def last_event_seq(self, session_id: str) -> int: ...
    async def list_events(self, *, run_id: str | None = None, session_id: str | None = None,
                          after_seq: int = 0, limit: int = 1000) -> list[Event]: ...

    # подтверждения
    async def create_approval(self, rec: ApprovalRec) -> ApprovalRec: ...
    async def get_approval(self, approval_id: str) -> ApprovalRec | None: ...
    async def list_approvals(self, session_id: str, *, status: str | None = None,
                             run_id: str | None = None) -> list[ApprovalRec]: ...
    async def update_approval(self, approval_id: str, **fields: Any) -> None: ...
    async def claim_approval(self, approval_id: str) -> bool: ...

    # входящие, пока агент работает
    async def inbox_push(self, session_id: str, content: Any, user: dict[str, Any]) -> int: ...
    async def inbox_pop(self, session_id: str) -> list[dict[str, Any]]: ...
    async def inbox_count(self, session_id: str) -> int: ...

    # служебное ключ-значение (OAuth и т. п.)
    async def kv_get(self, ns: str, key: str) -> Any: ...
    async def kv_set(self, ns: str, key: str, value: Any) -> None: ...
    async def kv_delete(self, ns: str, key: str) -> None: ...

    # память
    async def memory_add(self, scope: str, text: str, *, created_by: str = "") -> MemoryRec: ...
    async def memory_list(self, scopes: list[str]) -> list[MemoryRec]: ...
    async def memory_delete(self, memory_id: str) -> bool: ...
    async def memory_update(self, memory_id: str, text: str) -> bool: ...

    # большие данные: полный вывод инструментов, чекпоинты файлов
    async def blob_put(self, session_id: str, kind: str, content: str, *, meta: dict | None = None) -> str: ...
    async def blob_get(self, blob_id: str) -> tuple[str, dict[str, Any]] | None: ...
    async def blob_list(self, session_id: str, kind: str) -> list[tuple[str, dict[str, Any]]]: ...

    # расписание
    async def schedule_upsert(self, rec: ScheduleRec) -> ScheduleRec: ...
    async def claim_schedule(self, schedule_id: str, expected_next: float | None, new_next: float,
                             now: float) -> bool: ...
    async def schedule_list(self, *, user_id: str | None = None) -> list[ScheduleRec]: ...
    async def schedule_delete(self, schedule_id: str) -> None: ...

    async def usage_summary(self, *, user_id: str | None = None, since: float = 0) -> dict[str, Any]: ...
