"""OAuth для MCP, вложения, сообщения на ходу, несколько процессов (Redis)."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import shutil
import socket
import subprocess
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import collect, make_agent, status

from agentkit import FakeLLM, MCPManager, MCPServerConfig, SQLStore, ToolRegistry, User, tool
from agentkit.attachments import expand_parts, is_vision, save_upload
from agentkit.bus import LocalBus, RedisBus
from agentkit.events import Event, EventType
from agentkit.oauth import OAuthBroker, OAuthError, OAuthOptions
from agentkit.runs import Busy, RunManager
from agentkit.scheduler import Scheduler
from agentkit.store.base import ApprovalRec, ScheduleRec
from agentkit.tools.core import core_tools

# ── сообщения на ходу ───────────────────────────────────────────────


async def test_steer_between_steps(store, user):
    pushed = []

    @tool(read_only=True)
    async def slow_step(ctx) -> str:
        """Шаг, во время которого человек дописывает."""
        await ctx.store.inbox_push(ctx.session_id, "И ещё: добавь итог в рублях", {"id": user.id})
        pushed.append(1)
        return "сделано"

    fake = FakeLLM([{"tool_calls": [("slow_step", {})]}, "Итог: 100 ₽"])
    agent = make_agent(store, fake, tools=ToolRegistry([*core_tools(), slow_step]))
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "посчитай"))
    assert status(evs) == "done"
    um = [e for e in evs if e.type == EventType.USER_MESSAGE]
    assert um and um[0].data["content"] == "И ещё: добавь итог в рублях"
    # Второй запрос к модели: вызов → результат → новое сообщение человека
    roles = [(m.role, m.text[:20]) for m in fake.requests[1].messages[1:]]
    assert [r for r, _ in roles] == ["user", "assistant", "tool", "user"]
    assert await store.inbox_count(s.id) == 0


async def test_steer_arriving_at_the_end_continues(store, user):
    state = {"n": 0}

    def script(req):
        state["n"] += 1
        if state["n"] == 1:
            return "Первый ответ"
        return "Ответ на дописанное"

    fake = FakeLLM([script] * 3)
    agent = make_agent(store, fake, tools=ToolRegistry(core_tools()))
    s = await store.create_session(user.id)
    gen = agent.run(s.id, user, "a")
    evs = []
    async for ev in gen:
        evs.append(ev)
        if ev.type == EventType.TEXT_DELTA and state["n"] == 1 and not await store.inbox_count(s.id):
            await store.inbox_push(s.id, "уточнение", {"id": user.id})
    texts = [e.data["content"] for e in evs if e.type == EventType.MESSAGE]
    assert texts == ["Первый ответ", "Ответ на дописанное"]


async def test_approval_claim_is_atomic(store, user):
    s = await store.create_session(user.id)
    await store.create_approval(ApprovalRec(id="a1", session_id=s.id, run_id="r", tool_call_id="c", tool_name="x", args={}))
    results = await asyncio.gather(*(store.claim_approval("a1") for _ in range(5)))
    assert sum(results) == 1


# ── вложения ────────────────────────────────────────────────────────

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _docx_bytes(text: str) -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _pdf_bytes(text: str) -> bytes:
    # Минимальный PDF с текстовым слоем, без внешних библиотек.
    content = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref))
    return out.getvalue()


def test_save_and_expand(tmp_path):
    img = save_upload(tmp_path, "фото счётчика.png", PNG_1PX, "image/png")
    txt = save_upload(tmp_path, "notes.md", "# Заметки\nстрока".encode(), "text/markdown")
    doc = save_upload(tmp_path, "акт.docx", _docx_bytes("Акт поверки №12"), "")
    pdf = save_upload(tmp_path, "method.pdf", _pdf_bytes("Methodika MI 1592"), "application/pdf")
    bad = save_upload(tmp_path, "../../evil.sh", b"rm -rf /", "")
    assert img["kind"] == "image" and img["path"].startswith("uploads/")
    assert bad["path"] == "uploads/evil.sh"  # имя очищено, выйти из uploads нельзя
    assert doc["text_chars"] > 0 and pdf["text_chars"] > 0

    content = [{"type": "text", "text": "посмотри"}, img, txt, doc, pdf]
    with_vision = expand_parts(content, tmp_path, vision=True)
    kinds = [p["type"] for p in with_vision]
    assert "image_url" in kinds
    joined = " ".join(p.get("text", "") for p in with_vision)
    assert "строка" in joined and "Акт поверки №12" in joined and "MI 1592" in joined
    no_vision = expand_parts(content, tmp_path, vision=False)
    assert "image_url" not in [p["type"] for p in no_vision]
    assert "не видит изображения" in " ".join(p.get("text", "") for p in no_vision)


def test_big_image_is_downscaled(tmp_path):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4000, 3000), "white").save(buf, "PNG")
    info = save_upload(tmp_path, "big.png", buf.getvalue(), "image/png")
    small = tmp_path / (info["path"] + ".model.jpg")
    assert small.exists()
    assert max(Image.open(small).size) == 1600


def test_vision_patterns():
    assert is_vision("openrouter:anthropic/claude-sonnet-4.5")
    assert is_vision("yandex:qwen2.5-vl-72b-instruct")
    assert not is_vision("yandex:yandexgpt/latest")
    assert is_vision("my:model", ["my:*"])


async def test_agent_sends_image_to_model(store, user, tmp_path):
    fake = FakeLLM(["Вижу счётчик"])
    agent = make_agent(store, fake, tools=ToolRegistry(core_tools()), workspace=tmp_path)
    s = await store.create_session(user.id)
    info = save_upload(tmp_path, "a.png", PNG_1PX, "image/png")
    await collect(agent.run(s.id, user, [{"type": "text", "text": "что на фото?"}, info]))
    user_msg = fake.requests[0].messages[1]
    assert any(p.get("type") == "image_url" for p in user_msg.content)
    # в базе — ссылка, а не base64
    stored = (await store.list_messages(s.id))[0][1]
    assert stored.content[1]["type"] == "file" and "base64" not in json.dumps(stored.content)
    assert (await store.get_session(s.id)).title == "что на фото?"


# ── OAuth ───────────────────────────────────────────────────────────

class FakeOAuthServer:
    """MCP-сервер за OAuth: 401 → метаданные → DCR → authorize → token → MCP с Bearer."""

    def __init__(self):
        self.clients: dict[str, list[str]] = {}
        self.codes: dict[str, tuple[str, str]] = {}  # code → (challenge, client)
        self.access: set[str] = set()
        self.refresh: dict[str, str] = {}
        self.n = 0
        self.log: list[str] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        u = req.url
        path = u.path
        self.log.append(f"{req.method} {path}")
        if path == "/mcp":
            auth = req.headers.get("authorization", "")
            if auth.removeprefix("Bearer ") not in self.access:
                return httpx.Response(401, headers={"WWW-Authenticate":
                    'Bearer resource_metadata="https://srv.test/.well-known/oauth-protected-resource/mcp"'})
            body = json.loads(req.content)
            if "id" not in body:
                return httpx.Response(202)
            result = {"initialize": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "srv"}},
                      "tools/list": {"tools": [{"name": "whoami", "inputSchema": {"type": "object"},
                                                "annotations": {"readOnlyHint": True}}]},
                      "tools/call": {"content": [{"type": "text", "text": "вы — магазин №1"}]}}[body["method"]]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
        if path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json={"resource": "https://srv.test/mcp",
                                              "authorization_servers": ["https://auth.test"]})
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={
                "authorization_endpoint": "https://auth.test/authorize", "token_endpoint": "https://auth.test/token",
                "registration_endpoint": "https://auth.test/register", "revocation_endpoint": "https://auth.test/revoke"})
        if path == "/register":
            meta = json.loads(req.content)
            cid = f"client-{len(self.clients) + 1}"
            self.clients[cid] = meta["redirect_uris"]
            return httpx.Response(201, json={"client_id": cid})
        if path == "/token":
            form = parse_qs(req.content.decode())
            f = {k: v[0] for k, v in form.items()}
            assert f["resource"] == "https://srv.test/mcp"
            if f["grant_type"] == "authorization_code":
                challenge, cid = self.codes.pop(f["code"])
                calc = base64.urlsafe_b64encode(__import__("hashlib").sha256(f["code_verifier"].encode()).digest()).rstrip(b"=").decode()
                if calc != challenge or cid != f["client_id"]:
                    return httpx.Response(400, json={"error": "invalid_grant"})
            elif f["grant_type"] == "refresh_token":
                if f["refresh_token"] not in self.refresh:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                self.access.discard(self.refresh.pop(f["refresh_token"]))
            self.n += 1
            acc, ref = f"acc-{self.n}", f"ref-{self.n}"
            self.access.add(acc)
            self.refresh[ref] = acc
            return httpx.Response(200, json={"access_token": acc, "refresh_token": ref, "expires_in": 3600})
        if path == "/revoke":
            return httpx.Response(200)
        return httpx.Response(404)

    def consent(self, auth_url: str) -> tuple[str, str]:
        """Имитация экрана согласия: проверяем параметры и выдаём код."""
        q = {k: v[0] for k, v in parse_qs(urlparse(auth_url).query).items()}
        assert q["code_challenge_method"] == "S256" and q["resource"] == "https://srv.test/mcp"
        assert q["redirect_uri"] in self.clients[q["client_id"]]
        code = f"code-{len(self.codes)}"
        self.codes[code] = (q["code_challenge"], q["client_id"])
        return q["state"], code


async def test_oauth_full_flow(store):
    srv = FakeOAuthServer()
    http = httpx.AsyncClient(transport=httpx.MockTransport(srv.handler))
    broker = OAuthBroker(store, "https://agent.test/oauth/callback", http=http)
    cfg = MCPServerConfig(name="shop", url="https://srv.test/mcp")

    # до входа: сервер виден, но требует подключения
    mgr = MCPManager([cfg], http_client=http, oauth=broker)
    assert await mgr.connect() == []
    assert mgr.status["shop"].needs_auth

    url = await broker.start("shop", cfg.url, OAuthOptions(scope="read"), user_id="admin")
    assert url.startswith("https://auth.test/authorize?")
    state, code = srv.consent(url)
    assert await broker.finish(state, code) == "shop"
    with pytest.raises(OAuthError):
        await broker.finish(state, code)  # state одноразовый

    mgr = MCPManager([cfg], http_client=http, oauth=broker)
    tools = await mgr.connect()
    assert [t.name for t in tools] == ["mcp__shop__whoami"] and mgr.status["shop"].oauth
    res = await tools[0].call({}, None)
    assert res.content == "вы — магазин №1"

    # токен истёк — обновляется сам, с ротацией refresh
    rec = await broker.record("shop")
    rec.expires_at = time.time() + 10
    await broker._save("shop", rec)
    assert await broker.token("shop") == "acc-2"
    assert (await broker.record("shop")).refresh_token == "ref-2"

    # токен отозвали на сервере раньше срока — 401 → обновление → повтор
    srv.access.discard("acc-2")
    res = await tools[0].call({}, None)
    assert res.content == "вы — магазин №1"

    await broker.disconnect("shop")
    assert not (await broker.status("shop"))["connected"]
    assert "POST /revoke" in srv.log


async def test_oauth_no_registration_needs_client_id(store):
    def handler(req):
        if req.url.path == "/mcp":
            return httpx.Response(401)
        if req.url.path.startswith("/.well-known/oauth-protected-resource"):
            return httpx.Response(200, json={"authorization_servers": ["https://g.test"]})
        if "authorization-server" in req.url.path:
            return httpx.Response(200, json={"authorization_endpoint": "https://g.test/a", "token_endpoint": "https://g.test/t"})
        return httpx.Response(404)

    broker = OAuthBroker(store, "https://agent.test/cb", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(OAuthError, match="client_id"):
        await broker.start("g", "https://x.test/mcp")
    url = await broker.start("g", "https://x.test/mcp", OAuthOptions(client_id="my-id", client_secret="s",
                                                                      auth_params={"access_type": "offline"}))
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["my-id"] and q["access_type"] == ["offline"]


# ── шина и несколько процессов ──────────────────────────────────────

@pytest.fixture(scope="module")
def redis_url():
    if not shutil.which("redis-server"):
        pytest.skip("нет redis-server")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    proc = subprocess.Popen(["redis-server", "--port", str(port), "--save", "", "--appendonly", "no"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.1).close()
            break
        except OSError:
            time.sleep(0.05)
    yield f"redis://127.0.0.1:{port}/0"
    proc.terminate()


@pytest.mark.parametrize("kind", ["local", "redis"])
async def test_bus_contract(kind, request):
    if kind == "redis":
        url = request.getfixturevalue("redis_url")
        a, b = RedisBus(url, prefix="t1"), RedisBus(url, prefix="t1")
    else:
        a = b = LocalBus()
    await a.start()
    if b is not a:
        await b.start()
    try:
        assert await a.try_lock("s1", "starting", 5)
        assert not await b.try_lock("s1", "x", 5)
        await a.set_lock("s1", "r1", 5)
        assert await b.active("s1") == "r1"
        sub = await b.open("s1")
        await a.publish("s1", {"type": "x", "seq": 1})
        assert (await sub.get(2))["seq"] == 1
        await sub.close()
        await a.append_partial("s1", "При")
        await a.append_partial("s1", "вет")
        assert await b.partial("s1") == "Привет"
        got = []

        async def h(m):
            got.append(m)

        b.on_broadcast(h)
        await a.broadcast({"cmd": "ping"})
        for _ in range(40):
            if got:
                break
            await asyncio.sleep(0.05)
        assert got and got[0]["cmd"] == "ping"
        order = []

        async def crit(n):
            async with (a if n % 2 else b).lock("k", ttl=5):
                order.append(("in", n))
                await asyncio.sleep(0.05)
                order.append(("out", n))

        await asyncio.gather(*(crit(i) for i in range(4)))
        assert all(order[i][0] == "in" and order[i + 1] == ("out", order[i][1]) for i in range(0, 8, 2))
        await a.unlock("s1")
        assert await b.active("s1") is None
    finally:
        await a.close()
        if b is not a:
            await b.close()


async def test_two_processes_share_runs(tmp_path, redis_url, user):
    """Два RunManager (как два процесса) с общей базой и Redis."""
    db = f"sqlite:///{tmp_path}/shared.db"
    store_a, store_b = await SQLStore(db).init(), await SQLStore(db).init()
    bus_a, bus_b = RedisBus(redis_url, prefix="t2"), RedisBus(redis_url, prefix="t2")
    await bus_a.start()
    await bus_b.start()
    runs_a, runs_b = RunManager(store_a, bus_a), RunManager(store_b, bus_b)

    @tool(read_only=True)
    async def wait_a_bit(ctx) -> str:
        """Долгий шаг."""
        for _ in range(100):
            if ctx.cancel.is_set():
                break
            await asyncio.sleep(0.05)
        return "ok"

    fake = FakeLLM([{"text": "Начинаю", "tool_calls": [("wait_a_bit", {})]}] * 3 + ["Готово"])
    agent_a = make_agent(store_a, fake, tools=ToolRegistry([*core_tools(), wait_a_bit]))
    s = await store_a.create_session(user.id)
    try:
        await runs_a.start(s.id, lambda: agent_a.run(s.id, user, "go"), cancel=agent_a.cancel)
        assert await runs_b.active_run(s.id)  # «другой процесс» видит прогон
        with pytest.raises(Busy):
            await runs_b.start(s.id, lambda: agent_a.run(s.id, user, "x"))
        seen = []

        async def watch():
            async for ev in runs_b.subscribe(s.id, 0):
                seen.append(ev["type"])
                if ev["type"] == "tool_call":
                    assert await runs_b.cancel(s.id)  # отмена из «другого процесса»

        await asyncio.wait_for(watch(), 15)
        assert "run_started" in seen and seen[-1] == "run_finished"
        run = (await store_a.list_runs(s.id))[0]
        assert run.status == "cancelled"
        assert await runs_b.active_run(s.id) is None
    finally:
        await bus_a.close()
        await bus_b.close()
        await store_a.close()
        await store_b.close()


async def test_schedule_fires_once_across_processes(tmp_path, user):
    db = f"sqlite:///{tmp_path}/s.db"
    st = await SQLStore(db).init()
    fake = FakeLLM(["ок"] * 5)
    agent = make_agent(st, fake, tools=ToolRegistry(core_tools()))
    fired = []

    async def runner(rec, sid, u):
        fired.append(rec.id)
        return await agent.run_to_end(sid, u, rec.prompt)

    schedulers = [Scheduler(st, lambda n: agent, lambda uid: user, runner=runner) for _ in range(3)]
    await st.schedule_upsert(ScheduleRec(id="sc", name="x", cron="* * * * *", prompt="p", user_id=user.id,
                                         next_run_at=time.time() - 1))
    results = await asyncio.gather(*(sc.tick() for sc in schedulers))
    assert sum(len(r) for r in results) == 1
    await asyncio.sleep(0.3)
    assert fired == ["sc"]
    await st.close()


async def test_runmanager_local_subscribe_ends_when_idle(store, user):
    runs = RunManager(store, LocalBus())
    fake = FakeLLM(["привет"])
    agent = make_agent(store, fake, tools=ToolRegistry(core_tools()))
    s = await store.create_session(user.id)
    await runs.start(s.id, lambda: agent.run(s.id, user, "x"))
    types_ = [e["type"] async for e in runs.subscribe(s.id, 0, grace=0.2)]
    assert types_[0] == "run_started" and types_[-1] == "run_finished"


def test_event_roundtrip():
    e = Event(EventType.USER_MESSAGE, {"content": "x"}, run_id="r", session_id="s")
    assert e.to_dict()["type"] == "user_message"


__all__ = ["User"]
