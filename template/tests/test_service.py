"""Сквозной тест сервиса: HTTP → прогон → подтверждение → результат. Модель — FakeLLM."""

from __future__ import annotations

import json
import time

import pytest
from agentkit import FakeLLM, ModelRouter
from agentkit.llm.router import RetryPolicy
from agentkit.server.app import build_app
from agentkit.server.service import AgentService
from agentkit.server.settings import Settings
from fastapi.testclient import TestClient

from app.domain import build_domain

H = {"Authorization": "Bearer t-alex"}


@pytest.fixture
def client(tmp_path):
    fake = FakeLLM(default="ok")
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path}/a.db", tokens="alex:t-alex:admin,bob:t-bob:",
                        service_token="svc", mcp_config=tmp_path / "none.json", scheduler=False)
    router = ModelRouter(providers={"fake": fake}, roles={"main": "fake:m", "small": "fake:s"},
                         retry=RetryPolicy(attempts=1))
    svc = AgentService(settings, build_domain(settings), router=router)
    with TestClient(build_app(svc)) as c:
        c.fake = fake
        yield c


def wait_idle(c, sid, headers=H):
    for _ in range(200):
        if not c.get(f"/api/sessions/{sid}", headers=headers).json()["running_run"]:
            return
        time.sleep(0.02)
    raise AssertionError("прогон не закончился")


def test_auth(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/me", headers=H).json()["roles"] == ["admin"]
    r = client.get("/api/me", headers={"Authorization": "Bearer svc", "X-Agent-User-Id": "ei-42", "X-Agent-Roles": "metrolog"})
    assert r.json() == {"id": "ei-42", "name": "ei-42", "call_me": "", "roles": ["metrolog"]}
    assert client.get("/healthz").json()["ok"]


def test_chat_with_approval(client):
    client.fake.push({"tool_calls": [("notes_add", {"text": "Позвонить"})]}, "Записал #1.")
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "запиши: позвонить"}, headers=H)
    assert r.status_code == 200
    wait_idle(client, sid)
    s = client.get(f"/api/sessions/{sid}", headers=H).json()
    assert len(s["approvals"]) == 1 and s["approvals"][0]["view"] == [["Новая заметка", "Позвонить"]]

    r = client.post(f"/api/approvals/{s['approvals'][0]['id']}", json={"approve": True}, headers=H)
    assert r.status_code == 200
    wait_idle(client, sid)
    s = client.get(f"/api/sessions/{sid}", headers=H).json()
    assert s["approvals"] == []
    assert s["messages"][-1]["content"] == "Записал #1."
    # время записи — для разделителей дней и времени у сообщения в ленте
    assert all(isinstance(m["ts"], float) and m["ts"] > 0 for m in s["messages"])

    # события доступны и опросом, и через SSE
    polled = client.get(f"/api/sessions/{sid}/poll?after=0", headers=H).json()["events"]
    assert "approval_required" in [e["type"] for e in polled]
    with client.stream("GET", f"/api/sessions/{sid}/events?after=0", headers=H) as r:
        body = "".join(r.iter_text())
    types = [json.loads(line[5:])["type"] for line in body.splitlines() if line.startswith("data:") and line != "data: {}"]
    assert types.count("run_finished") == 2


def test_isolation_between_users(client):
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    bob = {"Authorization": "Bearer t-bob"}
    assert client.get(f"/api/sessions/{sid}", headers=bob).status_code == 404
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": "x"}, headers=bob).status_code == 404
    assert client.post("/api/sessions", json={"mode": "bypass"}, headers=bob).status_code == 403


def test_headless_run(client):
    client.fake.push({"tool_calls": [("notes_search", {})]}, "Заметок нет.")
    r = client.post("/api/run", json={"text": "что в заметках?"}, headers=H).json()
    assert r["status"] == "done" and r["text"] == "Заметок нет."


def test_memory_tools_schedules(client):
    assert client.post("/api/memory", json={"text": "Отвечать кратко"}, headers=H).status_code == 200
    assert [m["text"] for m in client.get("/api/memory", headers=H).json()] == ["Отвечать кратко"]
    tools = client.get("/api/tools", headers=H).json()
    names = {t["name"] for t in tools["tools"]}
    assert {"notes_search", "notes_add", "task", "todo_write", "skill"} <= names
    assert tools["skills"][0]["name"] == "weekly-review"
    assert client.post("/api/schedules", json={"name": "x", "cron": "bad", "prompt": "p"}, headers=H).status_code == 400
    r = client.post("/api/schedules", json={"name": "Утро", "cron": "45 8 * * 1-5", "prompt": "обзор"}, headers=H)
    assert r.status_code == 200 and r.json()["next_run_at"] > time.time()


def test_busy_session_conflict(client):
    # Второй запрос сразу после первого: если первый ещё идёт — 409, а не гонка двух прогонов.
    client.fake.push({"tool_calls": [("notes_search", {})]}, "ok")
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "a"}, headers=H)
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "b"}, headers=H)
    assert r.status_code in (200, 409)  # 409, если первый ещё идёт
    wait_idle(client, sid)


def test_message_while_running_is_queued_and_answered(client):
    import asyncio

    async def slow(req):
        await asyncio.sleep(0.5)  # первый шаг «думает» — второе сообщение приходит во время работы
        return "Ответ на первое"

    client.fake.push(slow, "Ответ на второе")
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    r1 = client.post(f"/api/sessions/{sid}/messages", json={"text": "первое"}, headers=H).json()
    r2 = client.post(f"/api/sessions/{sid}/messages", json={"text": "второе"}, headers=H).json()
    assert r1["queued"] is False and r2["queued"] is True
    for _ in range(500):
        s = client.get(f"/api/sessions/{sid}", headers=H).json()
        if not s["running_run"] and not s["queued"] and s["messages"] and s["messages"][-1]["role"] == "assistant" \
                and sum(m["role"] == "user" for m in s["messages"]) == 2:
            break
        time.sleep(0.02)
    texts = [(m["role"], m["content"]) for m in s["messages"]]
    # Второе сообщение агент прочёл либо после первого ответа (отдельный шаг), либо — если оно
    # успело до первого запроса к модели — вместе с первым. Главное: прочитано и отвечено.
    users = [c for r, c in texts if r == "user"]
    assert users == ["первое", "второе"] and texts[-1][0] == "assistant"
    assert texts.index(("user", "второе")) < len(texts) - 1


def test_upload_and_send_file(client):
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    r = client.post(f"/api/sessions/{sid}/files", files={"file": ("счёт.txt", "Итого: 1500 ₽".encode(), "text/plain")},
                    headers=H)
    assert r.status_code == 200
    info = r.json()
    assert info["path"].startswith("uploads/") and info["kind"] == "text"
    client.fake.push("Сумма 1500 ₽")
    client.post(f"/api/sessions/{sid}/messages", json={"text": "сколько?", "files": [info]}, headers=H)
    wait_idle(client, sid)
    sent = client.fake.requests[-1].messages[1].content
    assert any("1500" in p.get("text", "") for p in sent)
    got = client.get(f"/api/sessions/{sid}/files", params={"path": info["path"]}, headers=H)
    assert got.status_code == 200 and "1500" in got.text
    # чужой путь не пролезет ни в сообщение, ни в скачивание
    bad = {**info, "path": "../../a.db"}
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": "x", "files": [bad]}, headers=H).status_code == 400
    assert client.get(f"/api/sessions/{sid}/files", params={"path": "../a.db"}, headers=H).status_code == 404


def test_oauth_callback_rejects_unknown_state(client):
    r = client.get("/oauth/callback", params={"state": "nope", "code": "x"})
    assert r.status_code == 200 and "устарела" in r.text
    assert client.post("/api/mcp/none/connect", headers={"Authorization": "Bearer t-bob"}).status_code == 403


def test_profile_and_plan(client):
    assert client.get("/api/profile", headers=H).json()["name"] == "alex"
    r = client.put("/api/profile", json={"name": "Алексей", "call_me": "Алекс", "work": "метролог", "instructions": "Коротко"},
                   headers=H)
    assert r.status_code == 200
    me = client.get("/api/me", headers=H).json()
    assert me["name"] == "Алексей" and me["call_me"] == "Алекс"
    # профиль попадает в системный промпт
    client.fake.push("ок")
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "привет"}, headers=H)
    wait_idle(client, sid)
    system = client.fake.requests[-1].messages[0].text
    assert "Обращаться: Алекс" in system and "Коротко" in system
    plan = client.get("/api/plan", headers=H).json()
    assert plan["name"] and "usage" in plan


def test_models_catalog_prefs_and_policy(client):
    # Каталог провайдера в формате OpenRouter: одна модель без инструментов — агенту не годится.
    async def list_models():
        return [
            {"id": "a/smart", "name": "A: Smart", "context_length": 200000, "pricing": {"prompt": "0.000003", "completion": "0.000015"},
             "architecture": {"input_modalities": ["text", "image"]}, "supported_parameters": ["tools", "reasoning"]},
            {"id": "b/cheap", "name": "B: Cheap", "context_length": 32000, "pricing": {"prompt": "0", "completion": "0"},
             "supported_parameters": ["tools"]},
            {"id": "c/chat", "name": "C: Chat only", "supported_parameters": ["temperature"]},
        ]
    client.fake.list_models = list_models
    bob = {"Authorization": "Bearer t-bob"}
    d = client.get("/api/models?refresh=1", headers=H).json()
    ids = {m["id"]: m for m in d["models"]}
    assert "fake:a/smart" in ids and "fake:c/chat" not in ids
    smart = ids["fake:a/smart"]
    assert smart["name"] == "Smart" and smart["vision"] and smart["reasoning"] and smart["price_in"] == 3.0

    # избранное — у каждого своё
    r = client.put("/api/models/prefs", json={"favorites": ["fake:b/cheap"], "only_favorites": True}, headers=bob)
    assert r.json()["favorites"] == ["fake:b/cheap"] and r.json()["only_favorites"]
    assert client.get("/api/config", headers=bob).json()["models"] == ["fake:b/cheap"]
    assert client.get("/api/models", headers=H).json()["only_favorites"] is False

    # админ оставил только одну модель — остальные пропадают у пользователей и не проходят в API
    assert client.put("/api/models/policy", json={"ids": ["fake:a/smart"]}, headers=bob).status_code == 403
    client.put("/api/models/policy", json={"ids": ["fake:a/smart"]}, headers=H)
    seen = {m["id"] for m in client.get("/api/models", headers=bob).json()["models"]}
    assert "fake:b/cheap" not in seen and "fake:a/smart" in seen
    sid = client.post("/api/sessions", json={}, headers=bob).json()["id"]
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "x", "model": "fake:b/cheap"}, headers=bob)
    assert r.status_code == 400 and "недоступна" in r.json()["detail"]
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": "x", "model": "nope:z"}, headers=bob).status_code == 400
    admin_view = {m["id"]: m["allowed"] for m in client.get("/api/models", headers=H).json()["models"]}
    assert admin_view["fake:b/cheap"] is False
    client.put("/api/models/policy", json={"ids": None}, headers=H)
    assert "fake:b/cheap" in {m["id"] for m in client.get("/api/models", headers=bob).json()["models"]}


def test_memory_edit_and_schedule_lifecycle(client):
    bob = {"Authorization": "Bearer t-bob"}
    m = client.post("/api/memory", json={"text": "Кратко"}, headers=bob).json()
    assert client.patch(f"/api/memory/{m['id']}", json={"text": "Очень кратко"}, headers=bob).status_code == 200
    assert client.get("/api/memory", headers=bob).json()[0]["text"] == "Очень кратко"
    g = client.post("/api/memory", json={"text": "Общее", "scope": "global"}, headers=H).json()
    assert client.patch(f"/api/memory/{g['id']}", json={"text": "x"}, headers=bob).status_code == 403

    p = client.get("/api/schedules/preview", params={"cron": "0 9 * * 1-5"}, headers=bob).json()
    assert p["ok"] and len(p["next"]) == 3 and p["next"][0] < p["next"][1]
    assert client.get("/api/schedules/preview", params={"cron": "x"}, headers=bob).json()["ok"] is False
    s = client.post("/api/schedules", json={"name": "Утро", "cron": "0 9 * * *", "prompt": "обзор"}, headers=bob).json()
    s2 = client.post("/api/schedules", json={"id": s["id"], "name": "Утро", "cron": "30 9 * * *", "prompt": "обзор",
                                             "keep_session": True}, headers=bob).json()
    assert s2["id"] == s["id"] and s2["cron"] == "30 9 * * *" and s2["session_id"]
    assert client.patch(f"/api/schedules/{s['id']}", json={"enabled": False}, headers=bob).json()["enabled"] is False
    client.fake.push("Обзор готов")
    assert client.post(f"/api/schedules/{s['id']}/run", headers=bob).status_code == 200
    wait_idle(client, s2["session_id"], headers=bob)
    for _ in range(100):
        msgs = client.get(f"/api/sessions/{s2['session_id']}", headers=bob).json()["messages"]
        if msgs and msgs[-1]["content"] == "Обзор готов":
            break
        time.sleep(0.02)
    assert msgs[-1]["content"] == "Обзор готов"
    assert client.delete(f"/api/schedules/{s['id']}", headers={"Authorization": "Bearer t-alex"}).status_code == 200


def test_custom_mcp_servers(client):
    bob = {"Authorization": "Bearer t-bob"}
    body = {"name": "crm", "url": "http://127.0.0.1:9/mcp", "auth": "token", "token": "secret-1"}
    assert client.post("/api/mcp/servers", json=body, headers=bob).status_code == 403
    assert client.post("/api/mcp/servers", json={**body, "name": "Bad Name"}, headers=H).status_code == 400
    assert client.post("/api/mcp/servers", json={**body, "url": "ftp://x"}, headers=H).status_code == 400
    assert client.post("/api/mcp/servers", json={**body, "token": ""}, headers=H).status_code == 400
    t = client.post("/api/mcp/test", json=body, headers=H).json()
    assert t["ok"] is False and t["error"]
    assert client.post("/api/mcp/servers", json=body, headers=H).status_code == 200
    assert client.post("/api/mcp/servers", json=body, headers=H).status_code == 409  # имя занято

    mcp = {s["name"]: s for s in client.get("/api/tools", headers=H).json()["mcp"]}
    crm = mcp["crm"]
    assert crm["custom"] and crm["auth"] == "token" and crm["has_token"] and not crm["ok"]
    assert "secret-1" not in json.dumps(mcp)  # токен наружу не уходит
    assert "address" not in client.get("/api/tools", headers=bob).json()["mcp"][0]

    # правка без токена сохраняет прежний; переименование
    r = client.post("/api/mcp/servers", json={"name": "crm2", "original_name": "crm", "url": body["url"], "auth": "token"}, headers=H)
    assert r.status_code == 200
    mcp = {s["name"]: s for s in client.get("/api/tools", headers=H).json()["mcp"]}
    assert "crm" not in mcp and mcp["crm2"]["has_token"]
    assert client.patch("/api/mcp/servers/crm2", json={"enabled": False}, headers=H).status_code == 200
    assert client.get("/api/tools", headers=H).json()["mcp"][0]["enabled"] is False
    assert client.delete("/api/mcp/servers/crm2", headers=H).status_code == 200
    assert client.get("/api/tools", headers=H).json()["mcp"] == []


def test_projects_api_and_prompt(client):
    bob = {"Authorization": "Bearer t-bob"}
    p = client.post("/api/projects", json={"name": "Поверка воды", "instructions": "Ссылайся на МИ 1592-2015"},
                    headers=H).json()
    f = client.post(f"/api/projects/{p['id']}/files", files={"file": ("сроки.txt", "МПИ счётчиков — 6 лет".encode(), "text/plain")},
                    headers=H).json()
    assert f["text_chars"] > 0
    # чужому проект не виден, пока не открыли доступ
    assert client.get(f"/api/projects/{p['id']}", headers=bob).status_code == 404
    assert client.post("/api/sessions", json={"project_id": p["id"]}, headers=bob).status_code == 404
    assert client.put(f"/api/projects/{p['id']}/members", json={"user_id": "bob", "role": "viewer"}, headers=bob).status_code == 404
    client.put(f"/api/projects/{p['id']}/members", json={"user_id": "bob", "role": "viewer"}, headers=H)
    d = client.get(f"/api/projects/{p['id']}", headers=bob).json()
    assert d["role"] == "viewer" and d["files"][0]["name"] == "сроки.txt" and d["members"] == []
    assert client.patch(f"/api/projects/{p['id']}", json={"name": "x"}, headers=bob).status_code == 403
    assert client.get(f"/api/projects/{p['id']}/files/{f['id']}", headers=bob).content == "МПИ счётчиков — 6 лет".encode()

    # разговор в проекте видит инструкции и файл
    client.fake.push("ок")
    sid = client.post("/api/sessions", json={"project_id": p["id"]}, headers=bob).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "какой МПИ?"}, headers=bob)
    wait_idle(client, sid, headers=bob)
    system = client.fake.requests[-1].messages[0].text
    assert "МИ 1592-2015" in system and "6 лет" in system
    assert client.get(f"/api/sessions/{sid}", headers=bob).json()["project"]["name"] == "Поверка воды"
    listed = client.get("/api/sessions", headers=bob).json()
    assert listed[0]["project"] == "Поверка воды"
    assert [x["id"] for x in client.get("/api/projects", headers=bob).json()] == [p["id"]]
    # удаление проекта: разговоры остаются, но без проекта
    assert client.delete(f"/api/projects/{p['id']}", headers=bob).status_code == 403
    assert client.delete(f"/api/projects/{p['id']}", headers=H).status_code == 200
    assert client.get(f"/api/sessions/{sid}", headers=bob).json()["project"] is None


def test_devices_api(client):
    r = client.post("/api/devices", json={"name": "Ноутбук"}, headers=H).json()
    assert r["key"].startswith(r["device"]["id"] + ".") and r["bridge_url"].endswith("/bridge.py")
    assert client.get("/api/devices", headers=H).json()[0]["online"] is False
    assert client.get("/api/devices", headers={"Authorization": "Bearer t-bob"}).json() == []
    assert client.post("/api/bridge/poll", json={}, headers={"Authorization": "Bearer dev_x.y"}).status_code == 401
    assert "Мост agentkit" in client.get("/bridge.py").text
    client.delete(f"/api/devices/{r['device']['id']}", headers=H)
    assert client.post("/api/bridge/poll", json={}, headers={"Authorization": f"Bearer {r['key']}"}).status_code == 401


def test_connect_scripts_and_device_actions(client):
    sh = client.get("/connect.sh").text
    assert 'SERVER="http://testserver"' in sh and "--setup" in sh
    bat = client.get("/connect.bat").text
    assert "\r\n" in bat and "__KEY__" in bat and "http://testserver" in bat
    r = client.post("/api/devices", json={"name": "Ноут"}, headers=H).json()
    did = r["device"]["id"]
    # офлайн — действия не уходят; неизвестные — отклоняются
    assert client.post(f"/api/devices/{did}/action", json={"op": "pick_folder"}, headers=H).status_code == 409
    assert client.post(f"/api/devices/{did}/action", json={"op": "run", "args": {"command": "x"}}, headers=H).status_code == 400
    assert client.post(f"/api/devices/{did}/action", json={"op": "pick_folder"},
                       headers={"Authorization": "Bearer t-bob"}).status_code == 404


def test_file_preview_for_word(client, tmp_path):
    docx = pytest.importorskip("docx")
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    d = docx.Document()
    d.add_heading("Отчёт", level=1)
    d.add_paragraph("Поверок за октябрь: 412")
    ws = tmp_path / "workspaces" / sid
    ws.mkdir(parents=True, exist_ok=True)
    d.save(ws / "отчёт.docx")
    (ws / "x.bin").write_bytes(b"\0\1")
    r = client.get(f"/api/sessions/{sid}/files/preview", params={"path": "отчёт.docx"}, headers=H).json()
    assert r["kind"] == "doc" and r["blocks"][0] == {"type": "h", "level": 1, "text": "Отчёт"}
    assert client.get(f"/api/sessions/{sid}/files/preview", params={"path": "x.bin"}, headers=H).status_code == 415
    assert client.get(f"/api/sessions/{sid}/files/preview", params={"path": "../a.db"}, headers=H).status_code == 404
