#!/usr/bin/env python3
"""Мост agentkit: даёт агенту доступ к папкам и командной строке этого компьютера.

Запуск (ключ — из «Настройки → Компьютер» в чате агента):

    python3 bridge.py --server https://agent.example.ru --key dev_… --folder ~/Документы/работа
    python3 bridge.py … --folder ~/проекты --folder ~/Загрузки:ro --allow-shell

Мост сам подключается к серверу (длинный опрос) — порты открывать не нужно.
Агенту доступны только перечисленные папки (:ro — только чтение); команды —
только с --allow-shell. Каждое действие печатается здесь же. Ctrl+C — остановить.

Только стандартная библиотека Python 3.9+: файл можно скачать и запустить как есть.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import platform
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "1.0"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".cache", "Library", "AppData", "$RECYCLE.BIN"}
MAX_READ = 5_000_000
MAX_OUT = 60_000


class BridgeError(Exception):
    pass


class Folder:
    def __init__(self, name: str, path: Path, read_only: bool):
        self.name, self.path, self.read_only = name, path, read_only


class Bridge:
    def __init__(self, server: str, key: str, folders: list[Folder], allow_shell: bool):
        self.server = server.rstrip("/")
        self.key = key
        self.folders = {f.name: f for f in folders}
        self.allow_shell = allow_shell
        self.ctx = ssl.create_default_context()

    # ── сеть ─────────────────────────────────────────────────────────
    def post(self, path: str, body: dict, timeout: float) -> dict:
        req = urllib.request.Request(self.server + path, data=json.dumps(body, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}",
                                              "User-Agent": f"agentkit-bridge/{VERSION}"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as r:
            raw = r.read()
        return json.loads(raw or b"{}")

    def info(self) -> dict:
        return {"hostname": socket.gethostname(), "platform": f"{platform.system()} {platform.release()}",
                "version": VERSION, "shell": self.allow_shell,
                "folders": [{"name": f.name, "path": str(f.path), "read_only": f.read_only} for f in self.folders.values()]}

    def loop(self) -> None:
        delay = 1.0
        announced = False
        while True:
            try:
                resp = self.post("/api/bridge/poll", {"info": self.info(), "hello": not announced}, timeout=45)
                if not announced:
                    log("подключено — агент видит этот компьютер")
                    announced = True
                delay = 1.0
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    log("ключ не подошёл: компьютер удалён в настройках или ключ скопирован не полностью")
                    sys.exit(2)
                log(f"сервер ответил {e.code}, повтор через {delay:.0f} с")
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            except (urllib.error.URLError, OSError, ValueError) as e:
                reason = getattr(e, "reason", e)
                if "CERTIFICATE_VERIFY_FAILED" in str(reason):
                    log("не проверяется сертификат сервера. На macOS: запустите «Install Certificates.command» из папки Python")
                else:
                    log(f"нет связи с сервером ({reason}), повтор через {delay:.0f} с")
                announced = False
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            cmd = resp.get("cmd")
            if cmd:
                threading.Thread(target=self.handle, args=(cmd,), daemon=True).start()

    def handle(self, cmd: dict) -> None:
        op, args = cmd.get("op"), cmd.get("args") or {}
        log(f"{op} {args.get('path') or args.get('command') or args.get('query') or ''}".strip())
        try:
            fn = getattr(self, "op_" + str(op), None)
            if fn is None:
                raise BridgeError(f"мост не знает команду {op} — обновите bridge.py")
            out = {"call_id": cmd["id"], "ok": True, "result": fn(**args)}
        except BridgeError as e:
            out = {"call_id": cmd["id"], "ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 — любая ошибка уходит агенту текстом
            out = {"call_id": cmd["id"], "ok": False, "error": f"{type(e).__name__}: {e}"}
        if not out["ok"]:
            log(f"  ✕ {out['error']}")
        for _ in range(3):
            try:
                self.post("/api/bridge/result", out, timeout=30)
                return
            except Exception:  # noqa: BLE001
                time.sleep(1)

    # ── пути ─────────────────────────────────────────────────────────
    def resolve(self, path: str, *, write: bool = False) -> tuple[Folder, Path]:
        p = (path or "").replace("\\", "/").strip().strip("/")
        if not p or p == ".":
            raise BridgeError("укажите папку: " + ", ".join(self.folders))
        root, _, rest = p.partition("/")
        f = self.folders.get(root)
        if f is None:
            raise BridgeError(f"папка «{root}» не открыта; открыты: {', '.join(self.folders)}")
        target = (f.path / rest).resolve()
        if target != f.path and f.path not in target.parents:
            raise BridgeError("путь выходит за пределы открытой папки")
        if write and f.read_only:
            raise BridgeError(f"папка «{f.name}» открыта только для чтения")
        return f, target

    def rel(self, f: Folder, p: Path) -> str:
        r = p.relative_to(f.path).as_posix()
        return f.name if r == "." else f"{f.name}/{r}"

    # ── команды ──────────────────────────────────────────────────────
    def op_list(self, path: str = "") -> dict:
        if not path.strip(" ./"):
            return {"path": "", "items": [{"name": n, "dir": True} for n in self.folders]}
        f, d = self.resolve(path)
        if not d.is_dir():
            raise BridgeError("это не папка")
        items = []
        for e in sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if e.name.startswith("."):
                continue
            try:
                items.append({"name": e.name, "dir": e.is_dir(), "size": 0 if e.is_dir() else e.stat().st_size})
            except OSError:
                continue
        return {"path": self.rel(f, d), "items": items[:500], "more": max(0, len(items) - 500)}

    def op_read(self, path: str, offset: int = 0, limit: int = 400) -> dict:
        f, p = self.resolve(path)
        if not p.is_file():
            raise BridgeError("файла нет")
        text = read_text(p)
        lines = text.splitlines()
        part = lines[offset:offset + max(1, min(limit, 2000))]
        body = "\n".join(f"{i:>6}  {line}" for i, line in enumerate(part, offset + 1))
        return {"path": self.rel(f, p), "text": body[:MAX_OUT], "lines": len(part), "total": len(lines),
                "more": max(0, len(lines) - offset - len(part))}

    def op_search(self, query: str, path: str = "", glob: str = "", names_only: bool = False) -> dict:
        roots = [self.resolve(path)] if path.strip(" ./") else [(f, f.path) for f in self.folders.values()]
        q = query.lower()
        hits, more, seen, started = [], False, 0, time.time()
        for f, root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for name in filenames:
                    seen += 1
                    if len(hits) >= 200 or seen > 50_000 or time.time() - started > 90:
                        return {"hits": hits, "more": True}
                    if glob and not fnmatch.fnmatch(name.lower(), glob.lower()):
                        continue
                    fp = Path(dirpath) / name
                    if names_only:
                        if q in name.lower() or fnmatch.fnmatch(name.lower(), q):
                            hits.append({"path": self.rel(f, fp)})
                        continue
                    try:
                        if fp.stat().st_size > 2_000_000:
                            continue
                        raw = fp.read_bytes()
                    except OSError:
                        continue
                    if b"\0" in raw[:4096]:
                        continue
                    for n, line in enumerate(decode(raw).splitlines(), 1):
                        if q in line.lower():
                            hits.append({"path": self.rel(f, fp), "line": n, "text": line.strip()[:200]})
                            if len(hits) >= 200:
                                more = True
                                break
        return {"hits": hits, "more": more}

    def op_write(self, path: str, content: str) -> dict:
        f, p = self.resolve(path, write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"path": self.rel(f, p), "size": p.stat().st_size}

    def op_edit(self, path: str, old: str, new: str) -> dict:
        f, p = self.resolve(path, write=True)
        if not p.is_file():
            raise BridgeError("файла нет")
        text = read_text(p)
        n = text.count(old)
        if n != 1:
            raise BridgeError("фрагмент не найден" if n == 0 else f"фрагмент встречается {n} раз — уточните его")
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        return {"path": self.rel(f, p)}

    def op_run(self, command: str, cwd: str = "", timeout: int = 60) -> dict:
        if not self.allow_shell:
            raise BridgeError("команды на этом компьютере запрещены (мост запущен без --allow-shell)")
        if cwd.strip(" ./"):
            f, d = self.resolve(cwd)
        else:
            f = next(iter(self.folders.values()))
            d = f.path
        try:
            r = subprocess.run(command, shell=True, cwd=str(d), capture_output=True, timeout=max(1, min(timeout, 300)))
        except subprocess.TimeoutExpired as e:
            return {"code": None, "stdout": decode(e.stdout or b"")[-MAX_OUT:], "stderr": f"прервано: дольше {timeout} с"}
        return {"code": r.returncode, "stdout": decode(r.stdout)[-MAX_OUT:], "stderr": decode(r.stderr)[-MAX_OUT:]}


def decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_text(p: Path) -> str:
    ext = p.suffix.lower()
    if p.stat().st_size > MAX_READ * (4 if ext in (".pdf", ".docx", ".xlsx") else 1):
        raise BridgeError("файл слишком большой")
    raw = p.read_bytes()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            return "\n\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(raw)).pages)
        if ext == ".docx":
            import docx
            return "\n".join(par.text for par in docx.Document(io.BytesIO(raw)).paragraphs)
        if ext == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            return "\n".join("\t".join("" if v is None else str(v) for v in row)
                             for ws in wb.worksheets for row in ws.iter_rows(values_only=True))
    except ImportError as e:
        raise BridgeError(f"чтобы читать {ext}, установите на этом компьютере: pip install {e.name}") from None
    if b"\0" in raw[:4096]:
        raise BridgeError("это бинарный файл, а не текст")
    return decode(raw)


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def parse_folders(items: list[str]) -> list[Folder]:
    out: dict[str, Folder] = {}
    for item in items or ["."]:
        ro = item.endswith(":ro")
        raw = item[:-3] if ro else item
        p = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
        if not p.is_dir():
            sys.exit(f"нет такой папки: {p}")
        name = p.name or "root"
        base, n = name, 2
        while name in out:
            name, n = f"{base}-{n}", n + 1
        out[name] = Folder(name, p, ro)
    return list(out.values())


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Мост agentkit: папки и команды этого компьютера для агента")
    ap.add_argument("--server", required=True, help="адрес сервиса агента, https://…")
    ap.add_argument("--key", default=os.environ.get("AGENTKIT_BRIDGE_KEY", ""), help="ключ из «Настройки → Компьютер»")
    ap.add_argument("--folder", action="append", default=[], help="открыть папку агенту (можно несколько; :ro — только чтение)")
    ap.add_argument("--allow-shell", action="store_true", help="разрешить агенту выполнять команды (каждая — с вашим подтверждением в чате)")
    a = ap.parse_args(argv)
    if not a.key:
        sys.exit("нужен ключ: --key или переменная AGENTKIT_BRIDGE_KEY")
    folders = parse_folders(a.folder)
    log(f"мост agentkit {VERSION} → {a.server}")
    for f in folders:
        log(f"  папка {f.name}: {f.path}{' (только чтение)' if f.read_only else ''}")
    log(f"  команды: {'разрешены' if a.allow_shell else 'запрещены'}")
    log("Ctrl+C — остановить")
    try:
        Bridge(a.server, a.key, folders, a.allow_shell).loop()
    except KeyboardInterrupt:
        log("остановлено")


if __name__ == "__main__":
    main()
