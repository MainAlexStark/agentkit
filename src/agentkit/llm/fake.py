"""Сценарная модель для тестов и офлайн-evals.

Отвечает заранее заданными ответами по очереди. Ответ — строка (просто текст)
или dict {"text": …, "tool_calls": [(имя, аргументы), …]}, или функция от
запроса — для ответов, зависящих от истории.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from agentkit.llm.base import LLMError, LLMRequest, LLMResponse, StreamChunk
from agentkit.messages import Message, ToolCall, Usage, new_id

Script = str | dict[str, Any] | Callable[[LLMRequest], "str | dict[str, Any]"]


class FakeLLM:
    def __init__(self, script: list[Script] | None = None, name: str = "fake", default: str | None = "Готово."):
        self.name = name
        self.script = list(script or [])
        self.default = default
        self.requests: list[LLMRequest] = []

    def push(self, *items: Script) -> None:
        self.script.extend(items)

    async def stream(self, req: LLMRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(req)
        if self.script:
            item = self.script.pop(0)
        elif self.default is not None:
            item = self.default
        else:
            raise LLMError("FakeLLM: сценарий закончился")
        if callable(item):
            item = item(req)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            item = {"text": item}
        text = item.get("text") or ""
        for i in range(0, len(text), 16):  # имитация стрима
            yield StreamChunk("text", text=text[i : i + 16])
        calls = [
            ToolCall(id=new_id("call_"), name=n, arguments=a) for n, a in item.get("tool_calls", [])
        ]
        usage = Usage(input_tokens=sum(len(m.text) for m in req.messages) // 4, output_tokens=len(text) // 4)
        yield StreamChunk(
            "done",
            response=LLMResponse(
                message=Message(role="assistant", content=text or None, tool_calls=calls),
                usage=usage,
                finish_reason="tool_calls" if calls else "stop",
                model=req.model,
            ),
        )
