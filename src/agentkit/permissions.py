"""Права на вызов инструментов: режимы и правила allow / ask / deny.

Порядок решения (первое совпадение побеждает):
  1. deny-правило                         → deny
  2. режим plan и инструмент не read_only  → deny (план без изменений)
  3. режим readonly и инструмент пишет     → deny
  4. ask-правило                          → ask
  5. allow-правило                        → allow
  6. режим bypass                          → allow
  7. needs_approval у инструмента          → ask (даже в auto: это доменная страховка)
  8. read_only                             → allow
  9. режим auto                            → allow
 10. accept_edits и группа files           → allow (кроме destructive)
 11. иначе (режим default)                 → ask

Правило — строка «имя_инструмента» или «имя(шаблон)»: шаблон сравнивается
со «смысловым» аргументом инструмента (command у shell, path у файлов, url у
web_fetch) или с JSON всех аргументов. Примеры: "shell(git status*)",
"write_file(notes/*)", "mcp__ozon__*".
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agentkit.tools.base import Tool, ToolContext


class Mode(StrEnum):
    DEFAULT = "default"  # пишущее — по подтверждению
    ACCEPT_EDITS = "accept_edits"  # правки файлов рабочей папки — без подтверждения
    PLAN = "plan"  # только чтение, готовим план
    READONLY = "readonly"  # только чтение, без плана (наблюдатель)
    AUTO = "auto"  # всё, кроме deny и needs_approval
    BYPASS = "bypass"  # всё, кроме deny — только для доверенных автоматизаций


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class Verdict:
    decision: Decision
    reason: str = ""


# Какой аргумент считается главным для сопоставления с шаблоном правила.
PRIMARY_ARG = ("command", "path", "file_path", "url", "pattern", "query")

_RULE = re.compile(r"^(?P<name>[^()]+?)(?:\((?P<arg>.*)\))?$")


@dataclass
class Rule:
    name: str
    arg: str | None = None

    @classmethod
    def parse(cls, s: str) -> Rule:
        m = _RULE.match(s.strip())
        if not m:
            raise ValueError(f"не разобрать правило «{s}»")
        return cls(name=m["name"].strip(), arg=m["arg"])

    def matches(self, tool_name: str, args: dict[str, Any]) -> bool:
        if not fnmatch.fnmatchcase(tool_name, self.name):
            return False
        if self.arg is None:
            return True
        subject = next((str(args[k]) for k in PRIMARY_ARG if k in args), None)
        if subject is None and len(args) == 1:
            subject = str(next(iter(args.values())))
        if subject is None:
            subject = json.dumps(args, ensure_ascii=False, sort_keys=True)
        return fnmatch.fnmatchcase(subject, self.arg)


@dataclass
class Permissions:
    mode: Mode = Mode.DEFAULT
    allow: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    # Группы инструментов, которые режим ACCEPT_EDITS пропускает без вопроса.
    edit_groups: tuple[str, ...] = ("files",)

    def __post_init__(self) -> None:
        self._allow = [Rule.parse(r) for r in self.allow]
        self._ask = [Rule.parse(r) for r in self.ask]
        self._deny = [Rule.parse(r) for r in self.deny]

    def with_mode(self, mode: Mode | str) -> Permissions:
        return Permissions(mode=Mode(mode), allow=self.allow, ask=self.ask, deny=self.deny, edit_groups=self.edit_groups)

    def check(self, tool: Tool, args: dict[str, Any], ctx: ToolContext | None = None) -> Verdict:
        name = tool.name
        if any(r.matches(name, args) for r in self._deny):
            return Verdict(Decision.DENY, "запрещено правилом")
        if self.mode == Mode.PLAN and not tool.read_only:
            return Verdict(Decision.DENY, "режим планирования: изменения запрещены, сначала покажи план")
        if self.mode == Mode.READONLY and not tool.read_only:
            return Verdict(Decision.DENY, "режим только чтения")
        if any(r.matches(name, args) for r in self._ask):
            return Verdict(Decision.ASK, "правило требует подтверждения")
        if any(r.matches(name, args) for r in self._allow):
            return Verdict(Decision.ALLOW)
        if self.mode == Mode.BYPASS:
            return Verdict(Decision.ALLOW)
        need = tool.needs_approval
        if callable(need):
            res = need(args, ctx) if ctx is not None else True
            if res:
                return Verdict(Decision.ASK, res if isinstance(res, str) else "")
        elif need:
            return Verdict(Decision.ASK, "")
        if tool.read_only:
            return Verdict(Decision.ALLOW)
        if self.mode in (Mode.AUTO, Mode.BYPASS):
            return Verdict(Decision.ALLOW)
        if self.mode == Mode.ACCEPT_EDITS and tool.group in self.edit_groups and not tool.destructive:
            return Verdict(Decision.ALLOW)
        return Verdict(Decision.ASK, "")
