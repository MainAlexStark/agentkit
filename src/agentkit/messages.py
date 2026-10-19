"""Сообщения диалога в формате OpenAI Chat Completions.

Почему именно этот формат: его понимают OpenRouter, Yandex AI Studio, OpenAI,
vLLM, Ollama — один формат истории позволяет менять провайдера без миграции
базы. Anthropic-специфика (cache_control) добавляется в момент запроса,
в истории её нет.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:24]}"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # Сырые аргументы, если модель прислала невалидный JSON: модели
    # возвращается ошибка разбора, а не молчаливый пустой словарь.
    raw_arguments: str | None = None

    def to_openai(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.raw_arguments
                if self.raw_arguments is not None
                else json.dumps(self.arguments, ensure_ascii=False),
            },
        }

    @classmethod
    def from_openai(cls, d: dict[str, Any]) -> ToolCall:
        fn = d.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            if not isinstance(args, dict):
                raise ValueError("arguments is not an object")
            return cls(id=d.get("id") or new_id("call_"), name=fn.get("name", ""), arguments=args)
        except (ValueError, TypeError):
            return cls(id=d.get("id") or new_id("call_"), name=fn.get("name", ""), arguments={}, raw_arguments=raw)


@dataclass
class Message:
    role: Role
    # str — обычный текст; list — части (text / image_url) для мультимодальных моделей.
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    # Служебное: не уходит в модель. kind: "summary" (результат сжатия),
    # "event" (запись для человека), "hidden" (контекст от хуков) и т. п.
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        return "".join(p.get("text", "") for p in self.content if p.get("type") == "text")

    def to_openai(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role}
        if self.role == "assistant":
            d["content"] = self.content if self.content else None
            if self.tool_calls:
                d["tool_calls"] = [tc.to_openai() for tc in self.tool_calls]
        elif self.role == "tool":
            d["content"] = self.content if self.content is not None else ""
            d["tool_call_id"] = self.tool_call_id
            if self.name:
                d["name"] = self.name
        else:
            d["content"] = self.content if self.content is not None else ""
        return d

    def to_dict(self) -> dict[str, Any]:
        d = self.to_openai()
        if self.meta:
            d["meta"] = self.meta
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        return cls(
            role=d["role"],
            content=d.get("content"),
            tool_calls=[ToolCall.from_openai(tc) for tc in d.get("tool_calls") or []],
            tool_call_id=d.get("tool_call_id"),
            name=d.get("name"),
            meta=d.get("meta") or {},
        )

    @classmethod
    def user(cls, content: str | list[dict[str, Any]], **meta: Any) -> Message:
        return cls(role="user", content=content, meta=meta)

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role="system", content=content)

    @classmethod
    def tool(cls, call_id: str, name: str, content: str, **meta: Any) -> Message:
        return cls(role="tool", content=content, tool_call_id=call_id, name=name, meta=meta)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0  # в валюте провайдера (OpenRouter — USD), если он её сообщает

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_tokens += other.cached_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cost += other.cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost": round(self.cost, 6),
        }
