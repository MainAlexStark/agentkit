from __future__ import annotations

import asyncio
import time
from typing import Annotated

from conftest import collect, make_agent, status, types

from agentkit import (
    AgentConfig,
    AgentSpec,
    FakeLLM,
    HookEvent,
    HookResult,
    Hooks,
    Mode,
    Permissions,
    ToolRegistry,
    tool,
)
from agentkit.permissions import Decision
from agentkit.tools.core import core_tools


@tool(read_only=True)
async def lookup(ctx, key: Annotated[str, "ключ"]) -> str:
    """Читает значение."""
    return f"value-of-{key}"


CALLS: list[str] = []


@tool()
async def set_price(ctx, sku: str, price: float) -> str:
    """Меняет цену."""
    CALLS.append(f"{sku}={price}")
    return f"цена {sku} = {price}"


def reg(*extra):
    return ToolRegistry([*core_tools(), lookup, set_price, *extra])


async def test_plain_answer(store, user):
    fake = FakeLLM(["Привет!"])
    agent = make_agent(store, fake, tools=reg())
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "Привет"))
    assert status(evs) == "done"
    assert "text_delta" in types(evs)
    msgs = await store.list_messages(s.id)
    assert [m.role for _, m in msgs] == ["user", "assistant"]
    assert (await store.get_session(s.id)).title == "Привет"
    # системный промпт собран: правила ядра + окружение
    sys = fake.requests[0].messages[0].text
    assert "Как ты работаешь" in sys and "Окружение" in sys and "Алекс" in sys


async def test_read_tool_then_answer(store, user):
    fake = FakeLLM([{"tool_calls": [("lookup", {"key": "a"})]}, "Значение: value-of-a"])
    agent = make_agent(store, fake, tools=reg())
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "?"))
    assert status(evs) == "done"
    res = [e for e in evs if str(e.type) == "tool_result"][0]
    assert res.data["content"] == "value-of-a"
    # во втором запросе модель видит результат инструмента
    roles = [m.role for m in fake.requests[1].messages]
    assert roles == ["system", "user", "assistant", "tool"]


async def test_write_needs_approval_then_continues(store, user):
    CALLS.clear()
    fake = FakeLLM([{"tool_calls": [("set_price", {"sku": "X", "price": 10})]}, "Цена изменена."])
    agent = make_agent(store, fake, tools=reg())
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "поставь цену"))
    assert status(evs) == "waiting_approval"
    assert CALLS == []
    ap = [e for e in evs if str(e.type) == "approval_required"][0]
    assert ap.data["view"] == [["sku", "X"], ["price", "10.0"]]
    evs2 = await collect(agent.resolve_approval(ap.data["approval_id"], user, approve=True))
    assert CALLS == ["X=10.0"]
    assert status(evs2) == "done"
    assert (await store.get_approval(ap.data["approval_id"])).status == "approved"


async def test_reject(store, user):
    CALLS.clear()
    fake = FakeLLM([{"tool_calls": [("set_price", {"sku": "X", "price": 10})]}, "Ок, не меняю."])
    agent = make_agent(store, fake, tools=reg())
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "поставь цену"))
    aid = [e for e in evs if str(e.type) == "approval_required"][0].data["approval_id"]
    await collect(agent.resolve_approval(aid, user, approve=False, comment="дорого"))
    assert CALLS == []
    tool_msgs = [m for _, m in await store.list_messages(s.id) if m.role == "tool"]
    assert "отклонил" in tool_msgs[0].text and "дорого" in tool_msgs[0].text


async def test_new_message_expires_pending(store, user):
    fake = FakeLLM([{"tool_calls": [("set_price", {"sku": "X", "price": 1})]}, "Понял, другое."])
    agent = make_agent(store, fake, tools=reg())
    s = await store.create_session(user.id)
    await collect(agent.run(s.id, user, "поставь"))
    evs = await collect(agent.run(s.id, user, "нет, лучше расскажи"))
    assert status(evs) == "done"
    assert not await store.list_approvals(s.id, status="pending")
    # история валидна: на каждый вызов есть результат
    roles = [m.role for m in fake.requests[-1].messages]
    assert roles == ["system", "user", "assistant", "tool", "user"]


async def test_auto_mode_and_plan_mode(store, user):
    CALLS.clear()
    fake = FakeLLM([{"tool_calls": [("set_price", {"sku": "A", "price": 2})]}, "Готово"])
    agent = make_agent(store, fake, tools=reg())
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "x", mode="auto"))
    assert status(evs) == "done" and CALLS == ["A=2.0"]

    fake.push({"tool_calls": [("set_price", {"sku": "B", "price": 3})]}, "План: …")
    evs = await collect(agent.run(s.id, user, "y", mode="plan"))
    res = [e for e in evs if str(e.type) == "tool_result"][0]
    assert res.data["is_error"] and "планирования" in res.data["content"]
    assert CALLS == ["A=2.0"]


async def test_invalid_args_are_reported_to_model(store, user):
    fake = FakeLLM([{"tool_calls": [("lookup", {"nope": 1})]}, {"tool_calls": [("lookup", {"key": "k"})]}, "ok"])
    agent = make_agent(store, fake, tools=reg())
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "?"))
    results = [e.data for e in evs if str(e.type) == "tool_result"]
    assert results[0]["is_error"] and "неверные аргументы" in results[0]["content"]
    assert results[1]["content"] == "value-of-k"


async def test_unknown_tool(store, user):
    fake = FakeLLM([{"tool_calls": [("nope", {})]}, "ok"])
    agent = make_agent(store, fake, tools=reg())
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "?"))
    assert "нет инструмента" in [e for e in evs if str(e.type) == "tool_result"][0].data["content"]


async def test_deferred_tools(store, user):
    @tool(read_only=True, deferred=True)
    async def secret_report(ctx) -> str:
        """Отчёт о продажах."""
        return "отчёт"

    fake = FakeLLM([
        {"tool_calls": [("secret_report", {})]},
        {"tool_calls": [("tool_search", {"query": "select:secret_report"})]},
        {"tool_calls": [("secret_report", {})]},
        "Вот отчёт",
    ])
    agent = make_agent(store, fake, tools=reg(secret_report))
    s = await store.create_session(user.id)
    # до загрузки схемы инструмента нет в запросе, но имя есть в промпте
    evs = await collect(agent.run(s.id, user, "отчёт"))
    first = fake.requests[0]
    assert "secret_report" not in [t["function"]["name"] for t in first.tools]
    assert "secret_report" in first.messages[0].text
    results = [e.data for e in evs if str(e.type) == "tool_result"]
    assert results[0]["is_error"] and "tool_search" in results[0]["content"]
    assert results[2]["content"] == "отчёт"
    assert "secret_report" in [t["function"]["name"] for t in fake.requests[3].tools]


async def test_hooks_deny_and_stop_continue(store, user):
    hooks = Hooks()

    @hooks.on(HookEvent.PRE_TOOL_USE, matcher="lookup")
    async def no_secret(payload, ctx):
        if payload["args"]["key"] == "secret":
            return HookResult(decision=Decision.DENY, reason="секреты не читаем")

    stops = []

    @hooks.on(HookEvent.STOP)
    async def check(payload, ctx):
        stops.append(payload["text"])
        if len(stops) == 1:
            return HookResult(continue_with="Проверь ещё раз.")

    fake = FakeLLM([{"tool_calls": [("lookup", {"key": "secret"})]}, "Первый итог", "Проверил, всё ок"])
    agent = make_agent(store, fake, tools=reg(), hooks=hooks)
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "?"))
    res = [e.data for e in evs if str(e.type) == "tool_result"][0]
    assert "секреты не читаем" in res["content"]
    assert stops == ["Первый итог", "Проверил, всё ок"]
    assert status(evs) == "done"


async def test_hook_can_rewrite_args_and_result(store, user):
    hooks = Hooks()
    hooks.add(HookEvent.PRE_TOOL_USE, lambda p, c: HookResult(updated_args={"key": p["args"]["key"].upper()}))
    hooks.add(HookEvent.POST_TOOL_USE, lambda p, c: HookResult(add_context="проверено аудитом"))
    fake = FakeLLM([{"tool_calls": [("lookup", {"key": "a"})]}, "ok"])
    agent = make_agent(store, fake, tools=reg(), hooks=hooks)
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "?"))
    content = [e.data for e in evs if str(e.type) == "tool_result"][0]["content"]
    assert content.startswith("value-of-A") and "проверено аудитом" in content


async def test_max_steps_forces_summary(store, user):
    fake = FakeLLM([{"tool_calls": [("lookup", {"key": str(i)})]} for i in range(2)] + ["Итог: не успел"])
    agent = make_agent(store, fake, tools=reg(), spec=AgentSpec(max_steps=3))
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "?"))
    assert fake.requests[2].tools == []
    assert "Лимит шагов" in fake.requests[2].messages[-1].text
    assert status(evs) == "max_steps"


async def test_parallel_read_tools(store, user):
    @tool(read_only=True)
    async def slow(ctx, n: int) -> str:
        """Медленно."""
        await asyncio.sleep(0.3)
        return str(n)

    fake = FakeLLM([{"tool_calls": [("slow", {"n": i}) for i in range(4)]}, "ok"])
    agent = make_agent(store, fake, tools=reg(slow))
    s = await store.create_session(user.id)
    t0 = time.monotonic()
    await collect(agent.run(s.id, user, "?"))
    assert time.monotonic() - t0 < 0.9


async def test_loop_guard(store, user):
    fake = FakeLLM([{"tool_calls": [("lookup", {"key": "a"})]}] * 3 + ["стоп"])
    agent = make_agent(store, fake, tools=reg())
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "?"))
    third = [e.data for e in evs if str(e.type) == "tool_result"][2]["content"]
    assert "одинаковый вызов" in third


async def test_truncation_and_read_output(store, user):
    @tool(read_only=True)
    async def big(ctx) -> str:
        """Много текста."""
        return "x" * 5000 + "END"

    fake = FakeLLM([{"tool_calls": [("big", {})]}, "ok"])
    agent = make_agent(store, fake, tools=reg(big), config=AgentConfig(max_output_chars=1000))
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "?"))
    content = [e.data for e in evs if str(e.type) == "tool_result"][0]["content"]
    assert "Вывод обрезан" in content
    bid = content.split('id="')[1].split('"')[0]
    full, _ = await store.blob_get(bid)
    assert full.endswith("END")


async def test_compaction(store, user):
    # Сжатие идёт на модели роли small («s»): по ней сценарий и различает вызовы.
    fake = FakeLLM(default=None)
    fake.script = [lambda r: "СВОДКА: обсуждали X" if r.model == "s" else "ответ"] * 10
    agent = make_agent(store, fake, tools=reg(), config=AgentConfig(context_window=1500, keep_user_turns=1))
    s = await store.create_session(user.id)
    evs = []
    for text in ("a" * 1500, "b" * 1500, "c"):
        evs += await collect(agent.run(s.id, user, text))
    assert "compacted" in types(evs)
    assert any(r.model == "s" for r in fake.requests)
    last = fake.requests[-1].messages
    assert "СВОДКА" in last[1].text
    assert all("a" * 100 not in m.text for m in last)


async def test_memory_in_prompt(store, user):
    fake = FakeLLM([{"tool_calls": [("memory_save", {"text": "Отчёты — по пятницам"})]}, "Запомнил", "ok"])
    from agentkit import standard_tools

    agent = make_agent(store, fake, tools=standard_tools(files=False, subagents=False))
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "запомни"))
    assert status(evs) == "done"
    s2 = await store.create_session(user.id)
    await collect(agent.run(s2.id, user, "привет"))
    assert "Отчёты — по пятницам" in fake.requests[-1].messages[0].text


async def test_global_memory_needs_approval_for_non_admin(store):
    from agentkit import User, standard_tools

    fake = FakeLLM([{"tool_calls": [("memory_save", {"text": "Для всех", "scope": "global"})]}])
    agent = make_agent(store, fake, tools=standard_tools(files=False, subagents=False))
    u = User(id="u2", roles=["worker"])
    s = await store.create_session(u.id)
    evs = await collect(agent.run(s.id, u, "запомни для всех"))
    assert status(evs) == "waiting_approval"


async def test_ask_user_stops_run(store, user):
    fake = FakeLLM([{"tool_calls": [("ask_user", {"question": "Какой?", "options": [{"label": "A"}, {"label": "B"}]})]}])
    agent = make_agent(store, fake, tools=reg())
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "сделай"))
    assert status(evs) == "waiting_input"
    assert "ask_user" in types(evs)


async def test_todo_event(store, user):
    fake = FakeLLM([{"tool_calls": [("todo_write", {"todos": [{"content": "a", "status": "in_progress"}]})]}, "ok"])
    agent = make_agent(store, fake, tools=reg())
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "?"))
    assert [e.data for e in evs if str(e.type) == "todo"][0]["todos"][0]["status"] == "in_progress"
    assert (await store.get_state(s.id))["todos"][0]["content"] == "a"


async def test_permission_rules():
    p = Permissions(allow=["set_price(*)"], deny=["lookup(secret*)"])
    assert p.check(lookup, {"key": "secret1"}).decision == Decision.DENY
    assert p.check(lookup, {"key": "x"}).decision == Decision.ALLOW
    assert p.check(set_price, {"sku": "a", "price": 1}).decision == Decision.ALLOW
    p2 = Permissions(mode=Mode.READONLY)
    assert p2.check(set_price, {"sku": "a", "price": 1}).decision == Decision.DENY
    p3 = Permissions(mode=Mode.BYPASS)

    @tool(needs_approval=True)
    async def danger(ctx) -> str:
        """."""
        return ""

    assert p3.check(danger, {}).decision == Decision.ALLOW
    assert Permissions(mode=Mode.AUTO).check(danger, {}).decision == Decision.ASK


async def test_cancel(store, user):
    @tool(read_only=True)
    async def wait(ctx) -> str:
        """Ждёт."""
        await asyncio.sleep(0.2)
        return "ok"

    fake = FakeLLM([{"tool_calls": [("wait", {})]}] * 50)
    agent = make_agent(store, fake, tools=reg(wait))
    s = await store.create_session(user.id)
    evs = []
    async for ev in agent.run(s.id, user, "?"):
        evs.append(ev)
        if str(ev.type) == "tool_result":
            agent.cancel(ev.run_id)
    assert status(evs) == "cancelled"


async def test_session_start_and_prompt_hooks(store, user):
    hooks = Hooks()
    hooks.add(HookEvent.SESSION_START, lambda p, c: HookResult(add_context="Клиент: ООО Ромашка"))
    hooks.add(HookEvent.USER_PROMPT_SUBMIT,
              lambda p, c: HookResult(decision=Decision.DENY, reason="спам") if "казино" in p["prompt"] else None)
    fake = FakeLLM(["ok", "ok2"])
    agent = make_agent(store, fake, tools=reg(), hooks=hooks)
    s = await store.create_session(user.id)
    await collect(agent.run(s.id, user, "привет"))
    assert any("Ромашка" in m.text for m in fake.requests[0].messages)
    evs = await collect(agent.run(s.id, user, "казино"))
    assert status(evs) == "error" and len(fake.requests) == 1
