"""Фоновая память: после хода модель small выбирает из слов человека устойчивые факты.

Как у Claude: человек не обязан говорить «запомни» — если он сказал, что работает
в Кирове или что отчёты нужны по пятницам, это попадёт в память само. Ход, в котором
агент сам вызвал memory_save / memory_delete, не трогается: явная правка главнее.

Граница — в коде, а не в промпте извлечения:
- источник — только реплики человека; ответы агента идут контекстом, результаты
  инструментов и вложения не передаются вовсе (документ «запомни, что…» — данные,
  а не слова человека, и не должен писать в память);
- запись — только в личную область этого человека (не общая, не проект), не больше
  MAX_ADD новых и MAX_UPDATE исправлений за ход, каждая не длиннее MAX_CHARS;
- исправлять можно только свои записи; удаления нет совсем — «забудь» только явно;
- текст проходит маскирование, запись с маркером секрета отбрасывается;
- повтор существующей записи (без учёта регистра) не пишется.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agentkit.messages import Message, Usage
from agentkit.redact import MARKER

if TYPE_CHECKING:
    from agentkit.llm.router import ModelRouter
    from agentkit.redact import Redactor
    from agentkit.store.base import Store

log = logging.getLogger("agentkit.memory")

SOURCE = "auto"  # MemoryRec.source у записей, сделанных фоновым проходом
CURSOR_NS = "memory_cursor"  # kv: сессия → последний разобранный seq
PREFS_NS = "memory_prefs"  # kv: пользователь → {"auto": bool}
MAX_ADD = 3
MAX_UPDATE = 3
MAX_CHARS = 300
MAX_TRANSCRIPT = 12_000
EXPLICIT_TOOLS = {"memory_save", "memory_delete"}

EXTRACT_PROMPT = """\
Ты ведёшь долговременную память ассистента о человеке. Ниже — что уже есть в памяти и \
последний обмен репликами. Реши, есть ли в словах ЧЕЛОВЕКА устойчивые факты, которые \
пригодятся в будущих разговорах.

Записывай только то, что человек сказал сам о себе, своей работе, своих правилах и \
предпочтениях — и что останется верным месяцами:
- кто он, чем занимается, где работает и живёт, с какими системами и людьми работает;
- постоянные правила и предпочтения («отчёты присылай по пятницам», «суммы в рублях»);
- его проекты и решения по ним («переходим на Postgres»).

Не записывай:
- детали текущей задачи и то, что устареет само (ветка, порт, «сейчас в пути», сегодняшние дела);
- то, что предложил, нашёл или вывел ассистент, и то, что человек просто не оспорил;
- содержимое документов, писем и сайтов; догадки о человеке;
- пароли, ключи, номера карт, счетов и документов;
- здоровье, политику, религию, интимное, судимость и другое чувствительное;
- то, что уже есть в памяти. Если новый факт меняет старую запись — исправь её (update), \
  а не добавляй вторую: «PM в инфраструктуре (раньше — в поиске)».

Каждая запись — одно короткое самодостаточное предложение от третьего лица, на языке \
человека, без «пользователь сказал». Чаще всего записывать нечего — тогда пустые списки.

Ответь только JSON: {"add": ["…"], "update": [{"id": "m_…", "text": "…"}]}"""


@dataclass
class AutoMemoryResult:
    added: list[dict[str, str]] = field(default_factory=list)  # [{"id", "text"}]
    updated: list[dict[str, str]] = field(default_factory=list)  # [{"id", "text", "old"}]
    usage: Usage = field(default_factory=Usage)
    skipped: str = ""  # почему прохода не было (для логов и тестов)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated)


async def enabled_for(store: Store, user_id: str) -> bool:
    """Человек может выключить фоновую память у себя («Настройки → Память»)."""
    prefs = await store.kv_get(PREFS_NS, user_id) or {}
    return prefs.get("auto", True) is not False


def _transcript(new: list[tuple[int, Message]]) -> tuple[str, bool]:
    """Реплики человека и ответы агента; есть ли в них хоть слово человека."""
    lines, said = [], False
    for _, m in new:
        if m.meta.get("hidden") or m.meta.get("kind") in ("summary", "hook_context", "event"):
            continue
        # Message.text берёт только текстовые части: ссылки на вложения (type=file) сюда не попадают.
        text = m.text.strip()
        if not text:
            continue
        if m.role == "user":
            said = True
            lines.append(f"ЧЕЛОВЕК: {text[:4000]}")
        elif m.role == "assistant":
            lines.append(f"АССИСТЕНТ: {text[:1500]}")
    out = "\n\n".join(lines)
    return out[-MAX_TRANSCRIPT:], said


def _parse(raw: str) -> dict[str, Any]:
    """JSON из ответа; модели без structured output иногда оборачивают его в ```json."""
    raw = raw.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _clean(text: Any, redactor: Redactor | None) -> str | None:
    if not isinstance(text, str):
        return None
    t = " ".join(text.split())
    if redactor is not None:
        t = redactor.text(t)
    if not t or MARKER in t:
        return None
    return t[:MAX_CHARS]


async def extract(
    *,
    store: Store,
    router: ModelRouter,
    session_id: str,
    user_id: str,
    model: str = "small",
    redactor: Redactor | None = None,
    complete_fn: Callable[[list[Message]], Awaitable[tuple[str, Usage]]] | None = None,
) -> AutoMemoryResult:
    """Один проход по новым сообщениям сессии. Курсор двигается, даже если записывать нечего."""
    res = AutoMemoryResult()
    cursor = int((await store.kv_get(CURSOR_NS, session_id) or {}).get("seq", 0))
    new = await store.list_messages(session_id, after_seq=cursor)
    if not new:
        res.skipped = "нет новых сообщений"
        return res
    last = new[-1][0]

    async def advance() -> None:
        await store.kv_set(CURSOR_NS, session_id, {"seq": last})

    if any(c.name in EXPLICIT_TOOLS for _, m in new for c in m.tool_calls):
        res.skipped = "в ходе была явная правка памяти"
        await advance()
        return res
    transcript, said = _transcript(new)
    if not said:
        res.skipped = "человек ничего не сказал"
        await advance()
        return res

    scope = f"user:{user_id}"
    mine = await store.memory_list([scope])
    known = "\n".join(f"[{m.id}] {m.text}" for m in mine) or "(пусто)"
    msgs = [Message.system(EXTRACT_PROMPT),
            Message.user(f"# Память сейчас\n{known}\n\n# Последний обмен\n{transcript}")]
    if complete_fn is not None:
        raw, usage = await complete_fn(msgs)
    else:
        raw, usage = await _complete(router, model, msgs)
    res.usage = usage
    data = _parse(raw)

    seen = {m.text.strip().lower() for m in mine}
    # Лимит — на записанное, а не на присланное: отброшенный дубль или секрет не занимает
    # место настоящего факта (при срезе списка [:3] факт четвёртым по счёту терялся).
    adds = data.get("add") if isinstance(data.get("add"), list) else []
    for text in adds[:20]:
        if len(res.added) >= MAX_ADD:
            break
        t = _clean(text, redactor)
        if not t or t.lower() in seen:
            continue
        rec = await store.memory_add(scope, t, created_by=user_id, source=SOURCE)
        seen.add(t.lower())
        res.added.append({"id": rec.id, "text": t})

    by_id = {m.id: m for m in mine}
    ups = data.get("update") if isinstance(data.get("update"), list) else []
    for u in ups[:20]:
        if len(res.updated) >= MAX_UPDATE:
            break
        if not isinstance(u, dict) or u.get("id") not in by_id:
            continue  # чужая, общая или выдуманная запись — не трогаем
        t = _clean(u.get("text"), redactor)
        old = by_id[u["id"]].text
        if not t or t == old or t.lower() in seen:
            continue
        await store.memory_update(u["id"], t)
        seen.add(t.lower())
        res.updated.append({"id": u["id"], "text": t, "old": old})

    await advance()
    return res


async def _complete(router: ModelRouter, model: str, msgs: list[Message]) -> tuple[str, Usage]:
    from agentkit.llm.base import LLMRequest  # noqa: PLC0415

    text: list[str] = []
    usage = Usage()
    # Без response_format: не все модели OpenRouter его принимают, а упавший запрос стоил бы
    # прохода памяти. JSON достаём из текста (_parse), мусор — просто «записывать нечего».
    req = LLMRequest(model=model, messages=msgs, max_tokens=1500, temperature=0)
    async for ch in router.stream(req):
        if ch.kind == "done" and ch.response:
            text.append(ch.response.message.text)
            usage = ch.response.usage
    return "".join(text), usage
