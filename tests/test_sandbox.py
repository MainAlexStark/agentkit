"""Компьютер агента: песочница (local/docker), фоновые задачи, прокси egress, show_visual из файла."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentkit import User
from agentkit.egress import Egress, allowed, parse_allow, public
from agentkit.permissions import Decision, Mode, Permissions
from agentkit.sandbox import DockerBackend, execute
from agentkit.tools.base import ToolContext
from agentkit.tools.files import write_file
from agentkit.tools.sandbox import network_note, sandbox_tools
from agentkit.tools.visual import visual_tool

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="bash и nohup")


def ctx_for(store, ws: Path | None, session: str = "s_1") -> ToolContext:
    return ToolContext(session_id=session, run_id="r", user=User(id="u"), agent=None, store=store, emit=None,
                       workspace=ws)


@posix_only
def test_local_background_job_outlives_call(tmp_path):
    r = execute(tmp_path, "s_1", "bash", "echo старт; sleep 0.3; echo готово > done.txt; echo конец", 30,
                background=True)
    assert r["exit_code"] == 0 and r["job"]["pid"].isdigit()
    log = tmp_path / "s_1" / r["job"]["log"]
    for _ in range(50):
        if (tmp_path / "s_1" / "done.txt").exists() and "конец" in log.read_text():
            break
        time.sleep(0.1)
    assert "старт" in log.read_text() and "конец" in log.read_text()
    # служебное (.jobs) не попадает в список файлов для чата
    assert all(not f["path"].startswith(".") for f in r["files"])


@posix_only
def test_local_timeout_kills_whole_tree(tmp_path):
    t0 = time.monotonic()
    r = execute(tmp_path, "s_1", "bash", "sleep 20 & sleep 20", 1)
    assert r["exit_code"] is None and "прервано" in r["stderr"] and time.monotonic() - t0 < 10


class FakeDocker:
    """Запоминает команды docker и отвечает, как настоящий: контейнера нет → run, есть → exec."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.running: set[str] = set()
        self.exec_code = 0

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        op = cmd[1]
        ok = subprocess.CompletedProcess(cmd, 0, b"", b"")
        if op == "inspect":
            return (subprocess.CompletedProcess(cmd, 0, b"true\n", b"") if cmd[-1] in self.running
                    else subprocess.CompletedProcess(cmd, 1, b"", b"No such object"))
        if op == "run":
            self.running.add(cmd[cmd.index("--name") + 1])
        elif op == "rm":
            self.running.discard(cmd[-1])
        elif op == "exec":
            return subprocess.CompletedProcess(cmd, self.exec_code, "привет\n".encode(), b"")
        elif op == "ps":
            return subprocess.CompletedProcess(cmd, 0, b"s_old\n", b"")
        return ok


def test_docker_backend_container_per_session(tmp_path):
    fake = FakeDocker()
    b = DockerBackend("agentkit-sandbox", volume="agent-service_data", network="agent-sandbox-net",
                      proxy="http://egress:3128", idle_s=60, runner=fake)
    r = execute(tmp_path, "s_1", "python", "print('привет')", 30, backend=b)
    assert r["exit_code"] == 0 and "привет" in r["stdout"]
    run = next(c for c in fake.calls if c[1] == "run")
    # изоляция: без root, без capabilities, только чтение корня, своя папка разговора, сеть только к прокси
    for flag in ("--read-only", "--init", "no-new-privileges"):
        assert flag in run
    assert run[run.index("--cap-drop") + 1] == "ALL" and run[run.index("--user") + 1] == "1000:1000"
    assert "type=volume,src=agent-service_data,dst=/work,volume-subpath=workspaces/s_1" in run
    assert run[run.index("--network") + 1] == "agent-sandbox-net"
    assert "HTTPS_PROXY=http://egress:3128" in run and "PIP_USER=1" in run
    ex = next(c for c in fake.calls if c[1] == "exec")
    assert ex[ex.index("ak-sb-s_1") + 1:ex.index("ak-sb-s_1") + 5] == ["timeout", "-k", "5", "30"]
    # второй вызов — тот же контейнер, без нового run
    execute(tmp_path, "s_1", "bash", "ls", 30, backend=b)
    assert sum(c[1] == "run" for c in fake.calls) == 1
    # другой разговор — другой контейнер
    execute(tmp_path, "s_2", "bash", "ls", 30, backend=b)
    assert {c[c.index("--name") + 1] for c in fake.calls if c[1] == "run"} == {"ak-sb-s_1", "ak-sb-s_2"}
    # таймаут внутри контейнера (код 124) — «прервано», а не ошибка программы
    fake.exec_code = 124
    assert execute(tmp_path, "s_1", "bash", "sleep 99", 2, backend=b)["exit_code"] is None
    # простой: контейнеры удаляются, состояние остаётся в папке
    b.adopt()
    assert "s_old" in b.last
    gone = b.reap(now=time.monotonic() + 120)
    assert set(gone) == {"s_1", "s_2", "s_old"} and not fake.running


def test_docker_backend_bind_mount_for_laptop(tmp_path):
    b = DockerBackend("img", host_root=str(tmp_path), runner=FakeDocker())
    assert b.mount("s_9") == f"type=bind,src={tmp_path / 's_9'},dst=/work"
    assert "--network" in (args := b.run_args("s_9")) and args[args.index("--network") + 1] == "none"
    with pytest.raises(ValueError):
        DockerBackend("img")  # ни тома, ни папки — некуда монтировать


@posix_only
async def test_sandbox_tools_local_mode_sees_written_files(store, tmp_path):
    ws = tmp_path / "workspaces" / "s_1"
    ctx = ctx_for(store, ws)
    await write_file.call({"path": "data.csv", "content": "a\n1\n2\n"}, ctx)
    tools = {t.name: t for t in sandbox_tools("local")}
    # без изоляции — по подтверждению; в настоящей песочнице — без вопросов
    assert not tools["run_python"].read_only and sandbox_tools("http://sandbox:8090")[0].read_only
    res = await tools["run_python"].call({"code": "print(sum(int(x) for x in open('data.csv').read().split()[1:]))"},
                                         ctx)
    assert not res.is_error and "3" in res.content
    bg = await tools["run_bash"].call({"command": "sleep 0.1", "background": True}, ctx)
    assert "в фоне" in bg.content and ".jobs/" in bg.content


def test_network_note_tells_model_about_internet():
    assert "нет" in network_note("http://sandbox:8090", "")
    assert "pypi.org" in network_note("http://sandbox:8090", "pypi.org, files.pythonhosted.org")
    assert "локальная" in network_note("local", "")


def test_egress_allowlist_and_private_ips():
    allow = parse_allow("pypi.org, *.pythonhosted.org")
    assert allowed("pypi.org", allow) and allowed("files.pythonhosted.org", allow)
    assert not allowed("evil-pypi.org", allow) and not allowed("example.com", allow)
    assert allowed("anything.example", ["*"])
    for ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "::1", "::ffff:10.0.0.1"):
        assert not public(ip), ip
    assert public("1.1.1.1")


async def _http_get(proxy_port: int, url: str, host: str) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
    w.write(f"GET {url} HTTP/1.1\r\nHost: {host}\r\nProxy-Authorization: x\r\n\r\n".encode())
    await w.drain()
    data = await asyncio.wait_for(r.read(), 5)
    w.close()
    return data


async def test_egress_proxy_forwards_allowed_and_blocks_rest():
    seen: list[bytes] = []

    async def upstream(r, w):
        head = await r.readuntil(b"\r\n\r\n")
        seen.append(head)
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await w.drain()
        w.close()

    up = await asyncio.start_server(upstream, "127.0.0.1", 0)
    up_port = up.sockets[0].getsockname()[1]
    open_eg = Egress(["localhost"], ports={up_port}, block_private=False, log=lambda m: None)
    srv = await asyncio.start_server(open_eg.handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    ok = await _http_get(port, f"http://localhost:{up_port}/simple/", f"localhost:{up_port}")
    assert ok.startswith(b"HTTP/1.1 200") and ok.endswith(b"ok")
    # наверх уходит путь, а не абсолютный адрес, и без заголовков прокси
    assert seen[0].startswith(b"GET /simple/ HTTP/1.1") and b"Proxy-Authorization" not in seen[0]
    denied = await _http_get(port, "http://example.com/", "example.com")
    assert denied.startswith(b"HTTP/1.1 403") and "нет в списке".encode() in denied
    # CONNECT (https) к разрешённому — туннель
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"CONNECT localhost:{up_port} HTTP/1.1\r\n\r\n".encode())
    await w.drain()
    assert (await r.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
    w.close()
    srv.close()
    # с защитой от внутренней сети localhost не пройдёт, даже если он в списке
    strict = Egress(["localhost"], ports={up_port}, log=lambda m: None)
    srv2 = await asyncio.start_server(strict.handle, "127.0.0.1", 0)
    port2 = srv2.sockets[0].getsockname()[1]
    blocked = await _http_get(port2, f"http://localhost:{up_port}/", "localhost")
    assert blocked.startswith(b"HTTP/1.1 403") and "внутреннюю сеть".encode() in blocked
    srv2.close()
    up.close()


async def test_show_visual_from_file_and_lost_html_hint(store, tmp_path):
    t = visual_tool()
    ctx = ctx_for(store, tmp_path)
    lost = await t.call({"height": 480}, ctx)
    assert lost.is_error and "write_file" in lost.content and "path" in lost.content
    (tmp_path / "dash.html").write_text("<div>дашборд</div>", encoding="utf-8")
    ok = await t.call({"path": "dash.html", "height": 400}, ctx)
    assert not ok.is_error and ok.ui[0]["html"] == "<div>дашборд</div>" and ok.ui[0]["height"] == 400
    missing = await t.call({"path": "нет.html"}, ctx)
    assert missing.is_error and "write_file" in missing.content
    outside = await t.call({"path": "../../etc/passwd"}, ctx)
    assert outside.is_error


def test_workspace_edits_skip_approval_but_not_plan_mode():
    p = Permissions(workspace_edits=True)
    assert p.check(write_file, {"path": "a.html", "content": ""}).decision == Decision.ALLOW
    assert p.with_mode(Mode.PLAN).check(write_file, {"path": "a", "content": ""}).decision == Decision.DENY
    assert Permissions().check(write_file, {"path": "a", "content": ""}).decision == Decision.ASK
