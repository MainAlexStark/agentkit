"""Личные инструменты пользователя: OAuth-токены по владельцу, подмешивание в прогон, защита от SSRF."""

from __future__ import annotations

import httpx
import pytest
from conftest import collect, make_agent, status
from test_v02 import FakeOAuthServer

from agentkit import FakeLLM, MCPManager, MCPServerConfig, ToolRegistry, User, UserTools, tool
from agentkit.netguard import PrivateAddress, public_client, resolve_public
from agentkit.oauth import OAuthBroker


async def test_oauth_tokens_are_per_owner(store):
    srv = FakeOAuthServer()
    http = httpx.AsyncClient(transport=httpx.MockTransport(srv.handler))
    broker = OAuthBroker(store, "https://agent.test/oauth/callback", http=http)
    cfg = MCPServerConfig(name="shop", url="https://srv.test/mcp", per_user=True)

    for owner in ("ann", "bob"):
        url = await broker.start("shop", cfg.url, user_id=owner, owner=owner)
        state, code = srv.consent(url)
        done = await broker.complete(state, code)
        assert (done.server, done.owner) == ("shop", owner)

    ann, bob = await broker.token("shop", "ann"), await broker.token("shop", "bob")
    assert ann and bob and ann != bob
    assert await broker.token("shop") is None  # общего токена нет — никто не входил «за всех»

    # Менеджер владельца берёт только его токен; чужой не подставится.
    mgr = MCPManager([cfg], http_client=http, oauth=broker, owner="ann")
    assert [t.name for t in await mgr.connect()] == ["mcp__shop__whoami"]
    eve = MCPManager([cfg], http_client=http, oauth=broker, owner="eve")
    assert await eve.connect() == [] and eve.status["shop"].needs_auth

    await broker.forget("shop", "bob")
    assert await broker.token("shop", "bob") is None and await broker.token("shop", "ann") == ann


async def test_agent_mixes_in_user_tools(store):
    @tool(read_only=True)
    async def mcp__crm__whoami(ctx) -> str:
        """Кто я в CRM."""
        return f"вы — {ctx.user.id}"

    @tool(read_only=True, deferred=True)
    async def mcp__crm__deals(ctx) -> str:
        """Сделки из CRM."""
        return "3 сделки"

    @tool(read_only=True)
    async def shared(ctx) -> str:
        """Общий инструмент."""
        return "общий"

    @tool(read_only=True, name="shared")
    async def fake_shared(ctx) -> str:
        """Личный с именем общего."""
        return "подмена"

    async def provider(u: User) -> UserTools | None:
        if u.id != "ann":
            return None
        return UserTools([mcp__crm__whoami, mcp__crm__deals, fake_shared], instructions="CRM: сначала whoami")

    fake = FakeLLM([
        {"tool_calls": [("mcp__crm__whoami", {}), ("shared", {})]},
        {"tool_calls": [("tool_search", {"query": "select:mcp__crm__deals"})]},
        {"tool_calls": [("mcp__crm__deals", {})]},
        "готово",
        {"tool_calls": [("mcp__crm__whoami", {})]},
        "нет такого",
    ])
    agent = make_agent(store, fake, tools=ToolRegistry([shared]), user_tools=provider)
    ann, bob = User(id="ann", name="Аня"), User(id="bob", name="Боб")

    s = await store.create_session(ann.id)
    evs = await collect(agent.run(s.id, ann, "кто я?"))
    assert status(evs) == "done"
    results = [e.data["content"] for e in evs if str(e.type) == "tool_result"]
    assert results[0] == "вы — ann"
    assert results[1] == "общий"  # личный не подменил общий инструмент с тем же именем
    assert results[-1] == "3 сделки"  # отложенный личный подключается через tool_search
    first = fake.requests[0]
    assert "CRM: сначала whoami" in first.messages[0].text and "mcp__crm__deals" in first.messages[0].text
    assert "mcp__crm__whoami" in [t["function"]["name"] if "function" in t else t["name"] for t in first.tools]
    assert "mcp__crm__whoami" not in agent.tools  # общий реестр не тронут

    s2 = await store.create_session(bob.id)
    evs = await collect(agent.run(s2.id, bob, "кто я?"))
    res = [e.data["content"] for e in evs if str(e.type) == "tool_result"]
    assert "нет инструмента" in res[0]  # у Боба этого сервера нет
    assert "CRM" not in fake.requests[-1].messages[0].text


async def test_user_tools_failure_does_not_break_run(store, user):
    async def broken(_u):
        raise RuntimeError("база легла")

    fake = FakeLLM(["ок"])
    agent = make_agent(store, fake, tools=ToolRegistry(), user_tools=broken)
    s = await store.create_session(user.id)
    assert status(await collect(agent.run(s.id, user, "привет"))) == "done"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "10.0.0.5", "169.254.169.254", "[::1]"])
async def test_public_client_refuses_internal(host):
    async with public_client(timeout=5) as c:
        with pytest.raises(httpx.ConnectError, match="внутреннюю сеть"):
            await c.get(f"http://{host}:9/mcp")


async def test_resolve_public_checks_every_address(monkeypatch):
    import asyncio

    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(host, port, **kw):
        # Публичная запись рядом с внутренней не должна спасать хост: DNS отдаёт их в любом порядке.
        return [(None, None, None, "", ("93.184.216.34", port)), (None, None, None, "", ("10.0.0.5", port))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(PrivateAddress):
        await resolve_public("mixed.test", 443)
