"""Ядро Python разговора: переменные, импорты и загруженные данные живут между вызовами run_python.

Без ядра каждый вызов начинал с нуля: модель заново читала CSV на 200 МБ,
заново импортировала pandas, а «посчитай ещё по месяцам» превращалось в
повтор всей программы. Ядро — обычный процесс Python рядом с кодом разговора
(в его контейнере или, в режиме local, на этой машине), слушает unix-сокет;
run_python приходит клиентом:

    python -m agentkit.kernel run script.py 120 [--reset]

Клиент сам поднимает ядро, если его нет, и убивает его по таймауту — зависшая
ячейка не должна держать разговор: состояние сбрасывается, и об этом сказано
в выводе. Только стандартная библиотека: модуль работает в образе песочницы.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import hashlib
import importlib
import json
import linecache
import os
import signal
import site
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

CELL = "<ячейка>"
TIMEOUT_EXIT = 124
MAX_MSG = 64 * 2**20


def default_sock(work: Path | None = None) -> Path:
    # В контейнере /tmp свой у разговора; в режиме local — общий, поэтому имя от папки разговора.
    # Не в самой папке: у unix-сокета предел длины пути (104 байта на macOS), а папки бывают глубоко.
    if env := os.environ.get("AK_KERNEL_SOCK"):
        return Path(env)
    key = hashlib.sha1(str((work or Path.cwd()).resolve()).encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"ak-kernel-{key}.sock"


def _recv_all(conn: socket.socket) -> bytes:
    chunks, size = [], 0
    while data := conn.recv(65536):
        chunks.append(data)
        size += len(data)
        if size > MAX_MSG:
            raise ValueError("слишком большое сообщение")
    return b"".join(chunks)


# ── ядро ─────────────────────────────────────────────────────────────

class Kernel:
    def __init__(self) -> None:
        self.ns: dict[str, Any] = {"__name__": "__main__", "__builtins__": builtins}

    def run(self, code: str) -> dict[str, Any]:
        """Выполнить ячейку. Вывод ловится на уровне дескрипторов 1/2 — так видно и print, и
        вывод C-расширений, и os.system; последнее выражение печатается, как в Jupyter."""
        # pip install --user в этом же разговоре: папки user-site при старте ядра ещё не было, и без
        # этого «import six» после установки падал бы до перезапуска ядра (нашли на живом docker).
        user_site = site.getusersitepackages()
        if os.path.isdir(user_site) and user_site not in sys.path:
            site.addsitedir(user_site)
        importlib.invalidate_caches()
        # Текст ячейки — в linecache: в traceback видна строка с ошибкой, а не только её номер.
        linecache.cache[CELL] = (len(code), None, code.splitlines(True), CELL)
        out_f, err_f = tempfile.TemporaryFile(), tempfile.TemporaryFile()
        saved = os.dup(1), os.dup(2)
        exit_code = 0
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(out_f.fileno(), 1)
            os.dup2(err_f.fileno(), 2)
            try:
                tree = ast.parse(code, CELL, "exec")
                last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
                exec(compile(tree, CELL, "exec"), self.ns)  # noqa: S102 — в этом и смысл ядра
                if last is not None:
                    val = eval(compile(ast.Expression(last.value), CELL, "eval"), self.ns)  # noqa: S307
                    if val is not None:
                        print(repr(val))
            except SystemExit as e:
                exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
                if e.code is not None and not isinstance(e.code, int):
                    print(e.code, file=sys.stderr)
            except BaseException:  # noqa: BLE001 — любая ошибка ячейки — текст для модели, ядро живёт
                exit_code = 1
                etype, value, tb = sys.exc_info()
                # Кадры самого ядра модели не нужны — только её код.
                while tb is not None and tb.tb_frame.f_code.co_filename != CELL:
                    tb = tb.tb_next
                traceback.print_exception(etype, value, tb)
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
        finally:
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])
        res = {}
        for name, f in (("stdout", out_f), ("stderr", err_f)):
            f.seek(0)
            res[name] = f.read().decode("utf-8", "replace")
            f.close()
        res["exit_code"] = exit_code
        return res


def serve(sock: Path) -> None:
    try:
        # local-режим ставит процессу мягкий лимит CPU на время одного вызова; ядро живёт дольше.
        import resource

        _, hard = resource.getrlimit(resource.RLIMIT_CPU)
        resource.setrlimit(resource.RLIMIT_CPU, (hard, hard))
    except Exception:  # noqa: BLE001
        pass
    sock.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock))
    os.chmod(sock, 0o600)
    srv.listen(1)
    # Без вызовов полчаса — выходим: в режиме local ядра иначе копились бы по одному на каждый
    # разговор до перезагрузки машины. Переменные теряются, следующий вызов скажет об этом.
    srv.settimeout(float(os.environ.get("AK_KERNEL_IDLE", "1800")))
    Path(f"{sock}.pid").write_text(str(os.getpid()))
    k = Kernel()
    while True:
        try:
            conn, _ = srv.accept()
        except TimeoutError:
            sock.unlink(missing_ok=True)
            Path(f"{sock}.pid").unlink(missing_ok=True)
            return
        conn.settimeout(None)
        with conn:
            try:
                req = json.loads(_recv_all(conn) or b"{}")
                res = k.run(str(req.get("code", "")))
            except Exception as e:  # noqa: BLE001
                res = {"stdout": "", "stderr": f"ядро: {e}", "exit_code": 1}
            with contextlib.suppress(OSError):
                conn.sendall(json.dumps(res, ensure_ascii=False).encode())


# ── клиент (то, что запускает run_python) ────────────────────────────

def _kill(sock: Path) -> bool:
    pid_file = Path(f"{sock}.pid")
    killed = False
    try:
        os.kill(int(pid_file.read_text()), signal.SIGKILL)
        killed = True
    except (OSError, ValueError):
        pass
    pid_file.unlink(missing_ok=True)
    sock.unlink(missing_ok=True)
    return killed


def _connect(sock: Path) -> socket.socket | None:
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        c.connect(str(sock))
        return c
    except OSError:
        c.close()
        return None


def _start(sock: Path) -> socket.socket:
    log_dir = Path(".jobs")
    log_dir.mkdir(exist_ok=True)
    with open(log_dir / "kernel.log", "ab") as log:
        # Своя сессия: ядро переживает и этот вызов, и timeout/killpg вокруг него.
        subprocess.Popen([sys.executable, "-m", "agentkit.kernel", "serve", str(sock)], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if (c := _connect(sock)) is not None:
            return c
        time.sleep(0.05)
    raise RuntimeError("ядро Python не запустилось за 20 с — смотрите .jobs/kernel.log")


def client(script: Path, timeout: float, reset: bool = False) -> int:
    sock = default_sock()
    code = script.read_text(encoding="utf-8")
    notes = []
    if reset:
        _kill(sock)
        notes.append("Ядро Python перезапущено по просьбе — переменных нет.")
    c = _connect(sock)
    if c is None:
        # pid-файл без живого сокета — ядро было и умерло (память, kill): модель должна знать,
        # что её df из прошлого вызова больше нет, иначе получит NameError и не поймёт почему.
        if Path(f"{sock}.pid").exists() and not reset:
            notes.append("Ядро Python перезапущено (прежнее завершилось) — переменные прошлых вызовов потеряны.")
        _kill(sock)
        c = _start(sock)
    with c:
        c.settimeout(timeout)
        try:
            c.sendall(json.dumps({"code": code}, ensure_ascii=False).encode())
            c.shutdown(socket.SHUT_WR)
            res = json.loads(_recv_all(c) or b"{}")
        except TimeoutError:
            _kill(sock)
            sys.stderr.write(f"прервано: дольше {timeout:g} с; ядро Python остановлено, переменные сброшены\n")
            return TIMEOUT_EXIT
        except (OSError, ValueError) as e:
            _kill(sock)
            sys.stderr.write(f"ядро Python упало ({e}) — скорее всего, не хватило памяти; переменные сброшены\n")
            return 137
    for n in notes:
        sys.stderr.write(n + "\n")
    sys.stdout.write(res.get("stdout", ""))
    sys.stderr.write(res.get("stderr", ""))
    return int(res.get("exit_code") or 0)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["serve"]:
        serve(Path(argv[1]))
    elif argv[:1] == ["run"]:
        sys.exit(client(Path(argv[1]), float(argv[2]), "--reset" in argv[3:]))
    else:
        sys.exit("python -m agentkit.kernel run <файл> <таймаут> [--reset] | serve <сокет>")


if __name__ == "__main__":
    main()
