"""Контракт LLM-провайдера.

Провайдер умеет одно: принять запрос и отдать поток кусков, последний из
которых — собранный ответ. Всё остальное (повторы, fallback, учёт стоимости)
живёт уровнем выше, в ModelRouter, чтобы не повторять это в каждом провайдере.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentkit.messages import Message, Usage


@dataclass
class LLMRequest:
    model: str
    messages: list[Message]
    tools: list[dict[str, Any]] = field(default_factory=list)  # OpenAI function schemas
    max_tokens: int | None = None
    temperature: float | None = None
    # low / medium / high — провайдер переводит в свой параметр (reasoning.effort у OpenRouter)
    reasoning_effort: str | None = None
    # {"type": "json_schema", "json_schema": {...}} — для structured output
    response_format: dict[str, Any] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    message: Message
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    model: str | None = None  # фактическая модель (после fallback)


@dataclass
class StreamChunk:
    kind: str  # "text" | "thinking" | "done"
    text: str = ""
    response: LLMResponse | None = None


class LLMError(Exception):
    """Ошибка провайдера. retryable — стоит ли повторять (429, 5xx, обрыв связи)."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class LLMProvider(Protocol):
    name: str

    def stream(self, req: LLMRequest) -> AsyncIterator[StreamChunk]: ...


async def complete(provider: LLMProvider, req: LLMRequest) -> LLMResponse:
    """Нестриминговый вызов поверх stream — для сжатия, судьи в evals и т. п."""
    resp: LLMResponse | None = None
    async for ch in provider.stream(req):
        if ch.kind == "done":
            resp = ch.response
    if resp is None:
        raise LLMError("провайдер не вернул ответ")
    return resp


@dataclass
class ModelInfo:
    id: str  # "провайдер:модель"
    name: str = ""
    context_window: int = 128_000
    vision: bool = False
    tools: bool = True
    price_in: float | None = None  # $ за 1M токенов
    price_out: float | None = None
    provider: str = ""  # «openrouter», «yandex»…
    vendor: str = ""  # чья модель: «anthropic», «openai»… (у OpenRouter — до «/»)
    description: str = ""
    created: float = 0.0  # когда появилась в каталоге (для сортировки «новые»)
    reasoning: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)
