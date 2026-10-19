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
    assert r.json() == {"id": "ei-42", "name": "ei-42", "roles": ["metrolog"]}
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
