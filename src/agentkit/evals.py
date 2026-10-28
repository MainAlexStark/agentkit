"""Evals — регрессионные проверки поведения агента.

Без них любая правка промпта, описания инструмента или context.py — лотерея:
«вроде стало лучше» на одном примере и тихая поломка на пяти других. Набор
кейсов в YAML прогоняется до и после изменения, на нескольких моделях и по
нескольку раз (модель недетерминирована: один зелёный прогон ничего не значит).

Кейс:
    - id: fix-average
      tags: [files, code]
      input: "Тест падает — найди и исправь ошибку в calc.py"
      turns: ["А теперь добавь тест на пустой список"]   # следующие сообщения, после ответа на предыдущее
      answers: ["Да, в той же папке"]                 # ответы на ask_user по порядку
      history:                                        # разговор до кейса (user / assistant)
        - {role: user, content: "Проект — калькулятор для склада"}
        - {role: assistant, content: "Понял."}
      memory: ["Отвечать кратко"]                     # память пользователя до старта
      files:                                          # рабочая папка до старта
        calc.py: "def average(xs): ..."
        app.log: {lines: 3000, line: "INFO запрос {i} ok", insert: {2711: "ERROR code=E-4471"}}
      mode: auto                  # режим прав на время кейса
      approve: all                # all | none — что отвечать на карточки подтверждения
      tool_policy: {shell: deny}  # выбор пользователя по инструментам
      deny: ["shell(rm *)"]       # правила сервиса поверх агента
      config: {context_window: 6000, max_output_chars: 2000}   # поля AgentConfig
      max_steps: 10               # лимит шагов агента в этом кейсе
      timeout: 300                # секунд на кейс
      expect:
        status: done
        tools_called: [edit_file]
        tools_called_any: [read_file, grep]      # хотя бы один
        tools_not_called: [write_file]
        tool_args: [{tool: shell, args: {command: "test_calc"}}]  # подстрока в аргументе
        max_tool_calls: 8
        max_tool_errors: 2
        asked_user: false
        compacted: true           # история сжималась
        contains: ["исправил"]    # в итоговом ответе, без учёта регистра
        not_contains: []
        regex: "len\\(xs\\)"
        max_steps: 6
        max_cost: 0.05
        files:                    # рабочая папка после
          calc.py: {contains: ["len(xs)"], not_contains: ["len(xs) - 1"]}
          old.txt: {exists: false}
        judge: "Ответ объясняет, в чём была ошибка, и подтверждает проверку запуском"
      script:                     # необязательно: сценарий FakeLLM — кейс идёт офлайн и бесплатно
        - tool_calls: [[read_file, {path: calc.py}]]
        - "Исправил."

Кейсы со script проверяют проводку (инструменты, права, хуки), кейсы без
него — реальное поведение модели. Судья (judge) — модель роли «small» или
та, что передана в judge_model.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

from agentkit.agent import Agent
from agentkit.events import EventType, RunStatus
from agentkit.llm.base import LLMRequest
from agentkit.llm.fake import FakeLLM
from agentkit.messages import Message, Usage
from agentkit.store.sql import SQLStore
from agentkit.tools.base import User

MakeAgent = Callable[[SQLStore, FakeLLM | None], Agent | Awaitable[Agent]]

WAITING = (RunStatus.WAITING_APPROVAL, RunStatus.WAITING_INPUT)


@dataclass
class CaseResult:
    id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    text: str = ""
    tools: list[str] = field(default_factory=list)
    status: str = ""
    steps: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    model: str = ""
    attempt: int = 0
    tags: list[str] = field(default_factory=list)
    tool_errors: int = 0
    # Полный журнал прогона — для --save-runs и `agentkit replay`; в JSON отчёта не попадает.
    events: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d.pop("events", None)
        return d


@dataclass
class Report:
    suite: str
    results: list[CaseResult]

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {"suite": self.suite, "passed": self.passed, "total": len(self.results),
                "summary": summarize(self.results), "results": [r.to_dict() for r in self.results]}

    def table(self) -> str:
        lines = [f"{self.suite}: {self.passed}/{len(self.results)}"]
        models = list(dict.fromkeys(r.model for r in self.results))
        for m in models:
            rs = [r for r in self.results if r.model == m]
            if len(models) > 1 or m:
                lines.append(f" {m or 'модель по умолчанию'}: {sum(r.passed for r in rs)}/{len(rs)}")
            for r in rs:
                mark = "✓" if r.passed else "✗"
                rep = f" #{r.attempt + 1}" if any(x.attempt for x in rs) else ""
                lines.append(f"  {mark} {r.id}{rep}  [{r.status}, {r.steps} шагов, ${r.usage.get('cost', 0):.4f}, "
                             f"{r.seconds:.1f} с]")
                for f in r.failures:
                    lines.append(f"      – {f}")
        return "\n".join(lines)


# ── загрузка ──────────────────────────────────────────────────────────

def load_suite(path: str | Path) -> tuple[str, list[dict[str, Any]]]:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return p.stem, data
    return data.get("name", p.stem), data.get("cases", [])


def suite_files(path: str | Path) -> list[Path]:
    p = Path(path)
    if p.is_dir():
        return sorted([*p.glob("*.yaml"), *p.glob("*.yml")])
    return [p]


def make_text(v: Any) -> str:
    """Текст файла или сообщения из кейса: строка как есть или генератор.

    Генератор нужен для длинных входов (лог на 3000 строк, «болтовня» для сжатия
    истории): писать их в YAML руками бессмысленно, а без них не проверить
    обрезку вывода и сжатие контекста.
        {lines: 3000, line: "INFO {i}", insert: {2711: "ERROR …"}}   # строки, нумерация с 1
        {repeat: "бла ", times: 500}
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        if "repeat" in v:
            return str(v["repeat"]) * int(v.get("times", 1))
        if "lines" in v:
            tpl = str(v.get("line", "{i}"))
            ins = {int(k): str(t) for k, t in (v.get("insert") or {}).items()}
            return "\n".join(ins.get(i, tpl.format(i=i)) for i in range(1, int(v["lines"]) + 1)) + "\n"
    return json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v


# ── прогон кейса ──────────────────────────────────────────────────────

async def _judge(agent: Agent, rubric: str, question: str, answer: str, tools: list[str],
                 model: str = "small") -> tuple[bool, str]:
    msgs = [
        # Без «по смыслу» судья (small-модель) валил верные ответы за другие слова: «заблокировано»
        # вместо «запрещено правилами» — и набор шумел сильнее, чем модель под тестом.
        Message.system("Ты проверяешь ответ ИИ-агента по критерию. Суди по смыслу: критерий выполнен, если "
                       "ответ делает то, что в нём требуется, даже другими словами; не придирайся к стилю и "
                       "не требуй того, чего в критерии нет. Ответь строго JSON: "
                       "{\"pass\": true|false, \"why\": \"кратко\"}"),
        Message.user(f"Критерий: {rubric}\n\nЗапрос пользователя:\n{question}\n\n"
                     f"Инструменты, которые агент вызвал: {', '.join(tools) or 'никаких'}\n\n"
                     f"Итоговый ответ агента:\n{answer}"),
    ]
    text = ""
    async for ch in agent.router.stream(LLMRequest(model=model, messages=msgs, max_tokens=400)):
        if ch.kind == "done" and ch.response:
            text = ch.response.message.text
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        d = json.loads(text[start:end])
        return bool(d.get("pass")), str(d.get("why", ""))
    except ValueError:
        return False, f"судья ответил не JSON: {text[:200]}"


def _arg_match(want: Any, got: Any) -> bool:
    if isinstance(want, str):
        return want.lower() in (got if isinstance(got, str) else json.dumps(got, ensure_ascii=False)).lower()
    return want == got


def _check_files(ws: Path | None, spec: dict[str, Any]) -> list[str]:
    if ws is None:
        return ["у агента нет рабочей папки — проверять файлы негде"]
    out: list[str] = []
    for rel, want in spec.items():
        p = ws / rel
        if isinstance(want, str):
            want = {"contains": [want]}
        elif isinstance(want, list):
            want = {"contains": want}
        exists = p.is_file()
        if want.get("exists") is False:
            if exists:
                out.append(f"файл {rel} остался, а не должен")
            continue
        if not exists:
            out.append(f"нет файла {rel}")
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for s in _as_list(want.get("contains")):
            if s not in text:
                out.append(f"в {rel} нет «{s}»")
        for s in _as_list(want.get("not_contains")):
            if s in text:
                out.append(f"в {rel} осталось «{s}»")
        if "equals" in want and text.strip() != str(want["equals"]).strip():
            out.append(f"{rel} не совпадает с ожидаемым: {text[:120]!r}")
        if (rx := want.get("regex")) and not re.search(rx, text, re.M):
            out.append(f"в {rel} нет совпадения с /{rx}/")
    return out


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


async def run_case(case: dict[str, Any], make_agent: MakeAgent, *, model: str | None = None, attempt: int = 0,
                   timeout: float | None = None, judge_model: str = "small") -> CaseResult:
    t0 = time.monotonic()
    store = await SQLStore().init()
    fake = FakeLLM(case["script"], default=None) if case.get("script") else None
    agent = make_agent(store, fake)
    if hasattr(agent, "__await__"):
        agent = await agent  # type: ignore[misc]
    if case.get("config"):
        agent.config = replace(agent.config, **case["config"])
    if case.get("max_steps"):
        agent.spec = replace(agent.spec, max_steps=int(case["max_steps"]))
    if case.get("deny"):
        agent.permissions = replace(agent.permissions, deny=[*agent.permissions.deny, *case["deny"]])
    user = User(id=case.get("user", "eval"), name="Eval", roles=case.get("roles", []))
    state: dict[str, Any] = {}
    if case.get("mode"):
        state["mode"] = case["mode"]
    if model and fake is None:
        state["model"] = model
    session = await store.create_session(user.id, state=state)
    cid = case.get("id", case["input"][:30])
    expect = case.get("expect", {})
    tools: list[str] = []
    calls: list[tuple[str, dict]] = []
    events: list[dict[str, Any]] = []
    usage = Usage()
    counters = {"steps": 0, "tool_errors": 0, "asked": 0, "compacted": 0}
    status = RunStatus.DONE
    text = ""
    failures: list[str] = []
    ws = agent.workspace_for(session.id)

    async def consume(gen) -> None:
        nonlocal status, text
        async for ev in gen:
            events.append(ev.to_dict())
            main = not ev.agent
            if ev.type == EventType.TOOL_CALL and main:
                tools.append(ev.data["name"])
                calls.append((ev.data["name"], ev.data.get("args") or {}))
            elif ev.type == EventType.TOOL_RESULT and main and ev.data.get("is_error"):
                counters["tool_errors"] += 1
            elif ev.type == EventType.USAGE:
                usage.add(Usage(**{k: v for k, v in ev.data.items() if k in Usage.__dataclass_fields__}))
                counters["steps"] += 1 if main else 0
            elif ev.type == EventType.ASK_USER and main:
                counters["asked"] += 1
            elif ev.type == EventType.COMPACTED and main:
                counters["compacted"] += 1
            elif ev.type == EventType.MESSAGE and main and ev.data.get("content"):
                text = ev.data["content"]
            elif ev.type == EventType.RUN_FINISHED and main:
                status = RunStatus(ev.data["status"])

    async def drive() -> None:
        nonlocal status
        answers = [make_text(a) for a in case.get("answers", [])]
        for inp in [case["input"], *case.get("turns", [])]:
            await consume(agent.run(session.id, user, make_text(inp)))
            rounds = 0
            while status in WAITING and rounds < 10:
                rounds += 1
                if status == RunStatus.WAITING_APPROVAL:
                    approve = case.get("approve", "all") == "all"
                    for rec in await store.list_approvals(session.id, status="pending"):
                        await consume(agent.resolve_approval(rec.id, user, approve=approve))
                elif answers:
                    await consume(agent.run(session.id, user, answers.pop(0)))
                else:
                    break  # вопрос без заготовленного ответа — кейс кончается на нём

    try:
        for m in case.get("memory", []):
            await store.memory_add(f"user:{user.id}", make_text(m), created_by=user.id)
        for h in case.get("history", []):
            role = h.get("role", "user")
            await store.append_message(session.id, Message(role=role, content=make_text(h.get("content"))))
        if case.get("tool_policy"):
            await store.kv_set("tool_policy", user.id, dict(case["tool_policy"]))
        if case.get("files"):
            if ws is None:
                failures.append("в кейсе есть files, а у агента нет рабочей папки")
            else:
                for rel, content in case["files"].items():
                    p = ws / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(make_text(content), encoding="utf-8")
        limit = case.get("timeout", timeout)
        try:
            await asyncio.wait_for(drive(), limit) if limit else await drive()
        except TimeoutError:
            failures.append(f"не уложился в {limit:.0f} с")
            status = RunStatus.CANCELLED
        except Exception as e:  # noqa: BLE001 — упавший кейс не должен ронять весь набор
            failures.append(f"кейс упал: {type(e).__name__}: {e}")
            status = RunStatus.ERROR

        if (want := expect.get("status")) and str(status) != want:
            failures.append(f"статус {status}, ожидался {want}")
        for t in expect.get("tools_called", []):
            if t not in tools:
                failures.append(f"не вызван {t} (вызваны: {', '.join(tools) or 'ничего'})")
        if (anyof := expect.get("tools_called_any")) and not any(t in tools for t in anyof):
            failures.append(f"не вызван ни один из {', '.join(anyof)}")
        for t in expect.get("tools_not_called", []):
            if any(fnmatch(x, t) for x in tools):
                failures.append(f"вызван запрещённый {t}")
        for want in expect.get("tool_args", []):
            if not any(n == want["tool"] and all(_arg_match(v, a.get(k)) for k, v in (want.get("args") or {}).items())
                       for n, a in calls):
                failures.append(f"нет вызова {want['tool']} с {json.dumps(want.get('args'), ensure_ascii=False)}")
        if (mx := expect.get("max_tool_calls")) is not None and len(tools) > mx:
            failures.append(f"{len(tools)} вызовов инструментов > {mx}")
        if (mx := expect.get("max_tool_errors")) is not None and counters["tool_errors"] > mx:
            failures.append(f"{counters['tool_errors']} ошибок инструментов > {mx}")
        if "asked_user" in expect and bool(counters["asked"]) != bool(expect["asked_user"]):
            failures.append("агент не задал вопрос" if expect["asked_user"] else "агент задал вопрос, а не должен был")
        if "compacted" in expect and bool(counters["compacted"]) != bool(expect["compacted"]):
            failures.append("история не сжималась" if expect["compacted"] else "история сжималась")
        low = text.lower()
        for s in _as_list(expect.get("contains")):
            if s.lower() not in low:
                failures.append(f"в ответе нет «{s}»")
        for s in _as_list(expect.get("not_contains")):
            if s.lower() in low:
                failures.append(f"в ответе есть «{s}»")
        if (rx := expect.get("regex")) and not re.search(rx, text, re.I | re.M):
            failures.append(f"ответ не совпал с /{rx}/")
        if (mx := expect.get("max_steps")) and counters["steps"] > mx:
            failures.append(f"{counters['steps']} шагов > {mx}")
        if (mx := expect.get("max_cost")) is not None and usage.cost > mx:
            failures.append(f"стоимость ${usage.cost:.4f} > ${mx}")
        if expect.get("files"):
            failures += _check_files(ws, expect["files"])
        if (rubric := expect.get("judge")) and fake is None and not failures:
            # Судья — только если механические проверки прошли: дешевле и не путает причины.
            try:
                ok, why = await _judge(agent, rubric, make_text(case["input"]), text, tools, judge_model)
            except Exception as e:  # noqa: BLE001
                ok, why = False, f"судья недоступен: {e}"
            if not ok:
                failures.append(f"судья: {why}")
    finally:
        await store.close()

    return CaseResult(id=cid, passed=not failures, failures=failures, text=text, tools=tools, status=str(status),
                      steps=counters["steps"], usage=usage.to_dict(), seconds=round(time.monotonic() - t0, 2),
                      model=model or "", attempt=attempt, tags=list(case.get("tags", [])),
                      tool_errors=counters["tool_errors"], events=events)


def select_cases(cases: list[dict[str, Any]], *, only: str | None = None, tags: list[str] | None = None,
                 offline: bool = False, live: bool = False) -> list[dict[str, Any]]:
    out = []
    for c in cases:
        if only and not fnmatch(c.get("id", ""), only):
            continue
        if tags and not set(tags) & set(c.get("tags", [])):
            continue
        if offline and not c.get("script"):
            continue
        if live and c.get("script"):
            continue
        out.append(c)
    return out


async def run_cases(cases: list[dict[str, Any]], make_agent: MakeAgent, *, models: list[str | None] | None = None,
                    repeat: int = 1, parallel: int = 1, timeout: float | None = None, judge_model: str = "small",
                    on_result: Callable[[CaseResult], None] | None = None) -> list[CaseResult]:
    """Все кейсы × модели × повторы; parallel — сколько одновременно (у живых моделей кейс идёт минутами)."""
    jobs = [(c, m, a) for m in (models or [None]) for c in cases for a in range(repeat)]
    sem = asyncio.Semaphore(max(1, parallel))
    results: list[CaseResult | None] = [None] * len(jobs)

    async def one(i: int, c: dict, m: str | None, a: int) -> None:
        async with sem:
            # Офлайн-кейсу модель не нужна: прогонять его на каждой модели незачем.
            if c.get("script") and m is not None and m != (models or [None])[0]:
                return
            r = await run_case(c, make_agent, model=m, attempt=a, timeout=timeout, judge_model=judge_model)
            results[i] = r
            if on_result:
                on_result(r)

    await asyncio.gather(*(one(i, c, m, a) for i, (c, m, a) in enumerate(jobs)))
    return [r for r in results if r is not None]


async def run_suite(path: str | Path, make_agent: MakeAgent, *, only: str | None = None, **kw: Any) -> Report:
    name, cases = load_suite(path)
    return Report(suite=name, results=await run_cases(select_cases(cases, only=only), make_agent, **kw))


# ── сводка, сравнение, markdown ───────────────────────────────────────

def summarize(results: list[CaseResult] | list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """По моделям: доля успешных прогонов, pass^k (кейс прошёл во всех повторах), стоимость, шаги, время."""
    rows = [r.to_dict() if isinstance(r, CaseResult) else r for r in results]
    out: dict[str, dict[str, Any]] = {}
    for m in dict.fromkeys(r.get("model", "") for r in rows):
        rs = [r for r in rows if r.get("model", "") == m]
        by_case: dict[str, list[bool]] = {}
        for r in rs:
            by_case.setdefault(r["id"], []).append(bool(r["passed"]))
        n = len(rs) or 1
        cost = sum(r.get("usage", {}).get("cost", 0) for r in rs)
        out[m] = {
            "runs": len(rs), "passed": sum(r["passed"] for r in rs),
            "pass_rate": round(sum(r["passed"] for r in rs) / n, 4),
            "cases": len(by_case), "pass_all": round(sum(all(v) for v in by_case.values()) / (len(by_case) or 1), 4),
            "cost": round(cost, 6), "cost_per_run": round(cost / n, 6),
            "avg_steps": round(sum(r.get("steps", 0) for r in rs) / n, 2),
            "avg_seconds": round(sum(r.get("seconds", 0) for r in rs) / n, 2),
            "tool_errors": sum(r.get("tool_errors", 0) for r in rs),
        }
    return out


def load_report(path: str | Path) -> list[dict[str, Any]]:
    """JSON от `agentkit eval --json`: список отчётов по наборам."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("suites", [data])


def compare(current: list[dict[str, Any]], baseline: list[dict[str, Any]]) -> dict[str, Any]:
    """Что сломалось и что починилось относительно прошлого отчёта: по (набор, модель, кейс)."""

    def rates(reports: list[dict[str, Any]]) -> dict[tuple[str, str, str], float]:
        acc: dict[tuple[str, str, str], list[bool]] = {}
        for rep in reports:
            for r in rep.get("results", []):
                acc.setdefault((rep["suite"], r.get("model", ""), r["id"]), []).append(bool(r["passed"]))
        return {k: sum(v) / len(v) for k, v in acc.items()}

    cur, base = rates(current), rates(baseline)
    common = cur.keys() & base.keys()
    regressions = sorted(k for k in common if cur[k] < base[k])
    fixed = sorted(k for k in common if cur[k] > base[k])
    n = len(common) or 1
    return {
        "regressions": [{"suite": s, "model": m, "id": i, "was": base[(s, m, i)], "now": cur[(s, m, i)]}
                        for s, m, i in regressions],
        "fixed": [{"suite": s, "model": m, "id": i, "was": base[(s, m, i)], "now": cur[(s, m, i)]}
                  for s, m, i in fixed],
        "new": len(cur.keys() - base.keys()),
        "pass_rate_now": round(sum(cur[k] for k in common) / n, 4),
        "pass_rate_was": round(sum(base[k] for k in common) / n, 4),
    }


def markdown(reports: list[dict[str, Any]], diff: dict[str, Any] | None = None) -> str:
    """Сводка для GitHub (шаг job summary) или для чата."""
    rows = [r for rep in reports for r in rep.get("results", [])]
    lines = ["## Evals", "", "| Модель | Прошло | pass^k | Стоимость | $ / прогон | Шагов | Секунд | Ошибок инстр. |",
             "|---|---|---|---|---|---|---|---|"]
    for m, s in summarize(rows).items():
        lines.append(f"| {m or 'по умолчанию'} | {s['passed']}/{s['runs']} ({s['pass_rate']:.0%}) | {s['pass_all']:.0%} "
                     f"| ${s['cost']:.4f} | ${s['cost_per_run']:.4f} | {s['avg_steps']} | {s['avg_seconds']} "
                     f"| {s['tool_errors']} |")
    failed = [(rep["suite"], r) for rep in reports for r in rep.get("results", []) if not r["passed"]]
    if failed:
        lines += ["", "### Не прошли", ""]
        for suite, r in failed:
            rep_n = f" #{r.get('attempt', 0) + 1}" if r.get("attempt") else ""
            why = "; ".join(r.get("failures", []))[:300]
            lines.append(f"- `{r.get('model') or '—'}` · `{suite}/{r['id']}`{rep_n} — {why}")
    if diff:
        lines += ["", f"### Против baseline: {diff['pass_rate_was']:.0%} → {diff['pass_rate_now']:.0%}", ""]
        for x in diff["regressions"]:
            lines.append(f"- ✗ сломалось `{x['model'] or '—'}` · `{x['suite']}/{x['id']}` ({x['was']:.0%} → {x['now']:.0%})")
        for x in diff["fixed"]:
            lines.append(f"- ✓ починилось `{x['model'] or '—'}` · `{x['suite']}/{x['id']}` ({x['was']:.0%} → {x['now']:.0%})")
        if not diff["regressions"] and not diff["fixed"]:
            lines.append("Без изменений.")
    return "\n".join(lines) + "\n"


def save_run(directory: str | Path, suite: str, r: CaseResult) -> Path:
    """Журнал прогона кейса в JSONL — тот же формат, что у JSONLTracer: читается `agentkit replay`."""
    slug = re.sub(r"[^\w.-]+", "_", r.model or "default")
    p = Path(directory) / slug / f"{suite}__{r.id}__{r.attempt + 1}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for ev in r.events:
            f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
    return p
