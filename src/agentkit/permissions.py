"""Права на вызов инструментов: режимы и правила allow / ask / deny.

Порядок решения (первое совпадение побеждает):
  1. deny-правило или «Заблокирован» у инструмента → deny
  2. режим plan и инструмент не read_only  → deny (план без изменений)
  3. режим readonly и инструмент пишет     → deny
  4. ask-правило или «Спрашивать» у инструмента → ask
  5. allow-правило или «Всегда разрешать»  → allow
  6. режим bypass                          → allow
  7. needs_approval у инструмента          → ask (даже в auto: это доменная страховка)
  8. read_only                             → allow
  9. зона агента (группа sandbox)          → allow (кроме destructive)
 10. режим auto                            → allow
 11. accept_edits (или workspace_edits) и группа files → allow (кроме destructive)
 12. иначе (режим default)                 → ask

В интерфейсе три режима: default («Спрашивать»), auto («Автоматически»),
bypass («Без вопросов»); остальные — для субагентов и хост-приложений.
Выбор «Всегда разрешать / Спрашивать / Заблокирован» у конкретного инструмента —
`tool_policy` ({имя: allow|ask|deny}), его задаёт пользователь в «Инструментах».

Правило — строка «имя_инструмента» или «имя(шаблон)»: шаблон сравнивается
со «смысловым» аргументом инструмента (command у shell, path у файлов, url у
web_fetch) или с JSON всех аргументов. Примеры: "shell(git status*)",
"write_file(notes/*)", "mcp__ozon__*". Команда shell сверяется по частям (`a && b`, `$(…)`,
`bash -c`, `sudo`): deny и ask — если подходит хоть одна часть, allow — только если подходят все.
"""

from __future__ import annotations

import fnmatch
import json
import posixpath
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

    def matches(self, tool_name: str, args: dict[str, Any], *, every: bool = False) -> bool:
        """Совпадает ли вызов с правилом.

        У команды (аргумент command) шаблон сверяется с каждой частью: `ls x && rm y` — это и `ls x`,
        и `rm y`. Целиком строка шаблону «rm *» не равна, и deny-правило пропускало удаление
        (нашлось живым прогоном: модель сама дописала `ls … && rm tmp.log`). `every=False` (deny, ask) —
        хватит одной части; `every=True` (allow) — нужны все, иначе `shell(git status*)` без вопроса
        выполнял `git status; rm -rf ~`. Команду, которую не разобрать, allow не пропускает.
        """
        if not fnmatch.fnmatchcase(tool_name, self.name):
            return False
        if self.arg is None:
            return True
        if "command" in args and isinstance(args["command"], str):
            parts = command_parts(args["command"])
            if parts is None:  # не разобрали: deny/ask — по всей строке, allow — нет
                return not every and fnmatch.fnmatchcase(args["command"].strip(), self.arg)
            if every:
                return bool(parts) and all(any(fnmatch.fnmatchcase(v, self.arg) for v in p.exact) for p in parts)
            return any(fnmatch.fnmatchcase(v, self.arg) for p in parts for v in p.loose)
        subject = next((str(args[k]) for k in PRIMARY_ARG if k in args), None)
        if subject is not None and any(k in args for k in ("path", "file_path")) and "command" not in args:
            # `notes/../config.yaml` под allow «notes/*» попадал (* в fnmatch съедает и «/»),
            # а `./secret/x` мимо deny «secret/*». Сверяем путь, каким его увидит файловая система.
            subject = posixpath.normpath(subject.replace("\\", "/"))
        if subject is None and len(args) == 1:
            subject = str(next(iter(args.values())))
        if subject is None:
            subject = json.dumps(args, ensure_ascii=False, sort_keys=True)
        return fnmatch.fnmatchcase(subject, self.arg)


# Обёртки, за которыми идёт настоящая команда: `sudo rm x` — это `rm x`.
_WRAPPERS = {"sudo", "doas", "env", "command", "builtin", "exec", "nohup", "time", "nice"}
# Команды, исполняющие строку-аргумент как shell: `bash -c "rm x"`, `eval "rm x"`.
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SUBST = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_SEP_CHARS = set(";&|()")


@dataclass
class CommandPart:
    """Простая команда из составной. `exact` — её записи для allow: как есть, без `VAR=…` и обёрток
    (`sudo`, `env`…), с именем без пути (`/bin/rm` → `rm`). `loose` — ещё и с любого слова: для deny
    и ask, чтобы `timeout 5 rm x`, `sudo -u root rm x`, `find . -exec rm {} ;` тоже попадали под
    `rm *`. Лишний отказ безопаснее пропущенного удаления; allow по `loose` не судит никогда."""

    exact: list[str]
    loose: list[str]


def command_parts(command: str, depth: int = 0) -> list[CommandPart] | None:
    """Команда shell → простые команды: `a && b | c; d` → a, b, c, d. Подстановки `$(…)` и `` `…` ``,
    `bash -c "…"`, `eval …` разбираются внутрь. None — не разобрать (незакрытая кавычка, вложенность
    глубже 3) — тогда allow не пропускает такую команду."""
    import shlex

    if depth > 3:
        return None
    parts: list[CommandPart] = []
    for m in _SUBST.finditer(command):
        inner = command_parts(m.group(1) if m.group(1) is not None else m.group(2), depth + 1)
        if inner is None:
            return None
        parts += inner
    # Перевод строки — тоже разделитель команд, а shlex считает его пробелом. `<>` в пунктуации —
    # чтобы `2>&1` стал `2 >& 1`, а не разрезал команду по `&`.
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        return None
    seg: list[str] = []
    for tok in [*tokens, ";"]:
        if not (set(tok) <= _SEP_CHARS):  # «&&», «|», «;», «(»… — граница; «>&», «2>» — нет
            seg.append(tok)
            continue
        if seg:
            sub = _segment(seg, depth)
            if sub is None:
                return None
            parts += sub
        seg = []
    return parts


def _segment(tokens: list[str], depth: int) -> list[CommandPart] | None:
    exact = [" ".join(tokens)]
    words = list(tokens)
    while words and (_ASSIGN.match(words[0]) or words[0] in _WRAPPERS):
        words = words[1:]
        if words:
            exact.append(" ".join(words))
    if not words:
        return [CommandPart(exact, exact)]
    head = words[0].rsplit("/", 1)[-1]
    if head != words[0]:
        exact.append(" ".join([head, *words[1:]]))
    # Слово с пробелом — из кавычек (`git commit -m 'rm x'`): это данные, а не команда.
    loose = exact + [" ".join([w.rsplit("/", 1)[-1], *words[i + 1:]])
                     for i, w in enumerate(words) if i and not any(c.isspace() for c in w)]
    out = [CommandPart(exact, loose)]
    inner_cmd = None
    if head in _SHELLS and "-c" in words[1:]:
        i = words.index("-c", 1)
        inner_cmd = words[i + 1] if i + 1 < len(words) else None
    elif head == "eval" and len(words) > 1:
        inner_cmd = " ".join(words[1:])
    if inner_cmd is not None:
        inner = command_parts(inner_cmd, depth + 1)
        if inner is None:
            return None
        out += inner
    return out


@dataclass
class Permissions:
    mode: Mode = Mode.DEFAULT
    allow: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    # Группы инструментов, которые режим ACCEPT_EDITS пропускает без вопроса.
    edit_groups: tuple[str, ...] = ("files",)
    # Правки рабочей папки без вопроса в любом пишущем режиме — когда папка своя у каждого разговора
    # (сервис): иначе каждая правка HTML-черновика ждала человека (8 подтверждений на один дашборд).
    workspace_edits: bool = False
    # Зона агента — группы, которые работают без вопроса в любом пишущем режиме. Песочница —
    # отдельный контейнер на разговор: без этого каждый run_python в локальной песочнице
    # (url=local, там инструменты не read_only) ждал карточку, и агент не мог ничего посчитать сам.
    agent_zone: tuple[str, ...] = ("sandbox",)
    # Выбор пользователя по инструментам: {имя: allow|ask|deny}. Правила сервиса (deny/ask) сильнее.
    tool_policy: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._allow = [Rule.parse(r) for r in self.allow]
        self._ask = [Rule.parse(r) for r in self.ask]
        self._deny = [Rule.parse(r) for r in self.deny]

    def with_mode(self, mode: Mode | str, tool_policy: dict[str, str] | None = None) -> Permissions:
        return Permissions(mode=Mode(mode), allow=self.allow, ask=self.ask, deny=self.deny, edit_groups=self.edit_groups,
                           workspace_edits=self.workspace_edits, agent_zone=self.agent_zone,
                           tool_policy=self.tool_policy if tool_policy is None else tool_policy)

    def blocked(self, name: str) -> bool:
        """Заблокирован пользователем — такой инструмент не показываем модели вовсе."""
        return self.tool_policy.get(name) == "deny"

    def check(self, tool: Tool, args: dict[str, Any], ctx: ToolContext | None = None) -> Verdict:
        name = tool.name
        own = self.tool_policy.get(name)
        if any(r.matches(name, args) for r in self._deny):
            return Verdict(Decision.DENY, "запрещено правилом")
        if own == "deny":
            return Verdict(Decision.DENY, "инструмент заблокирован в настройках «Инструменты»")
        if self.mode == Mode.PLAN and not tool.read_only:
            return Verdict(Decision.DENY, "режим планирования: изменения запрещены, сначала покажи план")
        if self.mode == Mode.READONLY and not tool.read_only:
            return Verdict(Decision.DENY, "режим только чтения")
        if any(r.matches(name, args) for r in self._ask) or own == "ask":
            return Verdict(Decision.ASK, "правило требует подтверждения" if own != "ask" else "")
        if any(r.matches(name, args, every=True) for r in self._allow) or own == "allow":
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
        if tool.group in self.agent_zone and not tool.destructive:
            return Verdict(Decision.ALLOW)
        if self.mode in (Mode.AUTO, Mode.BYPASS):
            return Verdict(Decision.ALLOW)
        if (self.mode == Mode.ACCEPT_EDITS or self.workspace_edits) and tool.group in self.edit_groups \
                and not tool.destructive:
            return Verdict(Decision.ALLOW)
        return Verdict(Decision.ASK, "")
