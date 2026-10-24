#!/usr/bin/env python3
"""Песочница агента: Python и командная строка в отдельном контейнере.

Сервер (этот файл, только стандартная библиотека) работает в своём контейнере
без сети и без секретов сервиса и делит с агентом только папку рабочих
пространств: всё, что код создаст в /work/<сессия>, сразу видно в чате и
скачивается. Так агент может посчитать таблицу, построить график или собрать
документ, не получая доступа ни к базе, ни к ключам, ни к интернету.

    python -m agentkit.sandbox --root data/workspaces --port 8090 --token …   # локально
    (в проде — сервис sandbox в docker-compose шаблона)

Процесс между вызовами не живёт: состояние — это файлы рабочей папки.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_OUT = 40_000
MAX_FILES_LISTED = 50


def _limits(mem_mb: int, cpu_s: int):
    def apply() -> None:  # в дочернем процессе до exec
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 2**20, mem_mb * 2**20))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
            resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 2**20, 512 * 2**20))
            os.setsid()
        except Exception:  # noqa: BLE001 — на macOS часть лимитов недоступна
            pass
    return apply


def _snapshot(root: Path) -> dict[str, tuple[float, int]]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
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


def execute(root: Path, session: str, kind: str, code: str, timeout: int, mem_mb: int = 2048) -> dict[str, Any]:
    if not SESSION_RE.match(session):
        raise ValueError("неверный id сессии")
    work = root / session
    work.mkdir(parents=True, exist_ok=True)
    before = _snapshot(work)
    timeout = max(1, min(int(timeout), 600))
    script = None
    if kind == "python":
        script = work / f".run_{uuid.uuid4().hex[:8]}.py"
        script.write_text(code, encoding="utf-8")
        cmd: list[str] | str = [sys.executable, script.name]
    elif kind == "bash":
        cmd = ["bash", "-lc", code] if os.path.exists("/bin/bash") else code
    else:
        raise ValueError("kind: python | bash")
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(work), "LANG": "C.UTF-8",
           "MPLBACKEND": "Agg", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, cwd=work, env=env, capture_output=True, timeout=timeout,
                           shell=isinstance(cmd, str), preexec_fn=_limits(mem_mb, timeout + 5) if os.name == "posix" else None)
        code_, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        code_, out, err = None, e.stdout or b"", (e.stderr or b"") + f"\nпрервано: дольше {timeout} с".encode()
    finally:
        if script:
            script.unlink(missing_ok=True)
    after = _snapshot(work)
    changed = [{"path": p, "size": after[p][1], "new": p not in before} for p in sorted(after)
               if before.get(p) != after[p]]
    return {"exit_code": code_, "stdout": out.decode("utf-8", "replace")[-MAX_OUT:],
            "stderr": err.decode("utf-8", "replace")[-MAX_OUT:], "seconds": round(time.monotonic() - t0, 2),
            "files": changed[:MAX_FILES_LISTED], "more_files": max(0, len(changed) - MAX_FILES_LISTED)}


def serve(root: Path, host: str, port: int, token: str) -> None:
    root.mkdir(parents=True, exist_ok=True)

    class H(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._send(200, {"ok": True, "python": sys.version.split()[0]})

        def do_POST(self) -> None:
            if token and not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                return self._send(401, {"error": "нет доступа"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                res = execute(root, str(body.get("session", "")), str(body.get("kind", "")), str(body.get("code", "")),
                              int(body.get("timeout") or 60))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            self._send(200, res)

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("sandbox: " + fmt % args + "\n")

    print(f"sandbox: {root} на {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), H).serve_forever()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Песочница agentkit")
    ap.add_argument("--root", default=os.environ.get("SANDBOX_ROOT", "/work"))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("SANDBOX_PORT", "8090")))
    ap.add_argument("--token", default=os.environ.get("SANDBOX_TOKEN", ""))
    a = ap.parse_args(argv)
    serve(Path(a.root), a.host, a.port, a.token)


if __name__ == "__main__":
    main()
