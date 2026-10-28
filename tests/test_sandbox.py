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


@posix_only
def test_local_home_is_absolute_with_relative_root(tmp_path, monkeypatch):
    # Сервис из template/ с data_dir=data: HOME был относительным, и `cd ~/x` уводил во вложенную копию пути.
    monkeypatch.chdir(tmp_path)
    r = execute(Path("data/workspaces"), "s_1", "bash", "mkdir -p ~/lab && cd ~/lab && pwd", 30)
    assert r["exit_code"] == 0
    assert r["stdout"].strip() == str((tmp_path / "data/workspaces/s_1/lab").resolve())


class FakeDocker:
    """Запоминает команды docker и отвечает, как настоящий: контейнера нет → run, есть → exec."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.running: set[str] = set()
        self.exec_code = 0
        self.networks: dict[str, str] = {}
        self.connected: set[str] = set()
        self.busy_subnets: set[str] = set()

    def network(self, cmd):
        ok = subprocess.CompletedProcess(cmd, 0, b"", b"")
        sub, name = cmd[2], cmd[-1]
        if sub == "inspect":
            return ok if name in self.networks else subprocess.CompletedProcess(cmd, 1, b"", b"not found")
        if sub == "create":
            subnet = cmd[cmd.index("--subnet") + 1]
            if subnet in self.busy_subnets:
                return subprocess.CompletedProcess(cmd, 1, b"", b"Pool overlaps with other one on this address space")
            self.networks[name] = subnet
        elif sub == "connect":
            net = cmd[-2]
            if net in self.connected:
                return subprocess.CompletedProcess(cmd, 1, b"", b"endpoint with name egress already exists")
            self.connected.add(net)
        elif sub == "disconnect":
            self.connected.discard(cmd[-2])
        elif sub == "rm":
            self.networks.pop(name, None)
        elif sub == "ls":
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return ok

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        op = cmd[1]
        ok = subprocess.CompletedProcess(cmd, 0, b"", b"")
        if op == "network":
            return self.network(cmd)
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
    # Python — через ядро разговора: у клиента свой таймаут (30), у docker exec — с запасом на старт ядра
    i = ex.index("ak-sb-s_1")
    assert ex[i + 1:i + 5] == ["timeout", "-k", "5", "55"] and ex[i + 5:i + 10] == [
        "python", "-m", "agentkit.kernel", "run", ex[i + 9]] and ex[i + 10] == "30"
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


def test_docker_backend_network_per_session(tmp_path):
    import hashlib
    import ipaddress

    fake = FakeDocker()
    b = DockerBackend("img", volume="v", proxy="http://egress:3128", egress_container="agent-service-egress",
                      subnet_pool="10.213.0.0/16", runner=fake)
    # первая по счёту подсеть занята (чужая сеть на машине) — берётся следующая
    subnets = list(ipaddress.ip_network("10.213.0.0/16").subnets(new_prefix=28))
    first = subnets[int(hashlib.sha1(b"s_1").hexdigest(), 16) % len(subnets)]
    fake.busy_subnets.add(str(first))
    execute(tmp_path, "s_1", "bash", "ls", 30, backend=b)
    execute(tmp_path, "s_2", "bash", "ls", 30, backend=b)
    assert set(fake.networks) == {"ak-sbn-s_1", "ak-sbn-s_2"} and fake.networks["ak-sbn-s_1"] != str(first)
    assert ipaddress.ip_network(fake.networks["ak-sbn-s_1"]).subnet_of(ipaddress.ip_network("10.213.0.0/16"))
    create = next(c for c in fake.calls if c[1:3] == ["network", "create"])
    assert "--internal" in create
    run = next(c for c in fake.calls if c[1] == "run" and "ak-sb-s_1" in c)
    assert run[run.index("--network") + 1] == "ak-sbn-s_1"  # не общая сеть
    connect = next(c for c in fake.calls if c[1:3] == ["network", "connect"])
    assert connect[3:5] == ["--alias", "egress"] and connect[-1] == "agent-service-egress"
    # повторный вызов: egress уже подключён — это не ошибка
    execute(tmp_path, "s_1", "bash", "ls", 30, backend=b)
    b.reset("s_1")
    assert "ak-sbn-s_1" not in fake.networks and "ak-sbn-s_1" not in fake.connected


@pytest.fixture
def kernels(tmp_path):
    from agentkit.kernel import _kill, default_sock

    yield
    for d in tmp_path.iterdir():
        if d.is_dir():
            _kill(default_sock(d))


@posix_only
def test_kernel_keeps_state_between_calls(tmp_path, kernels):
    run = lambda code, **kw: execute(tmp_path, "s_k", "python", code, kw.pop("timeout", 30), **kw)  # noqa: E731
    first = run("import math\nrows = [1, 2, 3]\nprint('загружено', len(rows))")
    assert first["exit_code"] == 0 and "загружено 3" in first["stdout"]
    again = run("rows.append(4)\nsum(rows) * math.pi > 30")  # переменные живы, последнее выражение печатается
    assert again["exit_code"] == 0 and again["stdout"].strip() == "True"
    err = run("1 / 0")
    assert err["exit_code"] == 1 and "ZeroDivisionError" in err["stderr"] and "agentkit" not in err["stderr"]
    assert "1 / 0" in err["stderr"]  # строка ячейки в traceback
    assert run("print(len(rows))")["stdout"].strip() == "4"  # ошибка ячейки ядро не роняет
    assert run("raise SystemExit(3)")["exit_code"] == 3
    # зависшая ячейка: таймаут, ядро остановлено, переменных больше нет — и вывод это говорит
    hung = run("import time\ntime.sleep(30)", timeout=1)
    assert hung["exit_code"] is None and "переменные сброшены" in hung["stderr"]
    lost = run("print(rows)")
    assert lost["exit_code"] == 1 and "NameError" in lost["stderr"]
    run("x = 1")
    fresh = run("print('x' in dir())", reset=True)
    assert fresh["stdout"].strip() == "False" and "перезапущено" in fresh["stderr"]
    # старый режим — отдельный процесс с нуля
    run("y = 5")
    assert execute(tmp_path, "s_k", "python", "print(y)", 30, kernel=False)["exit_code"] == 1


@posix_only
def test_kernel_reports_when_it_died(tmp_path, kernels):
    import os
    import signal

    from agentkit.kernel import default_sock

    execute(tmp_path, "s_d", "python", "z = 1", 30)
    pid = int(Path(f"{default_sock(tmp_path / 's_d')}.pid").read_text())
    os.kill(pid, signal.SIGKILL)
    time.sleep(0.2)
    r = execute(tmp_path, "s_d", "python", "print('жив')", 30)
    assert r["exit_code"] == 0 and "жив" in r["stdout"] and "потеряны" in r["stderr"]


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
    assert (await write_file.call({"path": "data.csv", "content": "a\n1\n2\n"}, ctx)).ui
    assert not (await write_file.call({"path": ".probe.txt", "content": "x"}, ctx)).ui  # служебное — без карточки
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


def test_sandbox_is_agent_zone_in_every_writing_mode():
    """Песочница — зона агента: даже локальная (там run_python не read_only) не ждёт карточки."""
    run = {t.name: t for t in sandbox_tools("local")}["run_python"]
    assert not run.read_only
    for mode in (Mode.DEFAULT, Mode.AUTO, Mode.BYPASS):
        assert Permissions(mode=mode).check(run, {"code": "1"}).decision == Decision.ALLOW
    assert Permissions(mode=Mode.READONLY).check(run, {"code": "1"}).decision == Decision.DENY
    # свой выбор и правила сервиса сильнее зоны
    assert Permissions(tool_policy={"run_python": "ask"}).check(run, {"code": "1"}).decision == Decision.ASK
    assert Permissions(tool_policy={"run_python": "deny"}).check(run, {"code": "1"}).decision == Decision.DENY


def test_tool_policy_order():
    p = Permissions(tool_policy={"write_file": "allow"})
    assert p.check(write_file, {"path": "a", "content": ""}).decision == Decision.ALLOW
    assert p.with_mode(Mode.PLAN).check(write_file, {"path": "a", "content": ""}).decision == Decision.DENY
    # deny-правило сервиса не перебить «Всегда разрешать»
    strict = Permissions(deny=["write_file"], tool_policy={"write_file": "allow"})
    assert strict.check(write_file, {"path": "a", "content": ""}).decision == Decision.DENY
    # «Спрашивать» действует и в режиме «Без вопросов»
    asked = Permissions(mode=Mode.BYPASS, tool_policy={"write_file": "ask"})
    assert asked.check(write_file, {"path": "a", "content": ""}).decision == Decision.ASK
    assert asked.with_mode(Mode.AUTO).tool_policy == {"write_file": "ask"}
    assert asked.with_mode(Mode.AUTO, {}).tool_policy == {}


# ── превью серверов песочницы ────────────────────────────────────────

def _http_server():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: dict = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["headers"] = dict(self.headers)
            seen["path"] = self.path
            if self.path.startswith("/go"):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            body = "<h1>отчёт</h1>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "sid=1; Path=/")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


async def test_preview_link_proxies_with_sandbox_csp():
    import httpx
    from fastapi import FastAPI

    from agentkit.server.preview import SandboxPreview, register

    srv, seen = _http_server()
    port = srv.server_address[1]
    pv = SandboxPreview(b"k" * 32, public_url="http://agent.test", sandbox_url="local")
    svc = type("Svc", (), {"preview": pv})()
    app = FastAPI()
    register(app, svc)
    link = pv.link("s_1", port, "/report?x=1")
    assert link.startswith("http://agent.test/sandbox/p/") and pv.verify(link.split("/")[5]) == ("s_1", port)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent.test") as c:
        r = await c.get(link.removeprefix("http://agent.test"), headers={"Cookie": "chat=secret",
                                                                          "Authorization": "Bearer tok"})
        assert r.status_code == 200 and "отчёт" in r.text
        # страница с пустым origin: не достанет токен чата; куки и токен человека наверх не ушли
        assert r.headers["content-security-policy"].startswith("sandbox") and "allow-same-origin" not in \
            r.headers["content-security-policy"]
        assert "set-cookie" not in r.headers
        assert "Cookie" not in seen["headers"] and "Authorization" not in seen["headers"]
        assert seen["path"] == "/report?x=1"
        sig = link.split("/")[5]
        red = await c.get(f"/sandbox/p/{sig}/go")
        assert red.status_code == 302 and red.headers["location"] == f"/sandbox/p/{sig}/login"
        # подделка, чужой порт, просрочка
        assert (await c.get(f"/sandbox/p/{sig[:-2]}xx/")).status_code == 403
        old = pv.sign("s_1", port, ttl=-1)
        assert (await c.get(f"/sandbox/p/{old}/")).status_code == 403
        srv.shutdown()
        srv.server_close()
        dead = await c.get(f"/sandbox/p/{sig}/")
        assert dead.status_code == 502 and "ничего не отвечает" in dead.text
    assert await pv.check("s_1", port) is not None
    await pv.client.aclose()


async def test_sandbox_preview_tool_card(store, tmp_path):
    class FakePreview:
        async def check(self, session, port):
            return None if port == 8000 else f"на порту {port} ничего не слушает"

        def link(self, session, port, path="/"):
            return f"http://agent.test/sandbox/p/sig-{session}-{port}{path}"

    tools = {t.name: t for t in sandbox_tools("http://sandbox:8090", preview=FakePreview())}
    ctx = ctx_for(store, tmp_path / "s_7")
    ok = await tools["sandbox_preview"].call({"port": 8000, "title": "Дашборд"}, ctx)
    assert not ok.is_error and ok.ui[0] == {"type": "link", "url": "http://agent.test/sandbox/p/sig-s_7-8000/",
                                            "title": "Дашборд", "hint": "Компьютер агента · открыть в новой вкладке"}
    bad = await tools["sandbox_preview"].call({"port": 9000}, ctx)
    assert bad.is_error and "0.0.0.0" in bad.content and "background" in bad.content
    assert "sandbox_preview" not in {t.name for t in sandbox_tools("http://sandbox:8090")}


async def test_egress_reverse_needs_token_and_sandbox_target():
    eg = Egress([], token="t0k", log=lambda m: None)
    srv = await asyncio.start_server(eg.handle_reverse, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    async def ask(headers: str) -> bytes:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"GET / HTTP/1.1\r\nHost: x\r\n{headers}\r\n".encode())
        await w.drain()
        data = await asyncio.wait_for(r.read(), 5)
        w.close()
        return data

    assert (await ask("X-Sandbox-Target: ak-sb-s_1:8000\r\n")).startswith(b"HTTP/1.1 401")
    # к базе, Redis и прочему — нельзя: цель только контейнер разговора
    assert (await ask("X-Sandbox-Token: t0k\r\nX-Sandbox-Target: postgres:5432\r\n")).startswith(b"HTTP/1.1 400")
    assert (await ask("X-Sandbox-Token: t0k\r\nX-Sandbox-Target: ak-sb-s_1:8000\r\n")).startswith(b"HTTP/1.1 502")
    srv.close()


@posix_only
def test_installer_caches_not_listed_as_results(tmp_path):
    # Так pip на macOS кладёт кэш при HOME = папке разговора — в чат это попадало сотнями файлов.
    code = ("mkdir -p Library/Caches/pip/http-v2/a node_modules/x myenv && touch myenv/pyvenv.cfg myenv/lib.py"
            " && echo 1 > Library/Caches/pip/http-v2/a/abc.body && echo 1 > node_modules/x/i.js"
            " && echo \"$PIP_CACHE_DIR\" > cache.txt && echo ok > result.txt")
    r = execute(tmp_path, "s_1", "bash", code, 30)
    assert sorted(f["path"] for f in r["files"]) == ["cache.txt", "result.txt"]
    assert (tmp_path / "s_1" / "cache.txt").read_text().strip().endswith(".cache/pip")
