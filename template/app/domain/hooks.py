"""Хуки домена: аудит, доп. проверки, контекст. Ядро вызывает их в нужных точках цикла."""

from __future__ import annotations

import logging

from agentkit import HookEvent, HookResult, Hooks

audit = logging.getLogger("app.audit")
hooks = Hooks()


@hooks.on(HookEvent.POST_TOOL_USE)
async def audit_writes(payload, ctx):
    """Пишет в журнал каждое изменение данных: кто, что, с какими аргументами."""
    tool = ctx.agent.tools.get(payload["tool_name"])
    if tool and not tool.read_only:
        audit.info("user=%s tool=%s args=%s error=%s", ctx.user.id, payload["tool_name"], payload["args"],
                   payload["is_error"])


# Пример: запрет удаления по ночам — так делаются доменные правила, которые
# должны работать независимо от того, что «решила» модель.
#
# @hooks.on(HookEvent.PRE_TOOL_USE, matcher="notes_delete")
# async def no_night_deletes(payload, ctx):
#     from datetime import datetime
#     if datetime.now().hour < 7:
#         return HookResult(decision=Decision.DENY, reason="удаление ночью запрещено")
#
# Пример: не дать закончить без проверки.
#
# @hooks.on(HookEvent.STOP)
# async def verify(payload, ctx):
#     if "готово" in payload["text"].lower() and not checked(...):
#         return HookResult(continue_with="Перед «готово» проверь результат через notes_search.")

__all__ = ["HookResult", "hooks"]
