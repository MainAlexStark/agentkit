"""События прогона — единый поток для UI, трейсов, SSE и тестов.

Всё, что агент делает, превращается в событие. UI рисует по ним чат, трейсер
пишет их в журнал, evals проверяют по ним поведение. Новое событие = новая
строка в EventType, без изменения транспорта.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    USER_MESSAGE = "user_message"  # сообщение, присланное во время работы, принято агентом
    TEXT_DELTA = "text_delta"  # кусок текста ответа (стриминг)
    THINKING_DELTA = "thinking_delta"  # кусок рассуждений, если модель их отдаёт
    MESSAGE = "message"  # готовое сообщение assistant (после стрима)
    TOOL_CALL = "tool_call"  # модель вызвала инструмент
    TOOL_RESULT = "tool_result"  # результат инструмента
    APPROVAL_REQUIRED = "approval_required"  # нужно подтверждение человека
    APPROVAL_RESOLVED = "approval_resolved"
    ASK_USER = "ask_user"  # агент задал вопрос с вариантами
    TODO = "todo"  # обновился список задач
    SUBAGENT_STARTED = "subagent_started"
    SUBAGENT_FINISHED = "subagent_finished"
    COMPACTED = "compacted"  # история сжата
    USAGE = "usage"  # расход токенов за шаг
    NOTICE = "notice"  # служебное сообщение для человека (ретрай, fallback модели)
    ERROR = "error"
    RUN_FINISHED = "run_finished"


class RunStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    WAITING_APPROVAL = "waiting_approval"  # прогон остановлен до решения человека
    WAITING_INPUT = "waiting_input"  # агент задал вопрос (ask_user)
    CANCELLED = "cancelled"
    ERROR = "error"
    MAX_STEPS = "max_steps"


@dataclass
class Event:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    session_id: str | None = None
    # Субагент: id дочерней сессии, чтобы UI мог свернуть его поток.
    agent: str | None = None
    ts: float = field(default_factory=time.time)
    seq: int = 0  # проставляет хранилище; курсор для SSE / опроса

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "type": str(self.type),
            "data": self.data,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "agent": self.agent,
            "ts": self.ts,
        }
