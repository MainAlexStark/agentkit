"""0.3: проекты, компьютер через мост, песочница, миграция колонок."""

from __future__ import annotations

import asyncio
import json
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
    with pytest.raises(BridgeError, match="не разрешены"):
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


async def test_project_folders_limit_device_tools(store, tmp_path):
    hub = DeviceHub(store, LocalBus())
    dev, key = await hub.create("u1", "Ноутбук")
    bridge = make_bridge(tmp_path, shell=True)
    (tmp_path / "docs" / "b.txt").write_text("поверка в docs", encoding="utf-8")
    picked = tmp_path / "picked"
    picked.mkdir()

    async def fake_bridge():
        d = await hub.auth(key)
        while True:
            await hub.touch(d, bridge.info())
            cmd = await hub.next_command(d["id"], 0.2)
            if cmd:
                if cmd["op"] == "pick_folder":  # человек выбрал папку в окне
                    f = bridge.add_folder(picked, False)
                    res = {"name": f.name, "path": str(f.path), "read_only": False}
                else:
                    res = getattr(bridge, "op_" + cmd["op"])(**cmd["args"])
                await hub.touch(d, bridge.info())
                await hub.deliver(d["id"], cmd["id"], {"ok": True, "result": res})

    task = asyncio.create_task(fake_bridge())
    await asyncio.sleep(0.05)
    u = User(id="u1")
    p = await store.project_create("u1", "Поверка")
    s = await store.create_session("u1", project_id=p.id)
    tools = {t.name: t for t in device_tools(hub)}
    ctx = ToolContext(session_id=s.id, run_id="r", user=u, agent=None, store=store, emit=None)

    # без привязок проект видит все открытые папки, как обычный разговор
    assert {i["name"] for i in (await tools["pc_list"].call({}, ctx)).data["items"]} == {"work", "docs"}
    await hub.bind_folder(p.id, "u1", dev["id"], str((tmp_path / "work").resolve()))
    assert {i["name"] for i in (await tools["pc_list"].call({}, ctx)).data["items"]} == {"work"}
    res = await tools["pc_read"].call({"path": "docs/a.md"}, ctx)
    assert res.is_error and "не привязана" in res.content
    hits = (await tools["pc_search"].call({"query": "поверка"}, ctx)).data["hits"]
    assert {h["path"] for h in hits} == {"work/notes.txt"}  # docs/b.txt не ищется
    assert str(tmp_path / "work") in (await tools["pc_run"].call({"command": "pwd" if sys.platform != "win32" else "cd"}, ctx)).content
    tctx = await hub.context(u, s.id)
    assert "work" in tctx.text and "docs" not in tctx.text
    # обычный разговор этого же человека по-прежнему видит всё
    plain = ToolContext(session_id="s", run_id="r", user=u, agent=None, store=store, emit=None)
    assert not (await tools["pc_read"].call({"path": "docs/a.md"}, plain)).is_error

    # папка, открытая из разговора проекта, сразу привязывается к нему
    res = await tools["pc_request_access"].call({"reason": "для отчёта"}, ctx)
    assert "Привязана к проекту" in res.content
    view = await hub.project_folders_view(p.id, "u1")
    assert {b["name"] for b in view} == {"work", "picked"} and all(b["open"] and b["online"] for b in view)

    # человек закрыл папку на компьютере — доступа нет, хотя привязка осталась
    bridge.op_remove_folder("work")
    bridge.op_remove_folder("picked")
    await asyncio.sleep(0.3)
    await hub.update(dev["id"], info=bridge.info())
    res = await tools["pc_list"].call({}, ctx)
    assert res.is_error and "закрыты" in res.content
    assert await hub.unbind_folder(p.id, "u1", view[0]["id"])
    await hub.drop_project(p.id)
    assert await hub.project_folders(p.id, "u1") == []
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


def test_bridge_setup_config_and_access_dialogs(tmp_path, monkeypatch):
    import agentkit.bridge as br

    cfg = tmp_path / "bridge-dev_1.json"
    work = tmp_path / "Документы"
    work.mkdir()
    b = br.Bridge("https://agent.example", "dev_1.secret", [], False, config=cfg)
    # окно выбора на компьютере: человек выбрал папку и разрешил запись
    monkeypatch.setattr(br, "pick_folder", lambda prompt: str(work))
    answers = iter([True])
    monkeypatch.setattr(br, "confirm", lambda text: next(answers))
    r = b.op_pick_folder(reason="найти договоры")
    assert r == {"name": "Документы", "path": str(work.resolve()), "read_only": False}
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["key"] == "dev_1.secret" and saved["folders"][0]["read_only"] is False

    # расширить доступ без согласия на компьютере нельзя, сузить — можно
    monkeypatch.setattr(br, "confirm", lambda text: False)
    with pytest.raises(br.BridgeError, match="нет"):
        b.op_set_shell(enabled=True)
    b.op_set_folder(name="Документы", read_only=True)
    with pytest.raises(br.BridgeError, match="нет"):
        b.op_set_folder(name="Документы", read_only=False)
    b.op_remove_folder(name="Документы")
    assert json.loads(cfg.read_text(encoding="utf-8"))["folders"] == []
    # окно закрыли — доступа нет
    monkeypatch.setattr(br, "pick_folder", lambda prompt: None)
    with pytest.raises(br.BridgeError, match="закрыл"):
        b.op_pick_folder()


async def test_show_visual_validates_and_returns_widget(store):
    from agentkit.tools.visual import visual_tool

    t = visual_tool()
    ctx = ToolContext(session_id="s", run_id="r", user=User(id="u"), agent=None, store=store, emit=None)
    ok = await t.call({"title": "Поверки", "blocks": [
        {"type": "kpi", "label": "За октябрь", "value": 412, "delta": 12.4},
        {"type": "chart", "kind": "line", "x": ["сен", "окт"], "series": [{"name": "Вода", "values": [281, 302]}]},
    ]}, ctx)
    assert not ok.is_error and ok.ui[0]["type"] == "visual" and "Показано" in ok.content
    bad = await t.call({"blocks": [{"type": "chart", "kind": "bar", "x": ["a", "b"], "series": [{"name": "s", "values": [1]}]}]}, ctx)
    assert bad.is_error and "1 значений" in bad.content
    pie = await t.call({"blocks": [{"type": "chart", "kind": "donut", "x": list("abcdefghi"),
                                    "series": [{"name": "s", "values": [1] * 9}]}]}, ctx)
    assert pie.is_error and "Прочее" in pie.content
    html = await t.call({"title": "схема", "html": "<svg></svg>"}, ctx)
    assert html.ui[0]["html"] == "<svg></svg>"
    # схема без $ref — её понимают все провайдеры
    import json as _json
    assert "$ref" not in _json.dumps(t.schema())


def test_bridge_setup_checks_connection_and_background(tmp_path, monkeypatch):
    # Не тот адрес — настройка говорит об этом сразу, а не «готово» с молчащим фоновым мостом.
    b = Bridge("http://127.0.0.1:1", "k", [], False)
    assert "нет связи с http://127.0.0.1:1" in b.hello()

    from agentkit import bridge as br

    monkeypatch.setattr(br.time, "sleep", lambda s: None)
    log = tmp_path / "bridge.log"
    log.write_text("старое: подключено\n", encoding="utf-8")
    start = log.stat().st_size
    assert not br.wait_background(log, start, timeout=0.05)  # старые строки не считаются
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("Traceback: что-то сломалось\n[12:00:00] подключено — агент видит этот компьютер\n")
    assert br.wait_background(log, start, timeout=1)
    assert br.log_tail(log, start).startswith("Traceback")
