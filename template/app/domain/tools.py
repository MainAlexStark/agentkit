"""Доменные инструменты агента. ЗДЕСЬ — основная работа нового проекта.

Пример — заметки в JSON-файле. Замените на вызовы своей системы (БД, API,
MCP-клиент своего сервера). Правила хорошего инструмента:

* docstring пишется для модели: что делает и КОГДА пригодится;
* описания аргументов — в Annotated[тип, "описание"];
* читающий инструмент — read_only=True (вызывается без вопросов, параллельно);
* пишущий — без read_only: в режиме default пойдёт через карточку подтверждения;
  describe= задаёт, как карточка покажет действие человеку;
* опасное (удаление, деньги, отправка наружу) — destructive=True или
  needs_approval=функция(args, ctx) → причина, если подтверждать надо даже в auto;
* права пользователя проверяются ВНУТРИ инструмента (ctx.user.roles) —
  промпт не граница безопасности;
* ошибка — исключение или ToolResult.error("что не так и как исправить").
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Annotated

from agentkit import ToolContext, ToolDenied, ToolResult, tool


def _db(ctx: ToolContext) -> Path:
    return Path(ctx.deps["notes_path"])


def _load(ctx: ToolContext) -> list[dict]:
    p = _db(ctx)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _save(ctx: ToolContext, notes: list[dict]) -> None:
    p = _db(ctx)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(notes, ensure_ascii=False, indent=1), encoding="utf-8")


@tool(read_only=True)
async def notes_search(
    ctx: ToolContext,
    query: Annotated[str, "Слова для поиска; пусто — все заметки"] = "",
    limit: Annotated[int, "Сколько вернуть"] = 20,
) -> ToolResult:
    """Ищет заметки пользователя. Используй перед ответом на вопросы о том, что пользователь записывал."""
    q = query.lower()
    found = [n for n in _load(ctx) if n["owner"] == ctx.user.id and (not q or q in n["text"].lower())][:limit]
    if not found:
        return ToolResult(content="Заметок не найдено.")
    rows = [[n["id"], time.strftime("%d.%m.%Y", time.localtime(n["ts"])), n["text"]] for n in found]
    return ToolResult(
        content="\n".join(f"#{r[0]} ({r[1]}): {r[2]}" for r in rows),
        data=found,
        ui=[{"type": "table", "columns": ["#", "Дата", "Текст"], "rows": rows}],
    )


@tool(describe=lambda a: [["Новая заметка", a["text"]]])
async def notes_add(
    ctx: ToolContext,
    text: Annotated[str, "Текст заметки"],
) -> str:
    """Создаёт заметку."""
    notes = _load(ctx)
    nid = max((n["id"] for n in notes), default=0) + 1
    notes.append({"id": nid, "owner": ctx.user.id, "text": text, "ts": time.time()})
    _save(ctx, notes)
    return f"Заметка #{nid} создана."


@tool(destructive=True, describe=lambda a: [["Удалить заметку", f"#{a['id']}"]])
async def notes_delete(
    ctx: ToolContext,
    id: Annotated[int, "Номер заметки"],
) -> str:
    """Удаляет заметку по номеру."""
    notes = _load(ctx)
    note = next((n for n in notes if n["id"] == id), None)
    if note is None:
        return f"Заметки #{id} нет."
    if note["owner"] != ctx.user.id and not ctx.user.has_role("admin"):
        raise ToolDenied("это чужая заметка")
    _save(ctx, [n for n in notes if n["id"] != id])
    return f"Заметка #{id} удалена."


TOOLS = [notes_search, notes_add, notes_delete]
