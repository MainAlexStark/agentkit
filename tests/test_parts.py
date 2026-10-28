from __future__ import annotations

import json
import sys
import textwrap
import time

import httpx
import pytest
from conftest import collect, make_agent, status

from agentkit import AgentSpec, FakeLLM, MCPManager, MCPServerConfig, ModelRouter, standard_tools
from agentkit.evals import run_suite
from agentkit.llm.base import LLMError, LLMRequest
from agentkit.llm.openai_compat import OpenAICompatProvider, openrouter
from agentkit.llm.router import RetryPolicy, split_model
from agentkit.messages import Message
from agentkit.scheduler import Scheduler
from agentkit.skills import SkillLibrary
from agentkit.store.base import ScheduleRec


async def _nosleep(_):
    return None


# ── файлы и откат ───────────────────────────────────────────────────

async def test_files_and_rewind(store, user, tmp_path):
    (tmp_path / "a.txt").write_text("hello world\n")
    fake = FakeLLM([
        {"tool_calls": [("read_file", {"path": "a.txt"})]},
        {"tool_calls": [("edit_file", {"path": "a.txt", "old_string": "world", "new_string": "Kirov"})]},
        "Готово",
    ])
    agent = make_agent(store, fake, tools=standard_tools(subagents=False), workspace=tmp_path)
    s = await store.create_session(user.id, state={"mode": "accept_edits"})
    evs = await collect(agent.run(s.id, user, "поменяй"))
    assert status(evs) == "done"
    assert (tmp_path / "a.txt").read_text() == "hello Kirov\n"
    first_user_seq = (await store.list_messages(s.id))[0][0]
    res = await agent.rewind(s.id, first_user_seq)
    assert res["files_restored"] == ["a.txt"]
    assert (tmp_path / "a.txt").read_text() == "hello world\n"
    assert len(await store.list_messages(s.id)) == 1


async def test_path_escape_denied(store, user, tmp_path):
    fake = FakeLLM([{"tool_calls": [("read_file", {"path": "../../etc/passwd"})]}, "ok"])
    agent = make_agent(store, fake, tools=standard_tools(subagents=False), workspace=tmp_path)
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "?"))
    assert "вне рабочей папки" in [e for e in evs if str(e.type) == "tool_result"][0].data["content"]


# ── субагенты ───────────────────────────────────────────────────────

async def test_subagent(store, user):
    def script(req):
        sys_prompt = req.messages[0].text
        if "субагент-исследователь" in sys_prompt:
            return "Нашёл: ответ 42"
        if any(m.role == "tool" for m in req.messages):
            return "Итог: 42"
        return {"tool_calls": [("task", {"agent_type": "explore", "description": "найти ответ", "prompt": "найди"})]}

    fake = FakeLLM([script] * 5)
    agent = make_agent(store, fake, tools=standard_tools(files=False))
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "?"))
    assert status(evs) == "done"
    res = [e for e in evs if str(e.type) == "tool_result" and not e.agent][0]
    assert res.data["content"] == "Нашёл: ответ 42"
    assert "subagent_started" in [str(e.type) for e in evs]
    # субагент работает в дочерней сессии, которой нет в списке пользователя
    assert len(await store.list_sessions(user.id)) == 1
    assert len(await store.list_sessions(user.id, include_children=True)) == 2


# ── skills ──────────────────────────────────────────────────────────

async def test_skills(store, user, tmp_path):
    d = tmp_path / "skills" / "report"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: report\ndescription: Как писать отчёт\n---\nШаги отчёта: 1, 2, 3")
    (d / "template.md").write_text("# Шаблон")
    lib = SkillLibrary.from_dirs(tmp_path / "skills")
    fake = FakeLLM([{"tool_calls": [("skill", {"name": "report"})]}, "ok"])
    agent = make_agent(store, fake, tools=standard_tools(files=False, subagents=False), skills=lib)
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "отчёт"))
    assert "report: Как писать отчёт" in fake.requests[0].messages[0].text
    content = [e for e in evs if str(e.type) == "tool_result"][0].data["content"]
    assert "Шаги отчёта" in content and "template.md" in content


# ── MCP ─────────────────────────────────────────────────────────────

STDIO_SERVER = textwrap.dedent('''
    import json, sys
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        meth = m["method"]
        if meth == "initialize":
            r = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "t"}, "capabilities": {}, "instructions": "Тестовый сервер"}
        elif meth == "tools/list":
            r = {"tools": [
                {"name": "echo", "description": "Эхо", "inputSchema": {"type": "object", "properties": {"t": {"type": "string"}}, "required": ["t"]}, "annotations": {"readOnlyHint": True}},
                {"name": "wipe", "description": "Стирает", "inputSchema": {"type": "object"}, "annotations": {"destructiveHint": True}},
            ]}
        elif meth == "tools/call":
            r = {"content": [{"type": "text", "text": "эхо: " + m["params"]["arguments"].get("t", "")}]}
        else:
            r = {}
        print("log line to stdout that is not json")
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
''')


async def test_mcp_stdio(store, user, tmp_path):
    srv = tmp_path / "srv.py"
    srv.write_text(STDIO_SERVER)
    mgr = MCPManager([MCPServerConfig(name="t", command=sys.executable, args=[str(srv)])])
    tools = await mgr.connect()
    try:
        names = {t.name: t for t in tools}
        assert set(names) == {"mcp__t__echo", "mcp__t__wipe"}
        assert names["mcp__t__echo"].read_only and names["mcp__t__wipe"].destructive
        assert "Тестовый сервер" in mgr.instructions()
        fake = FakeLLM([{"tool_calls": [("mcp__t__echo", {"t": "hi"}), ("mcp__t__wipe", {})]}])
        agent = make_agent(store, fake, tools=standard_tools(files=False, subagents=False, extra=tools))
        s = await store.create_session(user.id)
        evs = await collect(agent.run(s.id, user, "?"))
        assert [e for e in evs if str(e.type) == "tool_result"][0].data["content"] == "эхо: hi"
        assert status(evs) == "waiting_approval"  # destructive — по подтверждению
    finally:
        await mgr.close()


async def test_mcp_http_sse_and_session():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append((body.get("method"), req.headers.get("mcp-session-id"), req.headers.get("authorization")))
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "protocolVersion": "2025-06-18", "serverInfo": {"name": "h"}}}, headers={"mcp-session-id": "S1"})
        if body["method"] == "tools/list":
            sse = "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {
                "tools": [{"name": f"t{i}", "inputSchema": {"type": "object"}} for i in range(20)]}}) + "\n\n"
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": []}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mgr = MCPManager([MCPServerConfig(name="h", url="http://x/mcp", token="tok")], http_client=client)
    tools = await mgr.connect()
    assert len(tools) == 20 and all(t.deferred for t in tools)  # >15 — отложенные автоматически
    assert seen[0] == ("initialize", None, "Bearer tok")
    assert seen[2] == ("tools/list", "S1", "Bearer tok")


async def test_mcp_dead_server_does_not_break():
    mgr = MCPManager([MCPServerConfig(name="dead", url="http://127.0.0.1:1/mcp", timeout=2)])
    assert await mgr.connect() == []
    assert not mgr.status["dead"].ok


# ── модели ──────────────────────────────────────────────────────────

def test_split_model():
    assert split_model("openrouter:anthropic/claude-sonnet-4.5", "x") == ("openrouter", "anthropic/claude-sonnet-4.5")
    assert split_model("anthropic/claude", "openrouter") == ("openrouter", "anthropic/claude")
    assert split_model("qwen3:32b", "ollama") == ("qwen3", "32b")  # без «/» двоеточие — провайдер
    assert split_model("ollama:qwen3:32b", "x") == ("ollama", "qwen3:32b")


async def test_router_retry_and_fallback():
    bad = FakeLLM([LLMError("429", retryable=True), LLMError("429", retryable=True)], name="bad", default=None)
    good = FakeLLM(["из запасной"], name="good")
    notes = []

    async def on_notice(t):
        notes.append(t)

    r = ModelRouter(providers={"bad": bad, "good": good}, fallbacks={"bad:m": ["good:m"]},
                    retry=RetryPolicy(attempts=2, base_delay=0), sleep=_nosleep)
    chunks = [c async for c in r.stream(LLMRequest(model="bad:m", messages=[Message.user("x")]), on_notice=on_notice)]
    assert chunks[-1].response.message.text == "из запасной"
    assert len(bad.requests) == 2 and any("запасную" in n for n in notes)


async def test_openai_compat_parsing():
    sse = "\n\n".join("data: " + json.dumps(c) for c in [
        {"model": "anthropic/claude-x", "choices": [{"delta": {"content": "При"}}]},
        {"choices": [{"delta": {"content": "вет", "reasoning": "думаю"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "look", "arguments": '{"k":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "v"}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0012,
                                  "prompt_tokens_details": {"cached_tokens": 80}}},
    ]) + "\n\ndata: [DONE]\n\n"
    captured = {}

    def handler(req):
        captured["body"] = json.loads(req.content)
        captured["auth"] = req.headers["authorization"]
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    p = OpenAICompatProvider(openrouter("KEY"), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    req = LLMRequest(model="anthropic/claude-x", messages=[Message.system("sys"), Message.user("hi")],
                     tools=[{"type": "function", "function": {"name": "look", "parameters": {}}}], reasoning_effort="low")
    chunks = [c async for c in p.stream(req)]
    resp = chunks[-1].response
    assert resp.message.text == "Привет"
    assert resp.message.tool_calls[0].arguments == {"k": "v"}
    assert resp.usage.cached_tokens == 80 and resp.usage.cost == 0.0012
    assert [c.kind for c in chunks].count("thinking") == 1
    body = captured["body"]
    assert captured["auth"] == "Bearer KEY"
    assert body["reasoning"] == {"effort": "low"} and body["usage"] == {"include": True}
    # cache_control на системном и последнем сообщении для моделей Anthropic
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-1]["content"][0]["cache_control"] == {"type": "ephemeral"}


async def test_openai_compat_http_error_retryable():
    p = OpenAICompatProvider(openrouter("K"), client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503, text="busy"))))
    with pytest.raises(LLMError) as e:
        [c async for c in p.stream(LLMRequest(model="m", messages=[Message.user("x")]))]
    assert e.value.retryable and e.value.status == 503


# ── evals и расписание ──────────────────────────────────────────────

async def test_evals_offline(tmp_path):
    suite = tmp_path / "s.yaml"
    suite.write_text(textwrap.dedent("""
        name: smoke
        cases:
          - id: remember
            input: "Запомни: отчёты по пятницам"
            expect: {status: done, tools_called: [memory_save], contains: ["запомнил"], max_steps: 3}
            script:
              - tool_calls: [[memory_save, {text: "Отчёты по пятницам"}]]
              - "Запомнил."
          - id: failing
            input: "x"
            expect: {tools_called: [todo_write]}
            script: ["нет"]
    """))

    def mk(store, fake):
        return make_agent(store, fake, tools=standard_tools(files=False, subagents=False))

    rep = await run_suite(suite, mk)
    assert rep.passed == 1
    assert "не вызван todo_write" in rep.results[1].failures[0]
    assert "smoke: 1/2" in rep.table()


async def test_scheduler(store, user):
    fake = FakeLLM(["Отчёт готов"])
    agent = make_agent(store, fake, tools=standard_tools(files=False, subagents=False))
    done = []

    async def on_done(s, outcome):
        done.append(outcome.text)

    sch = Scheduler(store, lambda name: agent, lambda uid: user, on_done=on_done)
    await store.schedule_upsert(ScheduleRec(id="sc1", name="Утро", cron="0 9 * * *", prompt="отчёт", user_id=user.id))
    await sch.tick()  # первый проход только считает next_run_at
    rec = (await store.schedule_list())[0]
    assert rec.next_run_at > time.time()
    fired = await sch.tick(now=rec.next_run_at + 1)
    assert fired == ["sc1"]
    import asyncio
    for _ in range(50):
        if done:
            break
        await asyncio.sleep(0.02)
    assert done == ["Отчёт готов"]


async def test_spec_tool_filter(store, user):
    fake = FakeLLM(["ok"])
    agent = make_agent(store, fake, tools=standard_tools(files=False))
    child = agent.child(AgentSpec(name="x", tools=["read_output", "todo_*"]))
    names = {t.name for t in child.tools.all()}
    assert "task" not in names and "todo_write" in names and "memory_save" not in names


async def test_model_catalog_keeps_snapshot_on_failure():
    from agentkit import FakeLLM, ModelRouter
    from agentkit.llm.catalog import ModelCatalog

    fake = FakeLLM()
    calls = {"n": 0}

    async def list_models():
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("openrouter недоступен")
        return [{"id": "x/a", "name": "X: A", "pricing": {"prompt": "-1"}, "supported_parameters": ["tools"]},
                {"id": "x/a:batch", "supported_parameters": ["tools"]},
                {"id": "x/b", "supported_parameters": []}]

    fake.list_models = list_models
    cat = ModelCatalog(ModelRouter({"or": fake}), configured=["or:x/conf"])
    await cat.refresh()
    ids = [m.id for m in cat.models()]
    assert ids == ["or:x/a", "or:x/conf"]  # без tools и :batch — нет; из настроек — заглушка
    assert cat.get("or:x/a").price_in is None and cat.get("or:x/a").name == "A"
    await cat.refresh(force=True)
    assert "or" in cat.errors and [m.id for m in cat.models()][0] == "or:x/a"


async def test_openrouter_cyrillic_app_name_does_not_break_requests():
    # Имя агента «Метролог» уходит в X-Title; httpx не пропускает не-ASCII в заголовках.
    import httpx

    from agentkit.llm.openai_compat import OpenAICompatProvider, openrouter

    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["title"] = req.headers["X-Title"]
        return httpx.Response(200, json={"data": [{"id": "a/b"}]})

    p = OpenAICompatProvider(openrouter("k", app_name="Метролог ЕИ"), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await p.list_models() == [{"id": "a/b"}]
    assert seen["title"] == "Metrolog EI"


# ── файлы: подсказки и контекст grep ────────────────────────────────

async def test_missing_file_suggests_similar(store, tmp_path):
    from agentkit.tools.base import ToolContext, User
    from agentkit.tools.files import grep, read_file

    (tmp_path / "conf").mkdir()
    (tmp_path / "settings.yaml").write_text("timeout: 42s\n")
    (tmp_path / "conf" / "db.yaml").write_text("port: 5432\n")
    ctx = ToolContext(session_id="s", run_id="r", user=User(id="u"), agent=None, store=store, emit=None,
                      workspace=tmp_path)
    r = await read_file.call({"path": "settings.yml"}, ctx)
    assert r.is_error and "Похожие: settings.yaml" in r.content
    r = await read_file.call({"path": "db.yaml"}, ctx)
    assert "conf/db.yaml" in r.content
    r = await read_file.call({"path": "zzz.bin"}, ctx)
    assert r.content.endswith("нет файла zzz.bin")

    (tmp_path / "n.py").write_text("def alert_owner(e):\n    text = e\n    send_sms(text)\n    send_sms(2)\n    return 1\n")
    r = await grep.call({"pattern": "send_sms", "glob": "*.py", "context": 2}, ctx)
    assert r.content.splitlines() == [
        "n.py-1- def alert_owner(e):", "n.py-2-     text = e", "n.py:3:     send_sms(text)",
        "n.py:4:     send_sms(2)", "n.py-5-     return 1"]
