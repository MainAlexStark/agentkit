"""Реестр инструментов и отложенная загрузка (tool_search).

Когда инструментов сотни (MCP-сервер Ozon — 100+), их схемы съедают десятки
тысяч токенов в каждом запросе и сбивают модель. Отложенные инструменты
видны в промпте только по имени; схему модель получает через tool_search,
после чего инструмент становится доступен до конца сессии.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable
from typing import Annotated

from agentkit.tools.base import Tool, ToolContext, ToolResult, tool


def match_any(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()):
        self._tools: dict[str, Tool] = {}
        for t in tools:
            self.add(t)

    def add(self, t: Tool) -> Tool:
        if not re.fullmatch(r"[a-zA-Z0-9_\-]{1,64}", t.name):
            # OpenAI и Anthropic отвергают весь запрос из-за одного кривого имени.
            raise ValueError(f"недопустимое имя инструмента «{t.name}»: только [a-zA-Z0-9_-], до 64 символов")
        self._tools[t.name] = t
        return t

    def extend(self, tools: Iterable[Tool]) -> None:
        for t in tools:
            self.add(t)

    def remove(self, name: str) -> None:
        self._tools.pop(name, None)

    def remove_group(self, group: str) -> None:
        for n in [n for n, t in self._tools.items() if t.group == group]:
            del self._tools[n]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def filtered(self, allow: Iterable[str] | None = None, deny: Iterable[str] = ()) -> ToolRegistry:
        """Подмножество для субагента: allow=["read_file", "grep", "mcp__rst__*"]."""
        allow = list(allow) if allow is not None else None
        deny = list(deny)
        return ToolRegistry(
            t
            for t in self._tools.values()
            if (allow is None or match_any(t.name, allow)) and not match_any(t.name, deny)
        )

    def visible(self, loaded: set[str]) -> list[Tool]:
        return [t for t in self._tools.values() if not t.deferred or t.name in loaded]

    def deferred_names(self, loaded: set[str]) -> list[str]:
        return [t.name for t in self._tools.values() if t.deferred and t.name not in loaded]

    def search(self, query: str, limit: int = 8) -> list[Tool]:
        """select:a,b — точные имена; иначе — ранжирование по словам в имени и описании."""
        q = query.strip()
        if q.startswith("select:"):
            names = [n.strip() for n in q[7:].split(",") if n.strip()]
            return [self._tools[n] for n in names if n in self._tools]
        words = [w for w in re.split(r"[\s,]+", q.lower()) if w]
        scored = []
        for t in self._tools.values():
            name = t.name.lower()
            desc = t.description.lower()
            s = sum(3 for w in words if w in name) + sum(1 for w in words if w in desc)
            if s:
                scored.append((s, t.name, t))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [t for _, _, t in scored[:limit]]


LOADED_KEY = "loaded_tools"


def make_tool_search(registry_getter) -> Tool:
    @tool(title="Подключение инструментов", read_only=True, group="core", source="builtin")
    async def tool_search(
        ctx: ToolContext,
        query: Annotated[str, "Слова для поиска или select:имя1,имя2 для точного выбора"],
        limit: Annotated[int, "Сколько инструментов вернуть"] = 8,
    ) -> ToolResult:
        """Находит и подключает отложенные инструменты. Их имена перечислены в системном промпте
        в разделе «Отложенные инструменты»; до вызова tool_search их схемы неизвестны и вызвать их нельзя.
        Загружай сразу все, которые понадобятся, одним вызовом."""
        reg: ToolRegistry = registry_getter(ctx)
        found = [t for t in reg.search(query, limit) if t.deferred]
        if not found:
            return ToolResult(content="Ничего не найдено. Попробуй другие слова или select:<точное имя>.")
        state = await ctx.store.get_state(ctx.session_id)
        loaded = set(state.get(LOADED_KEY, []))
        loaded.update(t.name for t in found)
        state[LOADED_KEY] = sorted(loaded)
        await ctx.store.set_state(ctx.session_id, state)
        lines = [f"Подключено {len(found)}; теперь их можно вызывать:"]
        for t in found:
            lines.append(f"- {t.name}: {t.description.splitlines()[0] if t.description else ''}")
        return ToolResult(content="\n".join(lines), data=[t.name for t in found])

    return tool_search
