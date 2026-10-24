"""Базовые инструменты любого агента: задачи, вопрос человеку, длинный вывод, память."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from agentkit.events import Event, EventType
from agentkit.tools.base import Tool, ToolContext, ToolResult, tool


class TodoItem(BaseModel):
    content: str = Field(description="Что сделать, в повелительном наклонении")
    status: Literal["pending", "in_progress", "completed"] = "pending"


@tool(title="План задач", read_only=True, concurrency_safe=False, group="core", source="builtin")
async def todo_write(
    ctx: ToolContext,
    todos: Annotated[list[TodoItem], "Полный список задач (заменяет прежний)"],
) -> ToolResult:
    """Ведёт список задач текущей работы; человек видит его рядом с чатом.
    Используй для задач из 3+ шагов: заведи список в начале, держи ровно одну задачу in_progress,
    отмечай completed сразу по завершении. Каждый вызов передаёт список целиком."""
    items = [t if isinstance(t, dict) else t.model_dump() for t in todos]
    state = await ctx.store.get_state(ctx.session_id)
    state["todos"] = items
    await ctx.store.set_state(ctx.session_id, state)
    await ctx.emit(Event(EventType.TODO, {"todos": items}, agent=ctx.agent_name if ctx.agent_name != "main" else None))
    done = sum(1 for t in items if t["status"] == "completed")
    return ToolResult(content=f"Список обновлён: {done}/{len(items)} выполнено.", data=items)


class Choice(BaseModel):
    label: str = Field(description="Короткий вариант ответа (1–5 слов)")
    description: str = Field("", description="Что будет, если выбрать этот вариант")


@tool(title="Вопрос", read_only=True, concurrency_safe=False, group="core", source="builtin")
async def ask_user(
    ctx: ToolContext,
    question: Annotated[str, "Вопрос целиком, заканчивается знаком вопроса"],
    options: Annotated[list[Choice], "2–4 варианта; рекомендуемый — первым с пометкой (рекомендую)"] = [],  # noqa: B006
    multi: Annotated[bool, "Можно выбрать несколько вариантов"] = False,
) -> ToolResult:
    """Задаёт человеку вопрос с вариантами и останавливает работу до ответа.
    Только когда решение действительно за человеком и его не вывести из данных и разумных умолчаний.
    Человек всегда может ответить своим текстом."""
    opts = [o if isinstance(o, dict) else o.model_dump() for o in options]
    await ctx.emit(Event(EventType.ASK_USER, {"question": question, "options": opts, "multi": multi,
                                              "tool_call_id": ctx.tool_call_id}))
    return ToolResult(content="Вопрос показан человеку. Ответ придёт следующим сообщением.",
                      data={"question": question, "options": opts}, stop="waiting_input",
                      ui=[{"type": "choices", "question": question, "options": opts, "multi": multi}])


@tool(title="Чтение вывода", read_only=True, group="core", source="builtin")
async def read_output(
    ctx: ToolContext,
    id: Annotated[str, "id из пометки «Вывод обрезан»"],
    offset: Annotated[int, "С какого символа читать"] = 0,
    limit: Annotated[int, "Сколько символов"] = 20_000,
) -> ToolResult:
    """Читает полный текст обрезанного вывода инструмента по частям."""
    got = await ctx.store.blob_get(id)
    if got is None:
        return ToolResult.error("нет такого вывода")
    text = got[0]
    part = text[offset: offset + limit]
    more = len(text) - offset - len(part)
    tail = f"\n\n[ещё {more} символов: offset={offset + len(part)}]" if more > 0 else "\n\n[конец]"
    return ToolResult(content=part + tail)


def memory_tools(*, global_roles: tuple[str, ...] = ("admin",)) -> list[Tool]:
    """memory_save / memory_delete. Общая память — только ролям global_roles, иначе через подтверждение."""

    def needs_ok(args, ctx) -> str | None:
        if args.get("scope") == "global" and ctx is not None and not ctx.user.has_role(*global_roles):
            return "Общая память влияет на всех пользователей"
        return None

    # read_only=True: память агента — не данные пользователя; без этого каждое
    # «запомни» требовало бы карточку. Общая память страхуется needs_approval.
    @tool(title="Запомнить", needs_approval=needs_ok, read_only=True, concurrency_safe=False, group="memory", source="builtin",
          describe=lambda a: [["Запомнить", a.get("text", "")],
                              ["Для кого", {"global": "всех", "project": "проекта"}.get(a.get("scope", ""), "меня")]])
    async def memory_save(
        ctx: ToolContext,
        text: Annotated[str, "Что запомнить: одно правило или факт, коротко и самодостаточно"],
        scope: Annotated[Literal["user", "global", "project"],
                         "user — только для этого человека, project — для всех разговоров проекта, global — для всех"] = "user",
    ) -> ToolResult:
        """Сохраняет правило или факт в долговременную память; он попадёт в контекст всех следующих разговоров.
        Используй, когда человек просит запомнить, или явно задаёт постоянное правило («всегда…», «никогда…»).
        Факты о работе проекта (в разговоре проекта) — scope=project. Не сохраняй разовые детали текущей задачи."""
        if scope == "project":
            from agentkit.projects import session_project_id  # noqa: PLC0415 — проекты подключаются не всегда

            pid = await session_project_id(ctx.store, ctx.session_id)
            if not pid:
                return ToolResult.error("этот разговор не в проекте — сохрани как user")
            _, role = await ctx.store.project_role(pid, ctx.user.id)
            if role not in ("owner", "editor"):
                return ToolResult.error("память проекта меняют только его редакторы — сохрани как user")
            sc = f"project:{pid}"
        else:
            sc = "global" if scope == "global" else f"user:{ctx.user.id}"
        existing = await ctx.store.memory_list([sc])
        if any(m.text.strip().lower() == text.strip().lower() for m in existing):
            return ToolResult(content="Уже есть в памяти.")
        rec = await ctx.store.memory_add(sc, text.strip(), created_by=ctx.user.id)
        return ToolResult(content=f"Запомнил [{rec.id}].", data={"id": rec.id})

    @tool(title="Забыть", group="memory", source="builtin", read_only=True, concurrency_safe=False)
    async def memory_delete(
        ctx: ToolContext,
        id: Annotated[str, "id записи из раздела «Память» системного промпта"],
    ) -> ToolResult:
        """Удаляет запись из памяти (когда человек просит забыть или правило устарело)."""
        scopes = [f"user:{ctx.user.id}"]
        if hasattr(ctx.store, "project_role"):
            from agentkit.projects import session_project_id  # noqa: PLC0415

            pid = await session_project_id(ctx.store, ctx.session_id)
            if pid and (await ctx.store.project_role(pid, ctx.user.id))[1] in ("owner", "editor"):
                scopes.append(f"project:{pid}")
        mine = await ctx.store.memory_list(scopes)
        if not any(m.id == id for m in mine) and not ctx.user.has_role(*global_roles):
            return ToolResult.error("можно удалять только свои записи и записи своего проекта")
        ok = await ctx.store.memory_delete(id)
        return ToolResult(content="Удалено." if ok else "Такой записи нет.")

    return [memory_save, memory_delete]


def core_tools() -> list[Tool]:
    return [todo_write, ask_user, read_output]
