"""Маскирование секретов: ключи и токены не уходят модели, в журнал, трейсы и логи.

Два источника:

* **Известные значения** — секреты самого сервиса: ключи моделей, токены доступа,
  токены MCP-серверов и OAuth. Берутся из окружения, настроек и подключений и
  заменяются всегда, выключить нельзя: модели они не нужны никогда (инструменты
  пользуются ими в коде), а `env` в shell или `cat .env` иначе отдавали ключ
  OpenRouter провайдеру модели и в журнал прогона.
* **Шаблоны** — типовые форматы (sk-…, ghp_…, AKIA…, JWT, PEM, `PASSWORD=…`,
  пароль в URL). Ловят чужие секреты в данных: ключ из .env пользователя тоже
  не должен оседать у провайдера модели и в логах. Выключаются `patterns=False`
  (`AGENT_REDACT_PATTERNS=false`), если агент по делу работает с такими данными.

Где применяется (agent.py): результат инструмента — до модели, до обрезки и до
blob (`read_output` не достаёт полный текст); каждое событие — до базы, UI и
трейсеров; записи стандартного `logging` — через фабрику записей.

Секрет заменяется маркером `[секрет скрыт: <что>]`. Модель видит маркер вместо
значения, поэтому может «вписать» его в файл — и испортить .env или конфиг.
Поэтому пишущий вызов с маркером в аргументах агент не выполняет (см.
`contains_marker` в `Agent._one`): секрет вписывает человек.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

MARKER = "[секрет скрыт"


def marker(label: str) -> str:
    return f"{MARKER}: {label}]"


# Порядок важен: конкретные форматы раньше общих (sk-or-v1 раньше sk-), чтобы метка была точной.
_PATTERNS: list[tuple[str, str]] = [
    ("private-key", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
    ("openrouter-key", r"\bsk-or-v1-[0-9a-f]{32,}"),
    ("anthropic-key", r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    ("openai-key", r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"),
    ("stripe-key", r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})"),
    ("gitlab-token", r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    ("aws-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("google-key", r"\bAIza[0-9A-Za-z_\-]{35}"),
    ("slack-token", r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    ("telegram-token", r"\b\d{8,10}:AA[A-Za-z0-9_\-]{33}\b"),
    ("yandex-key", r"\bAQVN[A-Za-z0-9_\-]{30,}"),
    ("yandex-iam", r"\bt1\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]{40,}"),
    ("yandex-oauth", r"\by[0-3]_[A-Za-z0-9_\-]{40,}"),
    ("tavily-key", r"\btvly-[A-Za-z0-9_\-]{20,}"),
    ("device-key", r"\bdev_[A-Za-z0-9]{6,}\.[A-Za-z0-9_\-]{24,}"),
    ("jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
]
# Значение после префикса: префикс (имя, «Bearer », «user:» в URL) остаётся — видно, что и где скрыто.
_KEEP_PREFIX: list[tuple[str, str]] = [
    ("authorization", r"(?i)(\bbearer\s+|\bbasic\s+)([A-Za-z0-9._~+/\-]{16,}=*)"),
    ("пароль в URL", r"(?i)(\b[a-z][a-z0-9+.\-]*://[^\s:/@\"'<>]*:)([^\s/@\"'<>]{3,})(?=@)"),
    # PASSWORD=…, "api_key": "…", client_secret: … — имя оканчивается ключевым словом.
    # Без «s» на конце: max_tokens: 100000 — не секрет; tokenizer — тоже.
    ("assignment", r"(?i)((?<![A-Za-z0-9])[\"']?[A-Za-z0-9_\-]*(?:api[_\-]?key|[_\-]key|secret|token|passw(?:or)?d"
                   r"|pwd|credentials?)[\"']?\s*[:=]\s*[\"']?)([^\s\"'`,;#\[\]{}()<>]{8,})"),
]
_DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
# Имена переменных окружения, значения которых — секреты сервиса.
_SECRET_NAME = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)")
_NOT_SECRET_NAMES = {"PWD", "OLDPWD", "SSH_AUTH_SOCK", "GPG_AGENT_INFO", "KEYTIMEOUT"}
MIN_LEN = 8


def _secretish(v: str) -> bool:
    """Похоже ли значение присваивания на секрет, а не на код или число.

    `api_key=settings.openrouter_key` в исходнике — не секрет: замаскируй его, и
    edit_file перестал бы находить строку, которую модель прочитала.
    """
    if v.isdigit() or v.lower() in ("true", "false", "none", "null") or _DOTTED.fullmatch(v):
        return False
    if v.startswith(("/", "./", "~", "$", "http://", "https://")) or MARKER in v:
        return False
    has_d, has_a = any(c.isdigit() for c in v), any(c.isalpha() for c in v)
    return (has_d and has_a) or (len(v) >= 20 and any(c.isupper() for c in v) and any(c.islower() for c in v))


class Redactor:
    """Заменяет секреты маркером. Потокобезопасен: значения добавляются на ходу (OAuth, MCP)."""

    def __init__(self, *, patterns: bool = True, env: Mapping[str, str] | None = None, base: Redactor | None = None):
        self.patterns = patterns
        # base — общий Redactor процесса: его известные значения действуют и здесь. Так у агента
        # свой выбор шаблонов (AGENT_REDACT_PATTERNS), а токены OAuth/MCP, пришедшие на ходу, — общие.
        self.base = base
        self._known: dict[str, str] = {}  # значение → метка
        self._lock = threading.Lock()
        self._rx: re.Pattern[str] | None = None
        self._pat = [(label, re.compile(rx)) for label, rx in _PATTERNS]
        self._keep = [(label, re.compile(rx)) for label, rx in _KEEP_PREFIX]
        if env is not None:
            self.add_env(env)

    # ── что скрывать ────────────────────────────────────────────────

    def add(self, value: str | None, label: str = "секрет") -> None:
        """Известный секрет: заменяется всегда, где бы ни встретился."""
        v = (value or "").strip()
        if len(v) < MIN_LEN or v.isdigit() or MARKER in v:
            return
        with self._lock:
            if self._known.get(v) == label:
                return
            self._known.setdefault(v, label)
            self._rx = None

    def add_url(self, url: str | None, label: str) -> None:
        """Пароль из строки подключения (postgres://user:пароль@…, redis://:пароль@…)."""
        try:
            pw = urlsplit(url or "").password
        except ValueError:
            pw = None
        self.add(pw, label)

    def add_env(self, env: Mapping[str, str], names: Iterable[str] | None = None) -> None:
        """Секреты из окружения: переменные с KEY/TOKEN/SECRET/PASSWORD в имени.

        Списки вида «id:токен:роль,…» (AGENT_TOKENS) скрываются и целиком, и по частям.
        """
        for name in names if names is not None else env:
            value = env.get(name) or ""
            if name in _NOT_SECRET_NAMES or not _SECRET_NAME.search(name) or value.startswith(("/", "~")):
                continue
            self.add(value, name)
            for piece in re.split(r"[\s,;:|]+", value):
                if len(piece) >= 12:
                    self.add(piece, name)

    def known_count(self) -> int:
        return len(self._known)

    # ── замена ──────────────────────────────────────────────────────

    def _known_rx(self) -> re.Pattern[str] | None:
        with self._lock:
            if self._rx is None and self._known:
                vals = sorted(self._known, key=len, reverse=True)
                self._rx = re.compile("|".join(re.escape(v) for v in vals))
            return self._rx

    def text(self, s: str) -> str:
        if not s:
            return s
        if self.base is not None:
            s = self.base.known(s)
        s = self.known(s)
        return self._by_patterns(s) if self.patterns else s

    def known(self, s: str) -> str:
        """Только известные значения, без шаблонов."""
        if s and (rx := self._known_rx()):
            s = rx.sub(lambda m: marker(self._known.get(m.group(0), "секрет")), s)
        return s

    def _by_patterns(self, s: str) -> str:
        for label, rx in self._pat:
            s = rx.sub(marker(label), s)
        for label, rx in self._keep:
            def keep(m: re.Match[str], label: str = label) -> str:
                if label != "assignment":
                    return m.group(1) + marker(label)
                if not _secretish(m.group(2)):
                    return m.group(0)
                # Метка — имя переменной: «PASSWORD», «client_secret».
                return m.group(1) + marker(m.group(1).strip(" \t\"':=") or "секрет")
            s = rx.sub(keep, s)
        return s

    def deep(self, v: Any) -> Any:
        """Строки внутри dict/list (данные событий, виджеты результата) — новая копия."""
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, dict):
            return {k: self.deep(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [self.deep(x) for x in v]
        return v


_PEM_END = re.compile(r"-----END [A-Z0-9 ]*-----")


class StreamRedactor:
    """Маскирование потока кусков текста (`text_delta`), где секрет может прийти по частям.

    Каждый кусок по отдельности не маскируется: «sk-or-v1-» в одном куске и остаток
    ключа в следующем ни один шаблон не узнаёт — в живом UI ключ мелькал целиком,
    хотя в базе и итоговом сообщении был скрыт. Поэтому придерживается хвост, который
    ещё может оказаться началом секрета: последнее «слово» без пробелов (ключи, токены,
    пароль в URL — без пробелов) и незакрытый блок PEM. Пробел или перевод строки
    завершают слово — тогда шаблоны решают окончательно, и текст уходит дальше.

    Шаблоны с префиксом («PASSWORD = …», «Bearer …») видят начало строки: маскируется
    кусок вместе с контекстом — хвостом уже отданной строки, — а наружу уходит только
    новая часть.
    """

    CONTEXT = 200   # сколько уже отданного текста строки видят шаблоны с префиксом
    HOLD_MAX = 400  # слово длиннее — не секрет (base64-картинка, длинный код): отдаём, чтобы стрим не вставал
    PEM_MAX = 8000

    def __init__(self, redactor: Redactor):
        self.r = redactor
        self._pending = ""  # пришло, но ещё не отдано
        self._ctx = ""      # хвост отданной строки (сырой) — контекст для шаблонов

    def push(self, chunk: str) -> str:
        """Добавить кусок; вернуть то, что уже можно показать (может быть пусто)."""
        self._pending += chunk
        return self._settle(self._cut())

    def flush(self) -> str:
        """Конец потока: отдать всё, что придерживалось."""
        return self._settle(len(self._pending))

    def _cut(self) -> int:
        p = self._pending
        ws = max(p.rfind(" "), p.rfind("\n"), p.rfind("\t"))
        cut = ws + 1
        if len(p) - cut > self.HOLD_MAX:
            cut = len(p)
        pem = p.rfind("-----BEGIN ")
        if pem != -1 and len(p) - pem < self.PEM_MAX:
            # Ключ PEM многострочный: держим весь блок до END, и END целиком — в одном куске с BEGIN.
            end = _PEM_END.search(p, pem)
            cut = pem if end is None else max(cut, end.end())
        return cut

    def _settle(self, cut: int) -> str:
        if cut <= 0:
            return ""
        part, self._pending = self._pending[:cut], self._pending[cut:]
        ctx = self._ctx
        whole, ctx_red = self.r.text(ctx + part), self.r.text(ctx)
        # Контекст уже отдан; если его маскирование не изменилось — отдаём только новое.
        out = whole[len(ctx_red):] if whole.startswith(ctx_red) else self.r.text(part)
        tail = (ctx + part).rsplit("\n", 1)[-1]
        if len(tail) > self.CONTEXT:
            tail = tail[-self.CONTEXT:]
            sp = tail.find(" ")
            tail = tail[sp + 1:] if sp != -1 else ""  # не начинать контекст с середины слова
        self._ctx = tail
        return out


def contains_marker(v: Any) -> bool:
    if isinstance(v, str):
        return MARKER in v
    if isinstance(v, dict):
        return any(contains_marker(x) for x in v.values())
    if isinstance(v, (list, tuple)):
        return any(contains_marker(x) for x in v)
    return False


# Общий на процесс: OAuth и MCP добавляют токены сюда, его же видит фабрика логов.
DEFAULT = Redactor(env=os.environ)


def remember(value: str | None, label: str = "секрет") -> None:
    """Добавить секрет, полученный на ходу (OAuth-токен, токен MCP), в общий Redactor."""
    DEFAULT.add(value, label)


_installed = False


def install_log_redaction(redactor: Redactor | None = None) -> None:
    """Секреты не попадают в стандартный logging — ни в сообщение, ни в traceback.

    Фабрика записей, а не фильтр: фильтр на логгере не видит записей, всплывающих
    от дочерних логгеров (httpx, uvicorn), а обработчики uvicorn ставит позже нас.
    """
    global _installed
    if _installed:
        return
    r = redactor or DEFAULT
    base = logging.getLogRecordFactory()

    def factory(*args: Any, **kw: Any) -> logging.LogRecord:
        rec = base(*args, **kw)
        try:
            rec.msg, rec.args = r.text(rec.getMessage()), None
            if rec.exc_info and not rec.exc_text:
                rec.exc_text = r.text(logging.Formatter().formatException(rec.exc_info))
        except Exception:  # noqa: BLE001 — лог с кривыми аргументами не должен ронять вызывающего
            pass
        return rec

    logging.setLogRecordFactory(factory)
    _installed = True
