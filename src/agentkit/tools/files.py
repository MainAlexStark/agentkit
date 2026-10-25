"""Файлы рабочей папки сессии: read / write / edit / glob / grep.

Всё строго внутри ctx.workspace — путь за её пределами отвергается до
обращения к диску. Перед каждой записью старое содержимое сохраняется в
чекпоинт, поэтому Agent.rewind() откатывает и диалог, и файлы.

edit_file — замена точной уникальной строки, а не «перепиши файл»: модель
не может случайно потерять остальное содержимое, а diff получается
минимальным и читаемым для человека.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from agentkit.tools.base import Tool, ToolContext, ToolDenied, ToolResult, tool

MAX_READ_LINES = 2000


def resolve(ctx: ToolContext, path: str) -> Path:
    if ctx.workspace is None:
        raise ToolDenied("у этого агента нет рабочей папки")
    root = ctx.workspace.resolve()
    p = (root / path.lstrip("/")).resolve()
    if p != root and root not in p.parents:
        raise ToolDenied(f"путь {path} вне рабочей папки")
    return p


def rel(ctx: ToolContext, p: Path) -> str:
    return str(p.relative_to(ctx.workspace.resolve()))  # type: ignore[union-attr]


async def checkpoint(ctx: ToolContext, p: Path) -> None:
    existed = p.exists()
    content = p.read_text(encoding="utf-8", errors="replace") if existed else ""
    await ctx.store.blob_put(ctx.session_id, "file_checkpoint", content,
                             meta={"path": rel(ctx, p), "existed": existed, "message_seq": ctx.message_seq})


@tool(title="Чтение файла", read_only=True, group="files", source="builtin")
async def read_file(
    ctx: ToolContext,
    path: Annotated[str, "Путь от корня рабочей папки"],
    offset: Annotated[int, "С какой строки (с 1)"] = 1,
    limit: Annotated[int, "Сколько строк"] = MAX_READ_LINES,
) -> ToolResult:
    """Читает текстовый файл с номерами строк. Для больших файлов читай по частям через offset/limit."""
    p = resolve(ctx, path)
    if not p.is_file():
        return ToolResult.error(f"нет файла {path}")
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(offset, 1) - 1
    chunk = lines[start: start + limit]
    body = "\n".join(f"{i + start + 1:>6}\t{line}" for i, line in enumerate(chunk))
    more = len(lines) - start - len(chunk)
    if more > 0:
        body += f"\n… ещё {more} строк (offset={start + len(chunk) + 1})"
    return ToolResult(content=body or "(пустой файл)")


@tool(title="Запись файла", group="files", source="builtin", describe=lambda a: [["Файл", a["path"]], ["Размер", f"{len(a['content'])} символов"]])
async def write_file(
    ctx: ToolContext,
    path: Annotated[str, "Путь от корня рабочей папки"],
    content: Annotated[str, "Полное содержимое файла"],
) -> ToolResult:
    """Создаёт или полностью перезаписывает файл. Для правки существующего — edit_file."""
    p = resolve(ctx, path)
    await checkpoint(ctx, p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    # Карточка файла в чате (и предпросмотр сбоку): написанный файл — обычно результат работы.
    r = rel(ctx, p)
    return ToolResult(content=f"Записан {path} ({len(content)} символов).",
                      ui=[{"type": "files", "files": [{"path": r, "name": p.name, "size": p.stat().st_size}]}])


@tool(title="Правка файла", group="files", source="builtin",
      describe=lambda a: [["Файл", a["path"]], ["Было", a["old_string"][:400]], ["Станет", a["new_string"][:400]]])
async def edit_file(
    ctx: ToolContext,
    path: Annotated[str, "Путь от корня рабочей папки"],
    old_string: Annotated[str, "Точный фрагмент, который заменить (с отступами); должен встречаться один раз"],
    new_string: Annotated[str, "Чем заменить"],
    replace_all: Annotated[bool, "Заменить все вхождения"] = False,
) -> ToolResult:
    """Заменяет точный фрагмент текста в файле. Сначала прочитай файл, затем копируй old_string дословно."""
    p = resolve(ctx, path)
    if not p.is_file():
        return ToolResult.error(f"нет файла {path}")
    text = p.read_text(encoding="utf-8", errors="replace")
    n = text.count(old_string)
    if n == 0:
        return ToolResult.error("old_string не найден. Перечитай файл и скопируй фрагмент дословно, с отступами.")
    if n > 1 and not replace_all:
        return ToolResult.error(f"old_string встречается {n} раз: расширь фрагмент до уникального или replace_all=true")
    await checkpoint(ctx, p)
    p.write_text(text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1),
                 encoding="utf-8")
    return ToolResult(content=f"Изменён {path} ({n if replace_all else 1} замен).")


@tool(title="Список файлов", read_only=True, group="files", source="builtin")
async def glob_files(
    ctx: ToolContext,
    pattern: Annotated[str, "Шаблон, например **/*.py или docs/*.md"] = "**/*",
) -> ToolResult:
    """Список файлов рабочей папки по шаблону (новые — первыми)."""
    root = resolve(ctx, ".")
    files = [p for p in root.glob(pattern) if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    names = [rel(ctx, p) for p in files[:500]]
    tail = f"\n… и ещё {len(files) - 500}" if len(files) > 500 else ""
    return ToolResult(content="\n".join(names) + tail if names else "Ничего не найдено.")


@tool(title="Поиск по файлам", read_only=True, group="files", source="builtin")
async def grep(
    ctx: ToolContext,
    pattern: Annotated[str, "Регулярное выражение (Python re)"],
    glob: Annotated[str, "Ограничить файлами по шаблону"] = "**/*",
    ignore_case: Annotated[bool, "Без учёта регистра"] = False,
    max_results: Annotated[int, "Максимум строк в ответе"] = 200,
) -> ToolResult:
    """Ищет строки по регулярному выражению в файлах рабочей папки. Ответ: файл:строка: текст."""
    root = resolve(ctx, ".")
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return ToolResult.error(f"неверное выражение: {e}")
    out: list[str] = []
    for p in sorted(root.glob(glob)):
        if not p.is_file() or p.stat().st_size > 5_000_000:
            continue
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{rel(ctx, p)}:{i}: {line[:300]}")
                    if len(out) >= max_results:
                        return ToolResult(content="\n".join(out) + "\n… (лимит; уточни запрос)")
        except UnicodeDecodeError:
            continue
    return ToolResult(content="\n".join(out) or "Совпадений нет.")


def file_tools(read_only: bool = False) -> list[Tool]:
    ro = [read_file, glob_files, grep]
    return ro if read_only else [*ro, write_file, edit_file]

