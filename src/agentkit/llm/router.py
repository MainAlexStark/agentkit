"""Маршрутизация моделей: «провайдер:модель», роли, повторы и запасные модели.

Роли развязывают код агента и выбор модели: агент просит «main» или «small»,
а какая это модель — решает конфиг. Сжатие истории, субагент Explore и судья
в evals идут на «small» и стоят в разы дешевле.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace

from agentkit.llm.base import LLMError, LLMProvider, LLMRequest, StreamChunk


def split_model(model_id: str, default_provider: str) -> tuple[str, str]:
    """"openrouter:anthropic/claude-sonnet-4.5" → ("openrouter", "anthropic/claude-sonnet-4.5").

    Двоеточие ищется до первого «/», потому что у Яндекса двоеточий в имени нет,
    а у Ollama бывают теги вида "qwen3:32b" — их нельзя принять за провайдера.
    """
    head = model_id.split("/", 1)[0]
    if ":" in head:
        p, rest = model_id.split(":", 1)
        return p, rest
    return default_provider, model_id


@dataclass
class RetryPolicy:
    attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 30.0


@dataclass
class ModelRouter:
    providers: dict[str, LLMProvider]
    roles: dict[str, str] = field(default_factory=dict)  # "main" → "openrouter:anthropic/claude-sonnet-4.5"
    fallbacks: dict[str, list[str]] = field(default_factory=dict)  # модель → запасные
    default_provider: str = ""
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def __post_init__(self) -> None:
        if not self.default_provider and self.providers:
            self.default_provider = next(iter(self.providers))

    def resolve(self, model_or_role: str | None) -> str:
        m = model_or_role or "main"
        return self.roles.get(m, m)

    def provider_for(self, model_id: str) -> tuple[LLMProvider, str]:
        p, name = split_model(model_id, self.default_provider)
        if p not in self.providers:
            raise LLMError(f"неизвестный провайдер «{p}» в модели «{model_id}»; есть: {', '.join(self.providers)}")
        return self.providers[p], name

    async def stream(
        self,
        req: LLMRequest,
        *,
        on_notice: Callable[[str], Awaitable[None]] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Стрим с повторами и переходом на запасные модели.

        Повтор возможен, только пока модель не начала отвечать: если часть
        текста уже ушла в UI, повтор задублировал бы его. После первого куска
        ошибка пробрасывается как есть.
        """
        model_id = self.resolve(req.model)
        chain = [model_id, *self.fallbacks.get(model_id, [])]
        last: Exception | None = None
        for i, mid in enumerate(chain):
            if i > 0 and on_notice:
                await on_notice(f"Переключаюсь на запасную модель {mid}: {last}")
            provider, name = self.provider_for(mid)
            for attempt in range(self.retry.attempts):
                started = False
                try:
                    async for ch in provider.stream(replace(req, model=name)):
                        started = True
                        if ch.kind == "done" and ch.response is not None and not ch.response.model:
                            ch.response.model = mid
                        yield ch
                    return
                except LLMError as e:
                    last = e
                    if started or not e.retryable:
                        if started:
                            raise
                        break  # неповторяемая — сразу к запасной модели
                    delay = min(self.retry.max_delay, self.retry.base_delay * 2**attempt)
                    delay *= 0.5 + random.random() / 2
                    if on_notice:
                        await on_notice(f"Модель {mid} ответила ошибкой ({e}); повтор через {delay:.0f} с")
                    await self.sleep(delay)
        raise last if last else LLMError("нет доступных моделей")
