"""MCP-сервер хоста (per_user): у каждого пользователя свои инструменты и данные.

Так агент встраивается в приложение, где у каждого свой магазин (OpenSeller):
хост отдаёт инструменты по X-Agent-User-Id, проверяя токен сервера.
"""

from __future__ import annotations

import json

import httpx
from conftest import collect, make_agent, status

from agentkit import FakeLLM, MCPManager, MCPServerConfig, ToolRegistry, User
from agentkit.events import EventType
from agentkit.tools.core import core_tools

TOKEN = "host-secret"


class Host:
    """Поддельный MCP-сервер хоста: набор инструментов зависит от пользователя."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.lists: list[str] = []
        self.down = False

    def tools_for(self, uid: str) -> list[dict]:
        tools = [{"name": "sales", "description": "Продажи магазина", "annotations": {"readOnlyHint": True},
                  "inputSchema": {"type": "object", "properties": {"days": {"type": "integer"}}}}]
        if uid == "u1":  # только у u1 подключён Ozon с правом менять цены
            tools.append({"name": "set_price", "description": "Поменять цену",
                          "inputSchema": {"type": "object", "properties": {"sku": {"type": "string"},
                                                                          "price": {"type": "number"}},
                                          "required": ["sku", "price"]}})
        return tools

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503, text="хост перезапускается")
        if req.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401)
        uid = req.headers.get("x-agent-user-id", "")
        msg = json.loads(req.content)
        if "id" not in msg:
            return httpx.Response(202)
        method = msg["method"]
        if method == "initialize":
            res = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "host"},
                   "instructions": f"Магазин пользователя {uid}."}
        elif method == "tools/list":
            self.lists.append(f"{uid}/{req.headers.get('x-agent-session-id')}")
            res = {"tools": self.tools_for(uid)}
        else:
            p = msg["params"]
            self.calls.append({"user": uid, "session": req.headers.get("x-agent-session-id"),
                               "run": req.headers.get("x-agent-run-id"), "name": p["name"], "args": p["arguments"]})
            res = {"content": [{"type": "text", "text": f"{p['name']} для {uid}: ок"}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": res})


def _setup(store, fake, host: Host, **cfg):
    http = httpx.AsyncClient(transport=httpx.MockTransport(host.handler))
    mgr = MCPManager([MCPServerConfig(name="shop", url="https://host.test/agent/mcp", token=TOKEN,
                                      per_user=True, **cfg)], http_client=http)
    agent = make_agent(store, fake, tools=ToolRegistry(core_tools()))
    agent.context_providers.append(mgr.turn_context)
    return agent, mgr


async def test_each_user_sees_own_host_tools(store):
    host = Host()
    fake = FakeLLM([{"tool_calls": [("mcp__shop__sales", {"days": 7})]}, "Готово", "Готово"])
    agent, mgr = _setup(store, fake, host)
    assert await mgr.connect() == [] and mgr.status["shop"].per_user

    u1, u2 = User(id="u1"), User(id="u2")
    s1 = await store.create_session(u1.id)
    evs = await collect(agent.run(s1.id, u1, "продажи за неделю"))
    assert status(evs) == "done"
    names = {t["function"]["name"] for t in fake.requests[0].tools}
    assert {"mcp__shop__sales", "mcp__shop__set_price"} <= names
    assert "Магазин пользователя u1." in fake.requests[0].messages[0].text
    res = [e for e in evs if e.type == EventType.TOOL_RESULT][0]
    assert "sales для u1: ок" in json.dumps(res.data, ensure_ascii=False)
    run_id = evs[0].run_id
    assert host.calls == [{"user": "u1", "session": s1.id, "run": run_id, "name": "sales", "args": {"days": 7}}]

    s2 = await store.create_session(u2.id)
    await collect(agent.run(s2.id, u2, "привет"))
    names2 = {t["function"]["name"] for t in fake.requests[-1].tools}
    assert "mcp__shop__sales" in names2 and "mcp__shop__set_price" not in names2
    assert "Магазин пользователя u2." in fake.requests[-1].messages[0].text
    # Список кэшируется на разговор: два шага u1 и один u2 — два похода за списком.
    assert host.lists == [f"u1/{s1.id}", f"u2/{s2.id}"]


async def test_host_write_needs_approval_and_runs_after_it(store):
    host = Host()
    fake = FakeLLM([{"tool_calls": [("mcp__shop__set_price", {"sku": "A-1", "price": 990})]}, "Цена изменена"])
    agent, mgr = _setup(store, fake, host)
    await mgr.connect()
    u1 = User(id="u1")
    s = await store.create_session(u1.id)
    evs = await collect(agent.run(s.id, u1, "поставь 990"))
    assert status(evs) == "waiting_approval" and host.calls == []
    appr = (await store.list_approvals(s.id, status="pending"))[0]
    # Решение — новый прогон: личный инструмент должен найтись и там.
    evs2 = await collect(agent.resolve_approval(appr.id, u1, approve=True))
    assert status(evs2) == "done"
    assert [c["name"] for c in host.calls] == ["set_price"] and host.calls[0]["user"] == "u1"
    assert host.calls[0]["run"] == evs2[0].run_id
    # Изменение сбросило кэш: следующий шаг взял список заново.
    assert host.lists == [f"u1/{s.id}"] * 2


async def test_host_down_is_text_for_model_not_crash(store):
    host = Host()
    host.down = True
    fake = FakeLLM(["Магазин недоступен"])
    agent, mgr = _setup(store, fake, host)
    await mgr.connect()
    u1 = User(id="u1")
    s = await store.create_session(u1.id)
    evs = await collect(agent.run(s.id, u1, "продажи"))
    assert status(evs) == "done"
    system = fake.requests[0].messages[0].text
    assert "«shop» сейчас недоступны" in system and "503" in system


async def test_subagent_tool_filter_applies_to_host_tools(store):
    from agentkit import AgentSpec

    host = Host()
    agent, mgr = _setup(store, FakeLLM(["ok"]), host)
    await mgr.connect()
    child = agent.child(AgentSpec(name="explore", tools=["mcp__shop__sales"]))
    u1 = User(id="u1")
    turn = await mgr.turn_context(u1, "s")
    reg = child._with_turn_tools("r1", turn.tools)
    assert host.lists == ["u1/s"]
    assert "mcp__shop__sales" in reg and "mcp__shop__set_price" not in reg


async def test_user_headers_are_ascii():
    from agentkit.mcp import user_headers

    h = user_headers(User(id="магазин 7", roles=["admin", "продавец"]))
    for v in h.values():
        v.encode("latin-1")
    assert h["X-Agent-User-Id"] == "%D0%BC%D0%B0%D0%B3%D0%B0%D0%B7%D0%B8%D0%BD%207"
