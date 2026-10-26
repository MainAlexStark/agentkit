#!/usr/bin/env python3
"""Мост agentkit: даёт агенту доступ к папкам и командной строке этого компьютера.

Обычно его ставит файл «Подключить компьютер» из чата (Windows) или одна команда в
Терминале (macOS, Linux) — они скачивают этот файл и запускают его с --setup:
мост запоминает сервер и ключ, предлагает выбрать папку в обычном окне выбора и
спрашивает, запускаться ли самому при входе в систему.

Папки добавляются только окном выбора на этом компьютере (из чата можно попросить
его открыть, но выбирает человек здесь). Сделать папку изменяемой и разрешить
команды тоже можно лишь подтверждением здесь. Сузить доступ можно и из чата.

Вручную, без установки:

    python3 bridge.py --server https://agent.example.ru --key dev_… --folder ~/работа --folder ~/Загрузки:ro

Мост сам ходит на сервер (длинный опрос) — порты открывать не нужно.
Только стандартная библиотека Python 3.9+.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "1.2"
HOME_DIR = Path.home() / ".agentkit"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".cache", "Library", "AppData", "$RECYCLE.BIN"}
MAX_READ = 5_000_000
MAX_OUT = 60_000


class BridgeError(Exception):
    pass


class Folder:
    def __init__(self, name: str, path: Path, read_only: bool):
        self.name, self.path, self.read_only = name, path, read_only


class Bridge:
    def __init__(self, server: str, key: str, folders: list[Folder], allow_shell: bool,
                 config: Path | None = None):
        self.server = server.rstrip("/")
        self.key = key
        self.folders = {f.name: f for f in folders}
        self.allow_shell = allow_shell
        self.config = config
        self.ctx = ssl.create_default_context()
        self._ssl_fixed = False
        self._lock = threading.Lock()

    def save(self) -> None:
        if self.config is None:
            return
        data = {"server": self.server, "key": self.key, "shell": self.allow_shell,
                "folders": [{"path": str(f.path), "read_only": f.read_only} for f in self.folders.values()]}
        self.config.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        if os.name == "posix":
            os.chmod(tmp, 0o600)  # в файле ключ
        os.replace(tmp, self.config)

    def add_folder(self, path: Path, read_only: bool) -> Folder:
        path = path.resolve()
        with self._lock:
            for f in self.folders.values():
                if f.path == path:
                    f.read_only = read_only and f.read_only
                    self.save()
                    return f
            base = path.name or "root"
            name, n = base, 2
            while name in self.folders:
                name, n = f"{base}-{n}", n + 1
            f = self.folders[name] = Folder(name, path, read_only)
            self.save()
        return f

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

    def hello(self) -> str | None:
        """Одна проверка связи при настройке: None — сервер принял ключ, иначе — что не так, по-человечески.

        Без неё ошибка (не тот адрес, сертификат, старый ключ) всплывала только в фоновом мосте после
        «готово», в логе, который никто не открывает, — а в чате вечно висело «Ждём подключения…».
        """
        for _ in range(2):
            try:
                self.post("/api/bridge/poll", {"info": self.info(), "hello": True}, timeout=20)
                return None
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    return "ключ не подошёл — скопируйте команду из чата заново (Настройки → Компьютер → Подключить)"
                return f"сервер {self.server} ответил {e.code}"
            except (urllib.error.URLError, OSError, ValueError) as e:
                reason = getattr(e, "reason", e)
                if "CERTIFICATE_VERIFY_FAILED" in str(reason) and not self._ssl_fixed:
                    self._ssl_fixed = True
                    if fix_ssl(self.ctx):
                        continue
                    return "не проверяется сертификат сервера. На macOS: запустите «Install Certificates.command» из папки Python"
                return f"нет связи с {self.server}: {reason}"
        return f"нет связи с {self.server}"

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
                    log("ключ не подошёл: компьютер удалён в настройках чата — мост отключается")
                    if self.config is not None:
                        # Компьютер удалили в чате — убираем и автозапуск, чтобы мост не стучался при каждом входе.
                        remove_autostart(self.config)
                        self.config.unlink(missing_ok=True)
                    sys.exit(2)
                log(f"сервер ответил {e.code}, повтор через {delay:.0f} с")
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            except (urllib.error.URLError, OSError, ValueError) as e:
                reason = getattr(e, "reason", e)
                if "CERTIFICATE_VERIFY_FAILED" in str(reason) and not self._ssl_fixed:
                    self._ssl_fixed = True
                    if fix_ssl(self.ctx):
                        continue  # подхватили системные сертификаты — сразу повторяем
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

    # ── доступ: расширить — только человек здесь, сузить — можно и из чата ──
    def op_pick_folder(self, reason: str = "", read_only: bool | None = None) -> dict:
        prompt = "Выберите папку, которую увидит агент" + (f"\n\nЗачем: {reason}" if reason else "")
        path = pick_folder(prompt)
        if not path:
            raise BridgeError("человек закрыл окно выбора — доступ не дан")
        ro = read_only if read_only is not None else not confirm(
            f"Разрешить агенту изменять файлы в папке «{Path(path).name}»?\n\n"
            "«Нет» — только чтение. Каждое изменение агент всё равно покажет в чате на подтверждение.")
        f = self.add_folder(Path(path), ro)
        log(f"открыта папка {f.name}: {f.path}{' (только чтение)' if f.read_only else ''}")
        return {"name": f.name, "path": str(f.path), "read_only": f.read_only}

    def op_remove_folder(self, name: str) -> dict:
        with self._lock:
            if self.folders.pop(name, None) is None:
                raise BridgeError(f"папка «{name}» и так не открыта")
            self.save()
        log(f"закрыта папка {name}")
        return {"ok": True}

    def op_set_folder(self, name: str, read_only: bool) -> dict:
        f = self.folders.get(name)
        if f is None:
            raise BridgeError(f"папка «{name}» не открыта")
        if f.read_only and not read_only and not confirm(f"Разрешить агенту изменять файлы в папке «{f.name}»?\n{f.path}"):
            raise BridgeError("на компьютере ответили «нет»")
        f.read_only = read_only
        self.save()
        return {"name": f.name, "read_only": f.read_only}

    def op_set_shell(self, enabled: bool) -> dict:
        if enabled and not self.allow_shell and not confirm(
                "Разрешить агенту выполнять команды на этом компьютере?\n\n"
                "Каждую команду он сначала покажет в чате на подтверждение."):
            raise BridgeError("на компьютере ответили «нет»")
        self.allow_shell = bool(enabled)
        self.save()
        return {"shell": self.allow_shell}

    def op_run(self, command: str, cwd: str = "", timeout: int = 60) -> dict:
        if not self.allow_shell:
            raise BridgeError("команды на этом компьютере не разрешены — включите их в чате: Настройки → Компьютер")
        if cwd.strip(" ./"):
            f, d = self.resolve(cwd)
        elif self.folders:
            d = next(iter(self.folders.values())).path
        else:
            d = Path.home()
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


_LOG_FILE: Path | None = None


def log(text: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {text}"
    if _LOG_FILE is not None:
        with open(_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d ") + line + "\n")
    else:
        try:
            print(line, flush=True)
        except (UnicodeEncodeError, OSError):
            print(line.encode("ascii", "replace").decode(), flush=True)


# ── сертификаты: python.org на macOS и старые Windows не видят системные ──
def fix_ssl(ctx: ssl.SSLContext) -> bool:
    try:
        import certifi  # noqa: PLC0415

        ctx.load_verify_locations(certifi.where())
        return True
    except Exception:  # noqa: BLE001
        pass
    if sys.platform == "darwin":
        try:
            pem = subprocess.run(["security", "find-certificate", "-a", "-p",
                                  "/System/Library/Keychains/SystemRootCertificates.keychain"],
                                 capture_output=True, timeout=20).stdout.decode()
            ctx.load_verify_locations(cadata=pem)
            return True
        except Exception:  # noqa: BLE001
            return False
    return False


# ── окна на этом компьютере: выбор папки и вопрос «да/нет» ─────────────
# Без tkinter: он есть не в каждом Python. Обычные системные окна — PowerShell,
# osascript, zenity/kdialog; без графики — вопрос в консоли.
def _ps(script: str, env: dict) -> str:
    full = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;Add-Type -AssemblyName System.Windows.Forms;" + script
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", full], capture_output=True,
                       env={**os.environ, **env}, timeout=3600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout.decode("utf-8", "replace").strip()


def _osa(script: str) -> str | None:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=3600)
    return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else None


def _q(s: str) -> str:  # строка для AppleScript
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pick_folder(prompt: str) -> str | None:
    try:
        if os.name == "nt":
            out = _ps("$f=New-Object System.Windows.Forms.FolderBrowserDialog;$f.Description=$env:AK_PROMPT;"
                      "$f.ShowNewFolderButton=$true;$w=New-Object System.Windows.Forms.Form -Property @{TopMost=$true};"
                      "if($f.ShowDialog($w) -eq 'OK'){$f.SelectedPath}", {"AK_PROMPT": prompt})
            return out or None
        if sys.platform == "darwin":
            out = _osa(f"POSIX path of (choose folder with prompt {_q(prompt)} default location (path to home folder))")
            return (out.rstrip("/") or "/") if out else None
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            for cmd in (["zenity", "--file-selection", "--directory", f"--title={prompt}"],
                        ["kdialog", "--getexistingdirectory", str(Path.home()), "--title", prompt]):
                if shutil.which(cmd[0]):
                    r = subprocess.run(cmd, capture_output=True, timeout=3600)
                    return (r.stdout.decode().strip() or None) if r.returncode == 0 else None
    except Exception as e:  # noqa: BLE001
        log(f"окно выбора папки не открылось ({e}) — спрошу здесь")
    if sys.stdin and sys.stdin.isatty():
        try:
            v = input(f"{prompt}\nПуть к папке (пусто — отмена): ").strip()
        except EOFError:
            return None
        p = Path(os.path.expanduser(v)) if v else None
        return str(p) if p and p.is_dir() else None
    return None


def confirm(text: str) -> bool:
    try:
        if os.name == "nt":
            out = _ps("$w=New-Object System.Windows.Forms.Form -Property @{TopMost=$true};"
                      "[System.Windows.Forms.MessageBox]::Show($w,$env:AK_TEXT,'Агент','YesNo','Question')",
                      {"AK_TEXT": text})
            return out == "Yes"
        if sys.platform == "darwin":
            out = _osa(f"button returned of (display dialog {_q(text)} with title \"Агент\" "
                       "buttons {\"Нет\", \"Да\"} default button \"Да\" with icon caution)")
            return out == "Да"
        if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which("zenity"):
            return subprocess.run(["zenity", "--question", "--title=Агент", f"--text={text}"], timeout=3600).returncode == 0
    except Exception as e:  # noqa: BLE001
        log(f"окно с вопросом не открылось ({e}) — спрошу здесь")
    if sys.stdin and sys.stdin.isatty():
        try:
            return input(f"{text}\n[д/н]: ").strip().lower() in ("д", "да", "y", "yes")
        except EOFError:
            return False
    return False  # спросить некого — значит «нет»


# ── автозапуск при входе в систему ──────────────────────────────────
def _autostart_path(config: Path) -> Path:
    tag = config.stem
    if os.name == "nt":
        return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup" / f"agentkit-{tag}.vbs"
    if sys.platform == "darwin":
        return Path.home() / "Library/LaunchAgents" / f"ru.agentkit.{tag}.plist"
    return Path.home() / ".config/autostart" / f"agentkit-{tag}.desktop"


def install_autostart(config: Path, script: Path) -> Path:
    target = _autostart_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    py = Path(sys.executable)
    if os.name == "nt":
        pyw = py.with_name("pythonw.exe")
        pyw = pyw if pyw.exists() else py
        # .vbs — чтобы при входе не висело чёрное окно консоли.
        target.write_text(f'CreateObject("WScript.Shell").Run """{pyw}"" ""{script}"" --config ""{config}"" --background", 0, False\r\n',
                          encoding="utf-16")
        subprocess.Popen(["wscript", str(target)])
    elif sys.platform == "darwin":
        label = target.stem
        args = "".join(f"<string>{a}</string>" for a in (str(py), str(script), "--config", str(config), "--background"))
        target.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{label}</string>
<key>ProgramArguments</key><array>{args}</array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
<key>StandardOutPath</key><string>{HOME_DIR / "bridge.log"}</string>
<key>StandardErrorPath</key><string>{HOME_DIR / "bridge.log"}</string>
</dict></plist>
''', encoding="utf-8")
        # Падение фонового моста до первой строки лога (не тот Python, нет прав) раньше уходило в никуда —
        # теперь трассировка попадает в bridge.log, а ошибку launchctl показываем здесь же.
        subprocess.run(["launchctl", "unload", str(target)], capture_output=True)
        r = subprocess.run(["launchctl", "load", "-w", str(target)], capture_output=True)
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        if r.returncode != 0 or err:
            r2 = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], capture_output=True)
            if r2.returncode != 0:
                log(f"launchctl не запустил мост: {err or (r2.stderr or b'').decode('utf-8', 'replace').strip()}")
    else:
        target.write_text(f"[Desktop Entry]\nType=Application\nName=Агент: мост\n"
                          f"Exec={py} {script} --config {config} --background\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")
        subprocess.Popen([str(py), str(script), "--config", str(config), "--background"], start_new_session=True)
    return target


def wait_background(log_file: Path, start: int, timeout: float = 20) -> bool:
    """Ждёт, пока фоновый мост напишет «подключено». Иначе «готово» в Терминале — неправда:
    человек закрывает окно, а компьютер в чате так и не появляется."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        time.sleep(1)
        try:
            with open(log_file, "rb") as fh:
                fh.seek(start)
                if "подключено".encode() in fh.read():
                    return True
        except OSError:
            pass
    return False


def log_tail(log_file: Path, start: int, lines: int = 15) -> str:
    try:
        with open(log_file, "rb") as fh:
            fh.seek(start)
            return "\n".join(fh.read().decode("utf-8", "replace").strip().splitlines()[-lines:])
    except OSError:
        return ""


def remove_autostart(config: Path) -> None:
    target = _autostart_path(config)
    if sys.platform == "darwin" and target.exists():
        subprocess.run(["launchctl", "unload", "-w", str(target)], capture_output=True)
    target.unlink(missing_ok=True)


# ── один мост на ключ: второй экземпляр не нужен ───────────────────────
def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes  # noqa: PLC0415

        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def take_lock(config: Path) -> bool:
    lock = config.with_suffix(".pid")
    try:
        pid = int(lock.read_text().strip())
        if pid != os.getpid() and _pid_alive(pid):
            return False
    except (OSError, ValueError):
        pass
    lock.write_text(str(os.getpid()))
    return True


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def find_config() -> Path | None:
    items = sorted(HOME_DIR.glob("bridge-*.json"), key=lambda p: -p.stat().st_mtime) if HOME_DIR.is_dir() else []
    return items[0] if items else None


def parse_folders(items: list[str]) -> list[Folder]:
    out: dict[str, Folder] = {}
    for item in items:
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
    global _LOG_FILE
    ap = argparse.ArgumentParser(description="Мост agentkit: папки и команды этого компьютера для агента")
    ap.add_argument("--server", help="адрес сервиса агента, https://…")
    ap.add_argument("--key", default=os.environ.get("AGENTKIT_BRIDGE_KEY", ""), help="ключ из «Настройки → Компьютер»")
    ap.add_argument("--folder", action="append", default=[], help="открыть папку агенту (можно несколько; :ro — только чтение)")
    ap.add_argument("--allow-shell", action="store_true", help="разрешить агенту выполнять команды (каждая — с подтверждением в чате)")
    ap.add_argument("--setup", action="store_true", help="первый запуск: запомнить ключ, выбрать папку, автозапуск")
    ap.add_argument("--config", type=Path, help="файл настроек моста (по умолчанию ~/.agentkit/bridge-<id>.json)")
    ap.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--uninstall", action="store_true", help="убрать автозапуск и забыть ключ")
    a = ap.parse_args(argv)

    cfg_path = a.config
    if a.key:
        cfg_path = cfg_path or HOME_DIR / f"bridge-{a.key.split('.')[0]}.json"
    elif cfg_path is None:
        cfg_path = find_config()
    if a.uninstall:
        if cfg_path:
            remove_autostart(cfg_path)
            cfg_path.unlink(missing_ok=True)
        log("мост удалён с этого компьютера")
        return
    saved = load_config(cfg_path) if cfg_path and cfg_path.is_file() else {}
    server = a.server or saved.get("server")
    key = a.key or saved.get("key")
    if not server or not key:
        sys.exit("нужны --server и --key (или запустите файл «Подключить компьютер» из чата)")
    if a.background:
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        _LOG_FILE = HOME_DIR / "bridge.log"
    folders = parse_folders(a.folder) if a.folder else [
        Folder(f.name, f.path, f.read_only) for f in parse_folders([x["path"] + (":ro" if x.get("read_only") else "")
                                                                   for x in saved.get("folders", []) if Path(x["path"]).is_dir()])]
    bridge = Bridge(server, key, folders, a.allow_shell or bool(saved.get("shell")), config=cfg_path)
    bridge.save()
    if not take_lock(cfg_path):
        log("мост уже работает на этом компьютере — второй не нужен")
        return

    if a.setup:
        log(f"мост agentkit {VERSION} → {server}")
        problem = bridge.hello()
        if problem:
            cfg_path.with_suffix(".pid").unlink(missing_ok=True)
            log(f"✕ {problem}")
            sys.exit(1)
        log("связь с сервером есть — в чате компьютер уже виден")
        if not bridge.folders:
            log("откройте агенту папку — появится обычное окно выбора")
            while True:
                path = pick_folder("Выберите папку, которую увидит агент (документы, проекты…)")
                if not path:
                    break
                ro = not confirm(f"Разрешить агенту изменять файлы в папке «{Path(path).name}»?\n\n"
                                 "«Нет» — только чтение. Изменения агент всё равно покажет в чате на подтверждение.")
                f = bridge.add_folder(Path(path), ro)
                log(f"  папка {f.name}: {f.path}{' (только чтение)' if f.read_only else ''}")
                if not confirm("Открыть агенту ещё одну папку?"):
                    break
        if confirm("Запускать связь с агентом автоматически при входе в систему?\n\n"
                   "Тогда это окно можно будет закрыть — мост будет работать незаметно в фоне."):
            script = HOME_DIR / "bridge.py"
            if Path(__file__).resolve() != script.resolve():
                HOME_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(__file__, script)
            cfg_path.with_suffix(".pid").unlink(missing_ok=True)  # фоновый экземпляр возьмёт замок сам
            log_file = HOME_DIR / "bridge.log"
            start = log_file.stat().st_size if log_file.exists() else 0
            install_autostart(cfg_path, script)
            if wait_background(log_file, start):
                log("готово: мост работает в фоне и запустится сам при входе. Окно можно закрыть.")
                return
            tail = log_tail(log_file, start)
            log("✕ фоновый мост не вышел на связь за 20 с" + (f". Его лог ({log_file}):\n{tail}" if tail else
                                                            f" и ничего не написал в {log_file}"))
            if not take_lock(cfg_path):
                log("фоновый мост запущен, но не подключается — пришлите этот вывод разработчику")
                sys.exit(1)
            log("пока держу связь из этого окна — не закрывайте его")
            try:
                bridge.loop()
            except KeyboardInterrupt:
                log("остановлено")
            return
        log("готово. Пока это окно открыто, агент видит компьютер. Закрыть — Ctrl+C или крестик.")
    else:
        log(f"мост agentkit {VERSION} → {server}")
        for f in bridge.folders.values():
            log(f"  папка {f.name}: {f.path}{' (только чтение)' if f.read_only else ''}")
        log(f"  команды: {'разрешены' if bridge.allow_shell else 'запрещены'}")
    try:
        bridge.loop()
    except KeyboardInterrupt:
        log("остановлено")


if __name__ == "__main__":
    main()
