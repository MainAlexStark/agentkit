#!/usr/bin/env python3
"""Компьютер агента: Python и командная строка вне процесса сервиса.

Два способа запуска (--backend):

* docker — по контейнеру на разговор. Контейнер создаётся при первом вызове,
  живёт, пока разговор активен (фоновые процессы, установленные пакеты), и
  удаляется после простоя; всё ценное — в папке разговора (/work), поэтому
  следующий вызов поднимает новый контейнер с тем же состоянием. Сеть — только
  через прокси с белым списком доменов (agentkit.egress), без секретов, без
  базы, без root. Этот сервер — единственный, кто держит docker.sock; сервису
  агента сокет не нужен.
* local — процессы на этой же машине: для разработки и тестов. Изоляции между
  разговорами нет, сеть — как у машины.

    agentkit sandbox --root data/workspaces --port 8090                                   # local
    agentkit sandbox --backend docker --image agentkit-sandbox --host-root $PWD/data/workspaces   # docker на ноутбуке
    (на VPS — сервис sandbox в docker-compose шаблона)

Модуль — только стандартная библиотека: он же работает и внутри образа песочницы.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_OUT = 40_000
MAX_FILES_LISTED = 50
JOBS_DIR = ".jobs"
LABEL = "agentkit.sandbox"
# Папки, которые наполняют установщики и инструменты, а не агент. Без этого фильтра
# pip на macOS (HOME = папка разговора → кэш в ~/Library/Caches/pip) выкладывал в чат
# сотню файлов-хэшей и их .body; то же — venv без точки, node_modules, __pycache__.
SKIP_DIRS = {"Library", "AppData", "node_modules", "__pycache__", "site-packages", "venv"}


class SandboxError(RuntimeError):
    """Песочница не смогла выполнить команду (не путать с ненулевым кодом выхода самой программы)."""


def _limits(mem_mb: int, cpu_s: int):
    def apply() -> None:  # в дочернем процессе до exec
        try:
            os.setsid()  # своя группа процессов — по таймауту убиваем всё дерево, а не только bash
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 2**20, mem_mb * 2**20))
            # Мягкий лимит, жёсткий не трогаем: ядро Python (agentkit.kernel) живёт дольше вызова
            # и поднимает себе мягкий обратно — при жёстком = мягкому оно умирало бы через timeout секунд CPU.
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, resource.getrlimit(resource.RLIMIT_CPU)[1]))
            resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 2**20, 512 * 2**20))
        except Exception:  # noqa: BLE001 — на macOS часть лимитов недоступна
            pass
    return apply


def _snapshot(root: Path) -> dict[str, tuple[float, int]]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        # .home (пакеты pip), .jobs (логи фоновых задач) и прочие скрытые — служебное, в чат не показываем
        # Library, node_modules, venv — тоже служебное: см. SKIP_DIRS.
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
                       and not (Path(dirpath) / d / "pyvenv.cfg").exists()]
        for n in filenames:
            if n.startswith("."):
                continue
            p = Path(dirpath) / n
            try:
                st = p.stat()
            except OSError:
                continue
            out[p.relative_to(root).as_posix()] = (st.st_mtime, st.st_size)
    return out


# ── способы запуска ──────────────────────────────────────────────────

class LocalBackend:
    """Процессы на этой машине. Только для разработки: разговоры видят друг друга и всю машину."""

    name = "local"
    python = sys.executable

    def __init__(self, mem_mb: int = 2048):
        self.mem_mb = mem_mb

    def prepare(self, work: Path) -> None:
        work.mkdir(parents=True, exist_ok=True)

    def env(self, work: Path) -> dict[str, str]:
        from agentkit.kernel import default_sock  # noqa: PLC0415

        # Сокет ядра считает сервер, а не дочерний процесс: у того другое окружение (TMPDIR),
        # и сервер потом не нашёл бы ядро, чтобы остановить его.
        return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(work), "LANG": "C.UTF-8",
                # Кэши — в скрытую .cache: по умолчанию pip на macOS пишет в ~/Library/Caches,
                # то есть прямо в папку разговора, и его файлы попадали в чат как результаты.
                "XDG_CACHE_HOME": str(work / ".cache"), "PIP_CACHE_DIR": str(work / ".cache" / "pip"),
                "MPLBACKEND": "Agg", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
                "AK_KERNEL_SOCK": str(default_sock(work)),
                **({"AK_KERNEL_IDLE": os.environ["AK_KERNEL_IDLE"]} if "AK_KERNEL_IDLE" in os.environ else {})}

    def exec(self, session: str, work: Path, argv: list[str], timeout: int, background: bool = False
             ) -> tuple[int | None, bytes, bytes]:
        posix = os.name == "posix"
        # У фоновой задачи свой срок жизни: лимит CPU прогона её бы убил через timeout секунд.
        limits = _limits(self.mem_mb, 24 * 3600 if background else timeout + 5) if posix else None
        p = subprocess.Popen(argv, cwd=work, env=self.env(work), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             preexec_fn=limits)
        try:
            out, err = p.communicate(timeout=timeout)
            return p.returncode, out, err
        except subprocess.TimeoutExpired:
            if posix:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
            p.kill()
            out, err = p.communicate()
            return None, out, err + f"\nпрервано: дольше {timeout} с".encode()

    def info(self) -> dict[str, Any]:
        return {"backend": self.name, "python": sys.version.split()[0], "isolated": False}

    def reset(self, session: str) -> None:  # процессов-долгожителей у local нет — нечего сбрасывать
        return None


Runner = Callable[..., subprocess.CompletedProcess]


class DockerBackend:
    """По контейнеру на разговор через docker CLI.

    CLI, а не Docker API напрямую: команды читаются глазами и копируются в
    терминал при разборе («почему не стартует»), а тесты подменяют один runner.
    """

    name = "docker"
    python = "python"

    def __init__(self, image: str, *, volume: str = "", subpath: str = "workspaces", host_root: str = "",
                 network: str = "none", proxy: str = "", mem_mb: int = 2048, cpus: float = 1.0, pids: int = 256,
                 idle_s: int = 900, uid: int = 1000, docker: str = "docker", runner: Runner = subprocess.run,
                 egress_container: str = "", subnet_pool: str = "10.213.0.0/16"):
        if not image:
            raise ValueError("docker: нужен образ (--image / SANDBOX_IMAGE)")
        if not volume and not host_root:
            raise ValueError("docker: укажите, откуда брать папки разговоров: --volume (том) или --host-root (папка)")
        self.image, self.volume, self.subpath, self.host_root = image, volume, subpath.strip("/"), host_root
        self.network, self.proxy = network or "none", proxy
        self.mem_mb, self.cpus, self.pids, self.idle_s, self.uid = mem_mb, cpus, pids, idle_s, uid
        self.docker, self.runner = docker, runner
        # Своя сеть у каждого разговора: в общей сети контейнеры разговоров видели бы друг друга
        # (сервер, поднятый агентом одного пользователя, был бы открыт коду другого). В сети разговора —
        # только его контейнер и egress (подключается к каждой с именем egress).
        self.egress_container = egress_container
        self.pool = ipaddress.ip_network(subnet_pool)
        self.last: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @staticmethod
    def container(session: str) -> str:
        return f"ak-sb-{session}"

    @staticmethod
    def net_name(session: str) -> str:
        return f"ak-sbn-{session}"

    @property
    def per_session_net(self) -> bool:
        return bool(self.egress_container)

    def _ensure_net(self, session: str) -> None:
        net = self.net_name(session)
        if self._run("network", "inspect", net, timeout=20).returncode != 0:
            # Подсеть — своя /28 из пула: пулы docker по умолчанию кончаются примерно на 30 сетях,
            # а разговоров одновременно бывает больше. Занятые подсети docker отвергает — берём следующую.
            start = int(hashlib.sha1(session.encode()).hexdigest(), 16)
            subnets = list(self.pool.subnets(new_prefix=28))
            err = ""
            for i in range(64):
                sub = subnets[(start + i) % len(subnets)]
                r = self._run("network", "create", "--internal", "--label", f"{LABEL}=1",
                              "--label", f"{LABEL}.session={session}", "--subnet", str(sub), net, timeout=30)
                if r.returncode == 0:
                    break
                err = (r.stderr or b"").decode("utf-8", "replace")
                if "already exists" in err:
                    break
                if "overlap" not in err.lower():
                    raise SandboxError(f"не создать сеть разговора: {err.strip()[-500:]}")
            else:
                raise SandboxError(f"не создать сеть разговора: нет свободной подсети в {self.pool} ({err.strip()})")
        # Каждый раз, а не только при создании: пересозданный egress теряет подключения к сетям.
        r = self._run("network", "connect", "--alias", "egress", net, self.egress_container, timeout=30)
        if r.returncode != 0 and b"already exists" not in (r.stderr or b""):
            self._check(r, "не подключить egress к сети разговора")

    def _run(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        try:
            return self.runner([self.docker, *args], capture_output=True, timeout=timeout)
        except FileNotFoundError as e:
            raise SandboxError(f"нет команды {self.docker}: в образе сервера песочницы нужен docker CLI") from e
        except subprocess.TimeoutExpired as e:
            raise SandboxError(f"docker {args[0]}: нет ответа {timeout:.0f} с") from e

    def _check(self, r: subprocess.CompletedProcess, what: str) -> None:
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", "replace").strip()[-800:]
            raise SandboxError(f"{what}: {err or f'код {r.returncode}'}")

    def prepare(self, work: Path) -> None:
        # Подпапка тома должна существовать до docker run, и её владелец — uid песочницы:
        # сервер песочницы работает от root (ради docker.sock), иначе код внутри не смог бы писать.
        # .jobs — туда же: иначе лог фоновой задачи не записать («Permission denied» — нашли на живом docker).
        for d in (work, work / ".home", work / JOBS_DIR):
            d.mkdir(parents=True, exist_ok=True)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                os.chown(d, self.uid, self.uid)

    def env(self) -> dict[str, str]:
        env = {"HOME": "/work/.home", "PYTHONUSERBASE": "/work/.home/.local", "PIP_USER": "1",
               "PIP_NO_CACHE_DIR": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
               "PATH": "/work/.home/.local/bin:/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
               "MPLBACKEND": "Agg", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        if self.proxy:
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env[k] = self.proxy
            env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1"
        return env

    def mount(self, session: str) -> str:
        if self.host_root:
            return f"type=bind,src={Path(self.host_root) / session},dst=/work"
        sub = f"{self.subpath}/{session}" if self.subpath else session
        return f"type=volume,src={self.volume},dst=/work,volume-subpath={sub}"

    def run_args(self, session: str) -> list[str]:
        args = ["run", "-d", "--name", self.container(session), "--label", f"{LABEL}=1",
                "--label", f"{LABEL}.session={session}", "--init", "--read-only",
                "--tmpfs", "/tmp:rw,nosuid,size=512m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", str(self.pids), "--memory", f"{self.mem_mb}m", "--memory-swap", f"{self.mem_mb}m",
                "--cpus", str(self.cpus),
                "--network", self.net_name(session) if self.per_session_net else self.network,
                "--user", f"{self.uid}:{self.uid}",
                "--workdir", "/work", "--mount", self.mount(session)]
        for k, v in self.env().items():
            args += ["--env", f"{k}={v}"]
        return [*args, self.image, "sleep", "infinity"]

    def _lock(self, session: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(session, threading.Lock())

    def ensure(self, session: str) -> str:
        name = self.container(session)
        with self._lock(session):
            if self.per_session_net:
                self._ensure_net(session)
            r = self._run("inspect", "--format", "{{.State.Running}}", name, timeout=20)
            if r.returncode == 0 and r.stdout.strip() == b"true":
                pass
            elif r.returncode == 0:
                self._check(self._run("start", name, timeout=60), "не запустить контейнер разговора")
            else:
                self._check(self._run(*self.run_args(session), timeout=120), "не создать контейнер разговора")
            self.last[session] = time.monotonic()
        return name

    def exec(self, session: str, work: Path, argv: list[str], timeout: int, background: bool = False
             ) -> tuple[int | None, bytes, bytes]:
        name = self.ensure(session)
        # timeout внутри контейнера убивает саму программу; таймаут runner — страховка, если завис docker.
        cmd = ["exec", "--workdir", "/work", name, "timeout", "-k", "5", str(timeout), *argv]
        t0 = time.monotonic()
        r = self._run(*cmd, timeout=timeout + 30)
        self.last[session] = time.monotonic()
        code, out, err = r.returncode, r.stdout or b"", r.stderr or b""
        if code == 124 or (code == 137 and time.monotonic() - t0 >= timeout):
            if "прервано".encode() not in err:  # ядро Python уже написало, что прервано и что сброшено
                err += f"\nпрервано: дольше {timeout} с".encode()
            return None, out, err
        if code == 137:
            err += f"\nпроцесс убит (137) — скорее всего, не хватило памяти: лимит {self.mem_mb} МБ".encode()
        return code, out, err

    def reset(self, session: str) -> None:
        with self._lock(session):
            self._run("rm", "-f", self.container(session), timeout=60)
            if self.per_session_net:
                net = self.net_name(session)
                self._run("network", "disconnect", "-f", net, self.egress_container, timeout=30)
                self._run("network", "rm", net, timeout=30)
            self.last.pop(session, None)

    def adopt(self) -> None:
        """После перезапуска сервера: живые контейнеры считаем только что использованными, а не сиротами."""
        fmt = f'{{{{.Label "{LABEL}.session"}}}}'
        r = self._run("ps", "-a", "--filter", f"label={LABEL}=1", "--format", fmt)
        found = (r.stdout or b"").decode().split()
        if self.per_session_net:  # сеть без контейнера (сервер упал между шагами) — тоже подберёт уборка
            found += (self._run("network", "ls", "--filter", f"label={LABEL}=1", "--format", fmt).stdout
                      or b"").decode().split()
        now = time.monotonic()
        for s in found:
            if SESSION_RE.match(s):
                self.last.setdefault(s, now)

    def reap(self, now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        gone = [s for s, t in list(self.last.items()) if now - t > self.idle_s]
        for s in gone:
            self.reset(s)
        return gone

    def shutdown(self) -> None:
        """Остановка сервера: контейнеры разговоров — следом, иначе `docker compose down` не удалит их сеть."""
        for s in list(self.last):
            self.reset(s)

    def info(self) -> dict[str, Any]:
        return {"backend": self.name, "image": self.image,
                "network": "своя у разговора" if self.per_session_net else self.network, "proxy": bool(self.proxy),
                "isolated": True, "containers": len(self.last)}


Backend = LocalBackend | DockerBackend


# ── выполнение ───────────────────────────────────────────────────────

def execute(root: Path, session: str, kind: str, code: str, timeout: int, mem_mb: int = 2048, *,
            background: bool = False, backend: Backend | None = None, kernel: bool = True,
            reset: bool = False) -> dict[str, Any]:
    """kernel=True (по умолчанию) — Python выполняется в ядре разговора и помнит переменные;
    False — отдельным процессом с нуля (как раньше)."""
    if not SESSION_RE.match(session):
        raise ValueError("неверный id сессии")
    backend = backend or LocalBackend(mem_mb)
    work = root / session
    backend.prepare(work)
    before = _snapshot(work)
    timeout = max(1, min(int(timeout), 600))
    script: Path | None = None
    job = uuid.uuid4().hex[:8]
    if kind == "python":
        if background:
            (work / JOBS_DIR).mkdir(exist_ok=True)
            script = work / JOBS_DIR / f"{job}.py"  # фоновой задаче скрипт нужен и после ответа
        else:
            script = work / f".run_{job}.py"
        script.write_text(code, encoding="utf-8")
        prog = [backend.python, script.relative_to(work).as_posix()]
        if kernel and not background:
            prog = [backend.python, "-m", "agentkit.kernel", "run", script.relative_to(work).as_posix(), str(timeout),
                    *(["--reset"] if reset else [])]
    elif kind == "bash":
        # -c, а не -lc: login-shell перечитывает /etc/profile и затирает PATH с ~/.local/bin (pip --user)
        prog = ["bash", "-c", code]
    else:
        raise ValueError("kind: python | bash")
    if background:
        (work / JOBS_DIR).mkdir(exist_ok=True)
        log = f"{JOBS_DIR}/{job}.log"
        # nohup и вывод в файл: задача переживает и этот вызов, и закрытие его pipe'ов
        # (setsid не берём — его нет на macOS, где local-режим чаще всего и запускают).
        launcher = f"nohup {shlex.join(prog)} > {log} 2>&1 < /dev/null & echo $!"
        argv, timeout = ["bash", "-c", launcher], min(timeout, 30)
    else:
        argv = prog
    in_kernel = kind == "python" and kernel and not background
    t0 = time.monotonic()
    try:
        # У ядра свой таймаут (клиент убивает зависшую ячейку и говорит, что состояние сброшено);
        # внешний — с запасом на старт ядра, только на случай, если завис сам клиент.
        code_, out, err = backend.exec(session, work, argv, timeout + 25 if in_kernel else timeout, background)
    finally:
        if script and not background:
            script.unlink(missing_ok=True)
    if in_kernel and code_ == 124:
        code_ = None
    after = _snapshot(work)
    changed = [{"path": p, "size": after[p][1], "new": p not in before} for p in sorted(after)
               if before.get(p) != after[p]]
    res: dict[str, Any] = {
        "exit_code": code_, "stdout": out.decode("utf-8", "replace")[-MAX_OUT:],
        "stderr": err.decode("utf-8", "replace")[-MAX_OUT:], "seconds": round(time.monotonic() - t0, 2),
        "files": changed[:MAX_FILES_LISTED], "more_files": max(0, len(changed) - MAX_FILES_LISTED)}
    if background and code_ == 0:
        res["job"] = {"id": job, "pid": res["stdout"].strip(), "log": log}
    return res


# ── HTTP ─────────────────────────────────────────────────────────────

def serve(root: Path, host: str, port: int, token: str, backend: Backend | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    backend = backend or LocalBackend()
    if isinstance(backend, DockerBackend):
        backend.adopt()

        def reaper() -> None:
            while True:
                time.sleep(60)
                try:
                    if gone := backend.reap():
                        print(f"sandbox: удалены простаивающие контейнеры: {', '.join(gone)}", flush=True)
                except Exception as e:  # noqa: BLE001 — уборка не должна ронять сервер
                    print(f"sandbox: уборка: {e}", file=sys.stderr, flush=True)

        threading.Thread(target=reaper, daemon=True).start()

        def stop(*_: Any) -> None:
            backend.shutdown()
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, stop)

    class H(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._send(200, {"ok": True, **backend.info()})

        def do_POST(self) -> None:
            if token and not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                return self._send(401, {"error": "нет доступа"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                session = str(body.get("session", ""))
                if self.path.rstrip("/") == "/reset":
                    if not SESSION_RE.match(session):
                        raise ValueError("неверный id сессии")
                    backend.reset(session)
                    return self._send(200, {"ok": True})
                res = execute(root, session, str(body.get("kind", "")), str(body.get("code", "")),
                              int(body.get("timeout") or 60), background=bool(body.get("background")),
                              backend=backend, reset=bool(body.get("reset")))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except SandboxError as e:
                return self._send(503, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            self._send(200, res)

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("sandbox: " + fmt % args + "\n")

    print(f"sandbox: {backend.name}, {root} на {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), H).serve_forever()


def main(argv: list[str] | None = None) -> None:
    e = os.environ.get
    ap = argparse.ArgumentParser(description="Песочница agentkit")
    ap.add_argument("--root", default=e("SANDBOX_ROOT", "/work"), help="папка с папками разговоров")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(e("SANDBOX_PORT", "8090")))
    ap.add_argument("--token", default=e("SANDBOX_TOKEN", ""))
    ap.add_argument("--backend", choices=["local", "docker"], default=e("SANDBOX_BACKEND", "local"))
    ap.add_argument("--memory", type=int, default=int(e("SANDBOX_MEMORY_MB", "2048")), help="МБ на разговор")
    d = ap.add_argument_group("docker")
    d.add_argument("--image", default=e("SANDBOX_IMAGE", ""), help="образ контейнера разговора")
    d.add_argument("--volume", default=e("SANDBOX_VOLUME", ""), help="том с папками разговоров (на VPS)")
    d.add_argument("--subpath", default=e("SANDBOX_SUBPATH", "workspaces"), help="подпапка тома")
    d.add_argument("--host-root", default=e("SANDBOX_HOST_ROOT", ""), help="или папка на машине с docker")
    d.add_argument("--network", default=e("SANDBOX_NETWORK", "none"), help="сеть контейнеров (none — без сети)")
    d.add_argument("--egress-container", default=e("SANDBOX_EGRESS_CONTAINER", ""),
                   help="контейнер egress: если задан — у каждого разговора своя сеть, egress подключается к ней")
    d.add_argument("--subnet-pool", default=e("SANDBOX_SUBNET_POOL", "10.213.0.0/16"), help="подсети для сетей разговоров")
    d.add_argument("--proxy", default=e("SANDBOX_PROXY", ""), help="http://egress:3128 — выход в интернет")
    d.add_argument("--cpus", type=float, default=float(e("SANDBOX_CPUS", "1")))
    d.add_argument("--pids", type=int, default=int(e("SANDBOX_PIDS", "256")))
    d.add_argument("--idle", type=int, default=int(e("SANDBOX_IDLE_MINUTES", "15")), help="минут простоя до удаления")
    a = ap.parse_args(argv)
    backend: Backend
    if a.backend == "docker":
        backend = DockerBackend(a.image, volume=a.volume, subpath=a.subpath, host_root=a.host_root,
                                network=a.network, proxy=a.proxy, mem_mb=a.memory, cpus=a.cpus, pids=a.pids,
                                idle_s=a.idle * 60, egress_container=a.egress_container, subnet_pool=a.subnet_pool)
    else:
        backend = LocalBackend(a.memory)
    serve(Path(a.root), a.host, a.port, a.token, backend)


if __name__ == "__main__":
    main()
