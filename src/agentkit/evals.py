"""Evals — регрессионные проверки поведения агента.

Без них любая правка промпта или описания инструмента — лотерея: «вроде
стало лучше» на одном примере и тихая поломка на пяти других. Набор
кейсов в YAML прогоняется до и после изменения.

Кейс:
    - id: remember-rule
      input: "Запомни: отчёты присылай по пятницам"
      mode: auto                  # режим прав на время кейса
      approve: all                # all | none — что отвечать на карточки подтверждения
      expect:
        status: done
        tools_called: [memory_save]
        tools_not_called: [shell]
        contains: ["запомнил"]    # в итоговом ответе, без учёта регистра
        not_contains: []
        max_steps: 5
        judge: "Ответ подтверждает, что правило сохранено, и не обещает лишнего"
      script:                     # необязательно: сценарий FakeLLM — кейс идёт офлайн и бесплатно
        - tool_calls: [[memory_save, {text: "Отчёты — по пятницам"}]]
        - "Запомнил."

Кейсы со script проверяют проводку (инструменты, права, хуки), кейсы без
него — реальное поведение модели. Судья (judge) — модель роли «small».
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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


@dataclass
class Report:
    suite: str
    results: list[CaseResult]

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {"suite": self.suite, "passed": self.passed, "total": len(self.results),
                "results": [r.__dict__ for r in self.results]}

    def table(self) -> str:
        lines = [f"{self.suite}: {self.passed}/{len(self.results)}"]
        for r in self.results:
            mark = "✓" if r.passed else "✗"
            lines.append(f"  {mark} {r.id}  [{r.status}, {r.steps} шагов, ${r.usage.get('cost', 0):.4f}, {r.seconds:.1f} с]")
            for f in r.failures:
                lines.append(f"      – {f}")
        return "\n".join(lines)


def load_suite(path: str | Path) -> tuple[str, list[dict[str, Any]]]:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return p.stem, data
    return data.get("name", p.stem), data.get("cases", [])


async def _judge(agent: Agent, rubric: str, question: str, answer: str) -> tuple[bool, str]:
    msgs = [
        Message.system("Ты проверяешь ответ ИИ-агента по критерию. Ответь строго JSON: "
                       '{"pass": true|false, "why": "кратко"}'),
        Message.user(f"Критерий: {rubric}\n\nЗапрос пользователя:\n{question}\n\nОтвет агента:\n{answer}"),
    ]
    text = ""
    async for ch in agent.router.stream(LLMRequest(model="small", messages=msgs, max_tokens=400)):
        if ch.kind == "done" and ch.response:
            text = ch.response.message.text
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        d = json.loads(text[start:end])
        return bool(d.get("pass")), str(d.get("why", ""))
    except ValueError:
        return False, f"судья ответил не JSON: {text[:200]}"


async def run_case(case: dict[str, Any], make_agent: MakeAgent) -> CaseResult:
    t0 = time.monotonic()
    store = await SQLStore().init()
    fake = FakeLLM(case["script"], default=None) if case.get("script") else None
    agent = make_agent(store, fake)
    if hasattr(agent, "__await__"):
        agent = await agent  # type: ignore[misc]
    user = User(id=case.get("user", "eval"), name="Eval", roles=case.get("roles", []))
    session = await store.create_session(user.id, state={"mode": case.get("mode")} if case.get("mode") else {})
    expect = case.get("expect", {})
    tools: list[str] = []
    usage = Usage()
    steps = 0
    status = RunStatus.DONE
    text = ""

    async def consume(gen) -> None:
        nonlocal status, text, steps
        async for ev in gen:
            if ev.type == EventType.TOOL_CALL and not ev.agent:
                tools.append(ev.data["name"])
            elif ev.type == EventType.USAGE:
                usage.add(Usage(**{k: v for k, v in ev.data.items() if k in Usage.__dataclass_fields__}))
                steps += 0 if ev.agent else 1
            elif ev.type == EventType.MESSAGE and not ev.agent and ev.data.get("content"):
                text = ev.data["content"]
            elif ev.type == EventType.RUN_FINISHED:
                status = RunStatus(ev.data["status"])

    try:
        await consume(agent.run(session.id, user, case["input"]))
        rounds = 0
        while status == RunStatus.WAITING_APPROVAL and rounds < 10:
            rounds += 1
            approve = case.get("approve", "all") == "all"
            pending = await store.list_approvals(session.id, status="pending")
            for rec in pending:
                await consume(agent.resolve_approval(rec.id, user, approve=approve))
    finally:
        await store.close()

    failures: list[str] = []
    if (want := expect.get("status")) and str(status) != want:
        failures.append(f"статус {status}, ожидался {want}")
    for t in expect.get("tools_called", []):
        if t not in tools:
            failures.append(f"не вызван {t} (вызваны: {', '.join(tools) or 'ничего'})")
    for t in expect.get("tools_not_called", []):
        if t in tools:
            failures.append(f"вызван запрещённый {t}")
    low = text.lower()
    for s in expect.get("contains", []):
        if s.lower() not in low:
            failures.append(f"в ответе нет «{s}»")
    for s in expect.get("not_contains", []):
        if s.lower() in low:
            failures.append(f"в ответе есть «{s}»")
    if (mx := expect.get("max_steps")) and steps > mx:
        failures.append(f"{steps} шагов > {mx}")
    if rubric := expect.get("judge"):
        if fake is not None:
            pass  # офлайн-кейс: судить нечем и незачем
        else:
            ok, why = await _judge(agent, rubric, case["input"], text)
            if not ok:
                failures.append(f"судья: {why}")
    return CaseResult(id=case.get("id", case["input"][:30]), passed=not failures, failures=failures, text=text,
                      tools=tools, status=str(status), steps=steps, usage=usage.to_dict(),
                      seconds=time.monotonic() - t0)


async def run_suite(path: str | Path, make_agent: MakeAgent, *, only: str | None = None) -> Report:
    name, cases = load_suite(path)
    results = []
    for case in cases:
        if only and case.get("id") != only:
            continue
        results.append(await run_case(case, make_agent))
    return Report(suite=name, results=results)
