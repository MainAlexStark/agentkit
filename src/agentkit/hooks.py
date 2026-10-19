"""Хуки — точки, где хост-приложение вмешивается в цикл, не трогая ядро.

  session_start      — добавить контекст в начало сессии
  user_prompt_submit — проверить/дополнить сообщение пользователя (или отклонить)
  pre_tool_use       — разрешить, запретить, спросить человека, поправить аргументы
  post_tool_use      — поправить результат, добавить контекст, записать аудит
  stop               — не дать агенту закончить (например, «сначала прогони тесты»)
  pre_compact        — сохранить важное перед сжатием истории

Хук — async-функция (payload, ctx) → HookResult | None. None — «без мнения».
Несколько хуков на одно событие выполняются по порядку регистрации; первый
deny останавливает цепочку.
"""

from __future__ import annotations

import fnmatch
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agentkit.permissions import Decision


class HookEvent(StrEnum):
    SESSION_START = "session_start"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    STOP = "stop"
    PRE_COMPACT = "pre_compact"


@dataclass
class HookResult:
    decision: Decision | None = None  # для pre_tool_use: allow / ask / deny; для prompt: deny = отклонить
    reason: str = ""
    # Текст, который ядро добавит в контекст модели (скрытым сообщением).
    add_context: str | None = None
    # pre_tool_use: новые аргументы; post_tool_use: новый текст результата.
    updated_args: dict[str, Any] | None = None
    updated_result: str | None = None
    # stop: заставить продолжить, отправив модели это сообщение.
    continue_with: str | None = None


Hook = Callable[[dict[str, Any], Any], Awaitable[HookResult | None] | HookResult | None]


@dataclass
class _Reg:
    event: HookEvent
    fn: Hook
    matcher: str = "*"  # для tool-событий — шаблон имени инструмента


@dataclass
class Hooks:
    _items: list[_Reg] = field(default_factory=list)

    def on(self, event: HookEvent | str, matcher: str = "*") -> Callable[[Hook], Hook]:
        def deco(fn: Hook) -> Hook:
            self._items.append(_Reg(HookEvent(event), fn, matcher))
            return fn

        return deco

    def add(self, event: HookEvent | str, fn: Hook, matcher: str = "*") -> None:
        self._items.append(_Reg(HookEvent(event), fn, matcher))

    def merged(self, other: Hooks | None) -> Hooks:
        return Hooks(self._items + (other._items if other else []))

    async def run(self, event: HookEvent, payload: dict[str, Any], ctx: Any = None) -> list[HookResult]:
        out: list[HookResult] = []
        name = payload.get("tool_name", "")
        for reg in self._items:
            if reg.event != event:
                continue
            if name and not fnmatch.fnmatchcase(name, reg.matcher):
                continue
            res = reg.fn(payload, ctx)
            if inspect.isawaitable(res):
                res = await res
            if res is None:
                continue
            out.append(res)
            if res.decision == Decision.DENY:
                break
            # Следующий хук видит уже исправленные аргументы.
            if res.updated_args is not None:
                payload = {**payload, "args": res.updated_args}
        return out


def fold(results: list[HookResult]) -> HookResult:
    """Сводит ответы нескольких хуков: deny > ask > allow, контекст склеивается."""
    final = HookResult()
    ctx_parts = []
    for r in results:
        if r.decision == Decision.DENY:
            final.decision, final.reason = Decision.DENY, r.reason
        elif r.decision == Decision.ASK and final.decision != Decision.DENY:
            final.decision, final.reason = Decision.ASK, r.reason or final.reason
        elif r.decision == Decision.ALLOW and final.decision is None:
            final.decision = Decision.ALLOW
        if r.add_context:
            ctx_parts.append(r.add_context)
        if r.updated_args is not None:
            final.updated_args = r.updated_args
        if r.updated_result is not None:
            final.updated_result = r.updated_result
        if r.continue_with:
            final.continue_with = r.continue_with
    final.add_context = "\n\n".join(ctx_parts) or None
    return final
