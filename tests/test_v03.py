"""0.3: проекты, компьютер через мост, песочница, миграция колонок."""

from __future__ import annotations

import asyncio
import sqlite3
import sys

import pytest
from conftest import collect, make_agent

from agentkit import FakeLLM, SQLStore, User
from agentkit.bridge import Bridge, BridgeError, Folder
from agentkit.bus import LocalBus
from agentkit.devices import DEVICE_TOOLS, DeviceHub, device_tools
from agentkit.projects import PROJECT_TOOLS, ProjectLibrary, project_tools
from agentkit.sandbox import execute
from agentkit.tools.base import ToolContext
from agentkit.tools.sandbox import _result


async def test_old_database_gets_new_columns(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ak_sessions (id VARCHAR(40) PRIMARY KEY, user_id VARCHAR(120) NOT NULL, title VARCHAR(300),"
                " agent VARCHAR(80), parent_id VARCHAR(40), state JSON, created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL)")
    con.commit()
    con.close()
    store = await SQLStore(f"sqlite:///{db}").init()
    s = await store.create_session("u", project_id="p_1")
    assert (await store.get_session(s.id)).project_id == "p_1"
    await store.close()


async def test_project_context_inline_then_search(store, tmp_path):
    lib = ProjectLibrary(store, tmp_path, inline_limit=200)
    owner, guest = User(id="u1", name="A"), User(id="u2", name="B")
    p = await store.project_create("u1", "Поверка", instructions="Отвечай по МИ 1592-2015")
    await lib.add_file(p.id, "методика.txt", "Межповерочный интервал счётчиков воды — 6 лет.".encode())
    s = await store.create_session("u1", project_id=p.id)

    ctx = await lib.context(owner, s.id)
    assert "МИ 1592-2015" in ctx.text and "Межповерочный интервал" in ctx.text and not ctx.hide_tools
    # чужой не видит ничего, и инструменты проекта ему спрятаны
    assert (await lib.context(guest, s.id)).hide_tools == PROJECT_TOOLS
    # разговор без проекта — инструменты спрятаны
    plain = await store.create_session("u1")
    assert (await lib.context(owner, plain.id)).hide_tools == PROJECT_TOOLS

    # много текста — в промпте только список, поиск находит нужный кусок
    big = "\n\n".join(f"Раздел {i}. Общие положения про термометры и манометры." for i in range(60))
    await lib.add_file(p.id, "большой.txt", (big + "\n\nПоверка теплосчётчиков ведётся по ГОСТ Р 8.591.").encode())
    ctx = await lib.context(owner, s.id)
    assert "project_search" in ctx.text and "ГОСТ Р 8.591" not in ctx.text
    hits = await lib.search(p.id, "поверки теплосчётчика")
    assert hits and "8.591" in hits[0]["text"]

    # общий доступ: viewer видит проект, но поиск идёт через инструмент
    await store.project_member_set(p.id, "*", "viewer")
    assert (await store.project_role(p.id, "u2"))[1] == "viewer"
    search = next(t for t in project_tools(lib) if t.name == "project_search")
    res = await search.call({"query": "ГОСТ теплосчётчиков"},
                            ToolContext(session_id=s.id, run_id="r", user=guest, agent=None, store=store, emit=None))
    assert "8.591" in res.content


async def test_agent_sees_project_and_hides_tools(store, tmp_path):
    lib = ProjectLibrary(store, tmp_path)
    p = await store.project_create("u1", "Клиенты", instructions="Обращайся к клиентам на «вы»")
    s = await store.create_session("u1", project_id=p.id)
    fake = FakeLLM(default="ок")
    agent = make_agent(store, fake, tools=None)
    for t in project_tools(lib):
        agent.tools.add(t)
    agent.context_providers.append(lib.context)
    await collect(agent.run(s.id, User(id="u1"), "привет"))
    req = fake.requests[-1]
    assert "Обращайся к клиентам на «вы»" in req.messages[0].text
    # файлов нет — инструменты проекта не предлагаются
    assert not PROJECT_TOOLS & {t["function"]["name"] for t in req.tools}


def make_bridge(tmp_path, shell=False):
    work = tmp_path / "work"
    work.mkdir()
    (work / "notes.txt").write_text("первая строка\nвторая — поверка\n", encoding="utf-8")
    ro = tmp_path / "docs"
    ro.mkdir()
    (ro / "a.md").write_text("только чтение", encoding="utf-8")
    return Bridge("http://x", "k", [Folder("work", work.resolve(), False), Folder("docs", ro.resolve(), True)], shell)


def test_bridge_ops_stay_inside_folders(tmp_path):
    b = make_bridge(tmp_path)
    assert {i["name"] for i in b.op_list("")["items"]} == {"work", "docs"}
    assert "поверка" in b.op_read("work/notes.txt")["text"]
    assert b.op_search("поверка")["hits"][0]["path"] == "work/notes.txt"
    b.op_write("work/sub/new.txt", "x")
    assert (tmp_path / "work/sub/new.txt").read_text() == "x"
    b.op_edit("work/notes.txt", "первая", "1-я")
    with pytest.raises(BridgeError, match="за пределы"):
        b.op_read("work/../docs/a.md")
    with pytest.raises(BridgeError, match="только для чтения"):
        b.op_write("docs/a.md", "y")
    with pytest.raises(BridgeError, match="не открыта"):
        b.op_read("etc/passwd")
    with pytest.raises(BridgeError, match="запрещены"):
        b.op_run("echo hi")


async def test_device_roundtrip_through_bus(store, tmp_path):
    hub = DeviceHub(store, LocalBus())
    dev, key = await hub.create("u1", "Ноутбук")
    assert (await hub.auth(key))["id"] == dev["id"] and await hub.auth(key + "x") is None
    bridge = make_bridge(tmp_path, shell=True)

    async def fake_bridge():
        # то же, что делает bridge.py: опрос → выполнить → ответ
        d = await hub.auth(key)
        await hub.touch(d, bridge.info())
        while True:
            cmd = await hub.next_command(d["id"], 2)
            if cmd:
                res = getattr(bridge, "op_" + cmd["op"])(**cmd["args"])
                await hub.deliver(d["id"], cmd["id"], {"ok": True, "result": res})

    task = asyncio.create_task(fake_bridge())
    await asyncio.sleep(0.05)
    tools = {t.name: t for t in device_tools(hub)}
    ctx = ToolContext(session_id="s", run_id="r", user=User(id="u1"), agent=None, store=store, emit=None)
    res = await tools["pc_read"].call({"path": "work/notes.txt"}, ctx)
    assert "поверка" in res.content
    cmd = "echo привет" if sys.platform != "win32" else "echo hi"
    res = await tools["pc_run"].call({"command": cmd}, ctx)
    assert "Код выхода: 0" in res.content
    tctx = await hub.context(User(id="u1"), "s")
    assert "Ноутбук" in tctx.text and "work" in tctx.text and not tctx.hide_tools
    # другой пользователь компьютера не видит
    other = await tools["pc_list"].call({}, ToolContext(session_id="s", run_id="r", user=User(id="u2"), agent=None,
                                                        store=store, emit=None))
    assert other.is_error and "не подключён" in other.content
    assert (await hub.context(User(id="u2"), "s")).hide_tools == DEVICE_TOOLS
    task.cancel()


def test_sandbox_execute_reports_files(tmp_path):
    r = execute(tmp_path, "s_1", "python", "open('out.csv','w').write('a,b\\n1,2')\nprint('готово')", 30)
    assert r["exit_code"] == 0 and "готово" in r["stdout"]
    assert r["files"] == [{"path": "out.csv", "size": 7, "new": True}]
    res = _result(r)
    assert res.ui[0]["type"] == "files" and not res.is_error
    bad = execute(tmp_path, "s_1", "python", "raise SystemExit(3)", 30)
    assert bad["exit_code"] == 3 and _result(bad).is_error
    with pytest.raises(ValueError):
        execute(tmp_path, "../etc", "python", "print(1)", 5)
