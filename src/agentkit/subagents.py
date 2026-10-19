"""Субагенты: изолированный контекст под подзадачу.

Главный агент вызывает task(agent_type, prompt). Субагент работает в
собственной дочерней сессии со своим промптом и набором инструментов,
а в контекст главного возвращается только его итоговый ответ. Так поиск по
сотне файлов не забивает контекст главного агента, а несколько субагентов
(вызовы task в одном ответе) работают параллельно.
"""

from __future__ import annotations

from typing import Annotated

from agentkit.agent import AgentSpec
from agentkit.events import Event, EventType
from agentkit.permissions import Mode
from agentkit.tools.base import Tool, ToolContext, ToolResult, tool

EXPLORE = AgentSpec(
    name="explore",
    description="Быстрый поиск и чтение: найти, где что лежит, собрать факты. Только чтение. "
                "Укажи в задаче, что найти и в каком виде вернуть.",
    prompt="Ты — субагент-исследователь. Найди то, что просят, и верни сжатый отчёт: факты, где они нашлись "
           "(файл/адрес/id), и чего найти не удалось. Не пересказывай лишнее — главный агент видит только твой отчёт.",
    model="small",
    mode=Mode.READONLY,
    max_steps=20,
)

PLAN = AgentSpec(
    name="plan",
    description="Продумать план реализации: шаги, затронутые места, риски. Только чтение.",
    prompt="Ты — субагент-архитектор. Изучи задачу и верни план по шагам: что менять, где, в каком порядке, "
           "какие риски и как проверить результат. Ничего не меняй.",
    model="main",
    mode=Mode.READONLY,
    max_steps=25,
)

GENERAL = AgentSpec(
    name="general",
    description="Самостоятельная многошаговая подзадача с теми же инструментами, что у главного агента "
                "(кроме подтверждаемых действий — их субагент вернёт тебе).",
    prompt="Ты — субагент. Выполни подзадачу целиком и верни итог: что сделано, результаты, что не удалось.",
    model="main",
    max_steps=30,
)

DEFAULT_SUBAGENTS = {s.name: s for s in (EXPLORE, PLAN, GENERAL)}


def task_tool(specs: dict[str, AgentSpec]) -> Tool:
    names = list(specs)

    @tool(title="Субагент", read_only=True, concurrency_safe=True, timeout=1800, group="core", source="builtin")
    async def task(
        ctx: ToolContext,
        agent_type: Annotated[str, "Тип субагента из описания инструмента"],
        description: Annotated[str, "Короткое название подзадачи (3–6 слов)"],
        prompt: Annotated[str, "Полная постановка: субагент не видит этот разговор, дай ему весь нужный контекст"],
    ) -> ToolResult:
        """Запускает субагента с отдельным контекстом и возвращает его итог.
        Несколько вызовов task в одном ответе работают параллельно."""
        spec = specs.get(agent_type)
        if spec is None:
            return ToolResult.error(f"нет субагента «{agent_type}». Есть: {', '.join(names)}")
        parent = await ctx.store.get_session(ctx.session_id)
        child_session = await ctx.store.create_session(
            ctx.user.id, title=description, agent=spec.name, parent_id=ctx.session_id,
            state={"model": (parent.state or {}).get("model")} if parent and spec.model == "main" else {},
        )
        child = ctx.agent.child(spec)
        await ctx.emit(Event(EventType.SUBAGENT_STARTED, {"session_id": child_session.id, "type": spec.name,
                                                          "description": description, "tool_call_id": ctx.tool_call_id}))
        text = ""
        status = "done"
        # Режим прав наследуется от родителя (план остаётся планом), если у типа нет своего.
        mode = spec.mode or (parent.state or {}).get("mode") if parent else spec.mode
        async for ev in child.run(child_session.id, ctx.user, prompt, model=spec.model, mode=mode):
            # Поток субагента пробрасывается в UI главного, помеченный его id.
            if ev.type in (EventType.TOOL_CALL, EventType.TOOL_RESULT, EventType.TODO, EventType.NOTICE,
                           EventType.ERROR, EventType.USAGE):
                await ctx.emit(Event(ev.type, ev.data, agent=child_session.id))
            if ev.type == EventType.MESSAGE and ev.data.get("content"):
                text = ev.data["content"]
            if ev.type == EventType.RUN_FINISHED:
                status = ev.data["status"]
        await ctx.emit(Event(EventType.SUBAGENT_FINISHED, {"session_id": child_session.id, "status": status,
                                                           "tool_call_id": ctx.tool_call_id}))
        if not text:
            return ToolResult.error(f"субагент завершился без ответа (статус {status})")
        return ToolResult(content=text, data={"session_id": child_session.id, "status": status})

    task.description += "\n\nТипы субагентов:\n" + "\n".join(f"- {s.name}: {s.description}" for s in specs.values())
    return task
