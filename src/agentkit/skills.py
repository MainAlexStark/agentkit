"""Skills — пакеты инструкций, которые агент подгружает сам, когда они нужны.

Каталог skill'а: SKILL.md с YAML-шапкой (name, description) и любые файлы
рядом (шаблоны, примеры, скрипты). В промпт попадает только список
«имя — описание»; текст целиком агент получает инструментом skill. Так
десятки skill'ов стоят столько же токенов, сколько их оглавление.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import yaml

from agentkit.tools.base import Tool, ToolContext, ToolResult, tool


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path | None = None
    files: list[str] = field(default_factory=list)


def parse_skill_md(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        return yaml.safe_load(fm) or {}, body.strip()
    return {}, text.strip()


class SkillLibrary:
    def __init__(self, skills: list[Skill] | None = None):
        self.skills: dict[str, Skill] = {s.name: s for s in skills or []}

    @classmethod
    def from_dirs(cls, *dirs: str | Path) -> SkillLibrary:
        lib = cls()
        for d in dirs:
            root = Path(d)
            if not root.is_dir():
                continue
            for md in sorted(root.glob("*/SKILL.md")):
                meta, body = parse_skill_md(md.read_text(encoding="utf-8"))
                name = meta.get("name") or md.parent.name
                files = sorted(str(p.relative_to(md.parent)) for p in md.parent.rglob("*")
                               if p.is_file() and p.name != "SKILL.md")
                lib.skills[name] = Skill(name=name, description=meta.get("description", ""), body=body,
                                         path=md.parent, files=files)
        return lib

    def index(self) -> str:
        if not self.skills:
            return ""
        lines = ["# Skills", "Готовые инструкции для типовых задач. Если задача подходит под описание — "
                 "сначала загрузи skill инструментом skill и следуй ему."]
        lines += [f"- {s.name}: {s.description}" for s in self.skills.values()]
        return "\n".join(lines)

    def tool(self) -> Tool:
        lib = self

        @tool(title="Инструкция", read_only=True, group="core", source="builtin")
        async def skill(
            ctx: ToolContext,
            name: Annotated[str, "Имя skill'а из списка в системном промпте"],
            file: Annotated[str | None, "Необязательно: файл внутри skill'а (шаблон, пример)"] = None,
        ) -> ToolResult:
            """Загружает инструкции skill'а (или один его файл)."""
            s = lib.skills.get(name)
            if s is None:
                return ToolResult.error(f"нет skill'а «{name}». Есть: {', '.join(lib.skills)}")
            if file:
                if s.path is None or file not in s.files:
                    return ToolResult.error(f"в skill'е {name} нет файла {file}. Есть: {', '.join(s.files)}")
                return ToolResult(content=(s.path / file).read_text(encoding="utf-8", errors="replace"))
            extra = f"\n\nФайлы skill'а (читай через skill(name, file)): {', '.join(s.files)}" if s.files else ""
            return ToolResult(content=s.body + extra)

        return skill
