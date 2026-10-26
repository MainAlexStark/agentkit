"""Компьютер пользователя: мост (bridge.py) на его машине и инструменты агента pc_*.

Мост сам ходит на сервер (длинный опрос /api/bridge/poll) — поэтому работает за
NAT и без открытых портов. Сервер кладёт команду в очередь шины, мост забирает,
выполняет и возвращает ответ. Какие папки открыты и можно ли выполнять команды,
решает человек при запуске моста, а не сервер: даже взломанный сервис не
расширит доступ к компьютеру.

Папки проекта (как в Cowork): человек привязывает к проекту свои открытые папки.
В разговоре такого проекта pc_* видят только их — это проверяет код инструментов,
а не промпт. Привязка личная (проект общий, а компьютер у каждого свой) и хранится
по пути: имя папки мост выдаёт заново при каждом открытии («Docs», «Docs-2»).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Annotated, Any

from agentkit.messages import new_id
from agentkit.projects import session_project_id
from agentkit.tools.base import Tool, ToolContext, ToolResult, User, tool

ONLINE_WINDOW = 45.0  # мост опрашивает раз в ≤25 с — если 45 с тишины, считаем отключённым
POLL_WAIT = 25.0
DEVICE_TOOLS = {"pc_list", "pc_read", "pc_search", "pc_write", "pc_edit", "pc_run", "pc_request_access"}
FOLDER_TOOLS = {"pc_list", "pc_read", "pc_search", "pc_write", "pc_edit"}
NS = "devices"
NS_PROJECT = "project_folders"  # ключ «<проект>:<пользователь>» → привязанные папки


class DeviceError(Exception):
    pass


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class DeviceHub:
    def __init__(self, store: Any, bus: Any):
        self.store = store
        self.bus = bus

    # ── учёт устройств ───────────────────────────────────────────────
    async def create(self, user_id: str, name: str = "") -> tuple[dict[str, Any], str]:
        did = new_id("dev_")
        secret = secrets.token_urlsafe(24)
        rec = {"id": did, "user_id": user_id, "name": name.strip()[:80] or "Мой компьютер", "hash": _hash(secret),
               "created_at": time.time(), "last_seen": 0.0, "info": {}}
        await self.store.kv_set(NS, did, rec)
        return rec, f"{did}.{secret}"

    async def auth(self, key: str) -> dict[str, Any] | None:
        did, _, secret = key.partition(".")
        rec = await self.store.kv_get(NS, did) if did.startswith("dev_") and secret else None
        if rec and hmac.compare_digest(rec["hash"], _hash(secret)):
            return rec
        return None

    async def list(self, user_id: str) -> list[dict[str, Any]]:
        items = [r for r in (await self.store.kv_list(NS)).values() if r.get("user_id") == user_id]
        return sorted(items, key=lambda r: -r.get("last_seen", 0))

    async def get(self, device_id: str) -> dict[str, Any] | None:
        return await self.store.kv_get(NS, device_id)

    async def update(self, device_id: str, **fields: Any) -> None:
        rec = await self.get(device_id)
        if rec:
            rec.update(fields)
            await self.store.kv_set(NS, device_id, rec)

    async def delete(self, device_id: str) -> None:
        await self.store.kv_delete(NS, device_id)

    async def touch(self, rec: dict[str, Any], info: dict[str, Any]) -> None:
        # Пишем не чаще раза в 10 с: опрос идёт постоянно, а база — не счётчик.
        now = time.time()
        clean = _clean_info(info)
        if now - rec.get("last_seen", 0) > 10 or clean != rec.get("info"):
            rec["last_seen"], rec["info"] = now, clean
            await self.store.kv_set(NS, rec["id"], rec)

    @staticmethod
    def online(rec: dict[str, Any]) -> bool:
        return time.time() - rec.get("last_seen", 0) < ONLINE_WINDOW

    async def pick(self, user_id: str, name: str = "") -> dict[str, Any]:
        on = [d for d in await self.list(user_id) if self.online(d)]
        if not on:
            raise DeviceError("компьютер не подключён: запустите мост (Настройки → Компьютер)")
        if name:
            low = name.lower()
            hit = [d for d in on if low in d["name"].lower() or d["id"] == name]
            if not hit:
                raise DeviceError(f"нет подключённого компьютера «{name}»; есть: {', '.join(d['name'] for d in on)}")
            return hit[0]
        return on[0]

    # ── команды ──────────────────────────────────────────────────────
    async def call(self, device: dict[str, Any], op: str, args: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        call_id = new_id("c_")
        await self.bus.push(f"dev:{device['id']}", {"id": call_id, "op": op, "args": args,
                                                    "deadline": time.time() + timeout}, ttl=timeout + 30)
        # Ключ ответа включает устройство: чужой мост не подсунет результат в эту команду.
        res = await self.bus.pop(f"devres:{device['id']}:{call_id}", timeout + 5)
        if res is None:
            raise DeviceError(f"компьютер «{device['name']}» не ответил за {int(timeout)} с — возможно, мост остановлен")
        if not res.get("ok"):
            raise DeviceError(res.get("error") or "ошибка на компьютере")
        return res.get("result") or {}

    async def next_command(self, device_id: str, wait: float = POLL_WAIT) -> dict[str, Any] | None:
        end = time.monotonic() + wait
        while (left := end - time.monotonic()) > 0:
            cmd = await self.bus.pop(f"dev:{device_id}", left)
            if cmd is None:
                return None
            if cmd.get("deadline", 0) > time.time():
                return cmd  # просроченные (мост был офлайн) — молча пропускаем
        return None

    async def deliver(self, device_id: str, call_id: str, result: dict[str, Any]) -> None:
        await self.bus.push(f"devres:{device_id}:{call_id}", result, ttl=120)

    # ── папки проекта ────────────────────────────────────────────────
    async def project_folders(self, project_id: str, user_id: str) -> list[dict[str, Any]]:
        return (await self.store.kv_get(NS_PROJECT, f"{project_id}:{user_id}") or {}).get("items", [])

    async def bind_folder(self, project_id: str, user_id: str, device_id: str, path: str) -> dict[str, Any]:
        items = await self.project_folders(project_id, user_id)
        hit = next((b for b in items if b["device_id"] == device_id and b["path"] == path), None)
        if hit:
            return hit
        rec = {"id": new_id("pf_"), "device_id": device_id, "path": path[:300], "added_at": time.time()}
        await self.store.kv_set(NS_PROJECT, f"{project_id}:{user_id}", {"items": [*items, rec][-20:]})
        return rec

    async def unbind_folder(self, project_id: str, user_id: str, binding_id: str) -> bool:
        items = await self.project_folders(project_id, user_id)
        rest = [b for b in items if b["id"] != binding_id]
        if len(rest) == len(items):
            return False
        await self.store.kv_set(NS_PROJECT, f"{project_id}:{user_id}", {"items": rest})
        return True

    async def drop_project(self, project_id: str) -> None:
        for key in await self.store.kv_list(NS_PROJECT):
            if key.startswith(project_id + ":"):
                await self.store.kv_delete(NS_PROJECT, key)

    async def project_folders_view(self, project_id: str, user_id: str) -> list[dict[str, Any]]:
        """Привязки с текущим состоянием: на связи ли компьютер, открыта ли папка в мосте и под каким именем."""
        devs = {d["id"]: d for d in await self.list(user_id)}
        out = []
        for b in await self.project_folders(project_id, user_id):
            d = devs.get(b["device_id"])
            f = next((f for f in ((d or {}).get("info") or {}).get("folders", []) if f["path"] == b["path"]), None)
            out.append({**b, "device": d["name"] if d else "", "online": bool(d and self.online(d)),
                        "name": f["name"] if f else _basename(b["path"]), "open": f is not None,
                        "read_only": bool(f and f.get("read_only")), "missing_device": d is None})
        return out

    async def session_folders(self, user: User, session_id: str) -> list[dict[str, Any]]:
        """Папки проекта этого разговора (пусто — разговор не в проекте или к проекту ничего не привязано)."""
        pid = await session_project_id(self.store, session_id)
        return await self.project_folders_view(pid, user.id) if pid else []

    @staticmethod
    def scope_of(items: list[dict[str, Any]]) -> dict[str, set[str]] | None:
        """Какие папки можно трогать: None — все открытые, иначе {устройство: {имена открытых папок проекта}}.

        Разговор проекта с привязанными папками видит только их: иначе агент из проекта «Бухгалтерия»
        полез бы в папку проекта «Личное» на том же компьютере, просто потому что она открыта.
        """
        if not items:
            return None
        out: dict[str, set[str]] = {b["device_id"]: set() for b in items if not b["missing_device"]}
        for b in items:
            if b["open"] and b["device_id"] in out:
                out[b["device_id"]].add(b["name"])
        return out

    async def choose(self, user: User, scope: dict[str, set[str]] | None, device: str = "") -> dict[str, Any]:
        if scope is None:
            return await self.pick(user.id, device)
        on = [d for d in await self.list(user.id) if self.online(d) and d["id"] in scope]
        if not on:
            raise DeviceError("компьютер с папками проекта не на связи — включите его, мост подключится сам")
        if device:
            low = device.lower()
            on = [d for d in on if low in d["name"].lower() or d["id"] == device]
            if not on:
                raise DeviceError(f"в этом проекте нет папок на компьютере «{device}»")
        return next((d for d in on if scope[d["id"]]), on[0])  # сначала тот, где папки проекта открыты

    # ── контекст для агента ──────────────────────────────────────────
    async def context(self, user: User, session_id: str):
        from agentkit.agent import TurnContext

        items = await self.session_folders(user, session_id)
        scope = self.scope_of(items)
        on = [d for d in await self.list(user.id) if self.online(d) and (scope is None or d["id"] in scope)]
        if not on:
            if scope is not None:
                return TurnContext(text="# Компьютер пользователя\nК проекту привязаны папки на компьютере, "
                                        "но он сейчас не на связи. Если нужны эти файлы — скажи человеку "
                                        "включить компьютер (мост подключится сам).", hide_tools=set(DEVICE_TOOLS))
            return TurnContext(hide_tools=set(DEVICE_TOOLS))
        lines = ["# Компьютер пользователя",
                 "Через мост подключён компьютер человека — инструменты pc_*. Пути пиши как «папка/путь внутри», "
                 "где папка — одна из открытых ниже; выйти за них нельзя."]
        if scope is not None:
            lines.append("К проекту привязаны свои папки — в этом разговоре доступны только они. "
                         "Это рабочая папка проекта: ищи и сохраняй результаты в ней.")
        hide: set[str] = set()
        any_folders = False
        for d in on:
            info = d.get("info") or {}
            folders = [f for f in info.get("folders") or [] if scope is None or f["name"] in scope[d["id"]]]
            any_folders = any_folders or bool(folders)
            fl = ", ".join(f"{f['name']}{' (только чтение)' if f.get('read_only') else ''}" for f in folders) or "нет"
            lines.append(f"- «{d['name']}» ({info.get('platform', '?')}): папки — {fl}; "
                         f"команды — {'можно' if info.get('shell') else 'запрещены'}")
        closed = [b for b in items if b["online"] and not b["open"]]
        if closed:
            lines.append("Папки проекта, закрытые на компьютере (человек убрал доступ): "
                         + ", ".join(f"{b['name']} ({b['path']})" for b in closed)
                         + ". Нужны — попроси доступ через pc_request_access.")
        if not any((d.get("info") or {}).get("shell") for d in on):
            hide.add("pc_run")
        if not any_folders:
            hide |= FOLDER_TOOLS
            lines.append("Папок пока не открыто: если нужны файлы с компьютера, попроси доступ через pc_request_access.")
        else:
            lines.append("Нужна другая папка — попроси доступ через pc_request_access (человек выберет её у себя"
                         + (", и она привяжется к проекту)." if scope is not None else ")."))
        if len(on) > 1:
            lines.append("Компьютеров несколько — укажи device, иначе берётся последний активный.")
        return TurnContext(text="\n".join(lines), hide_tools=hide)


def _clean_info(info: dict[str, Any]) -> dict[str, Any]:
    folders = [{"name": str(f.get("name", ""))[:80], "path": str(f.get("path", ""))[:300],
                "read_only": bool(f.get("read_only"))} for f in (info.get("folders") or [])[:20]]
    return {"hostname": str(info.get("hostname", ""))[:80], "platform": str(info.get("platform", ""))[:80],
            "version": str(info.get("version", ""))[:20], "shell": bool(info.get("shell")), "folders": folders}


def _root(path: str) -> str:
    # Так же, как bridge.resolve: «папка/путь» → «папка»; пусто — корень (список открытых папок).
    p = (path or "").replace("\\", "/").strip().strip("/")
    return "" if not p.strip(" ./") else p.partition("/")[0]


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rpartition("/")[2] or path


def device_tools(hub: DeviceHub) -> list[Tool]:
    async def target(ctx: ToolContext, device: str) -> tuple[dict[str, Any], set[str] | None]:
        """Компьютер для команды и папки, которые в этом разговоре можно трогать (None — любые открытые)."""
        scope = hub.scope_of(await hub.session_folders(ctx.user, ctx.session_id))
        dev = await hub.choose(ctx.user, scope, device)
        allowed = None if scope is None else scope[dev["id"]]
        if allowed is not None and not allowed:
            raise DeviceError("папки проекта на этом компьютере закрыты — откройте их снова: проект → «Папки на компьютере»")
        return dev, allowed

    def check(path: str, allowed: set[str] | None) -> None:
        root = _root(path)
        if allowed is not None and root and root not in allowed:
            raise DeviceError(f"папка «{root}» не привязана к этому проекту; доступны: {', '.join(sorted(allowed))}")

    async def run(ctx: ToolContext, device: str, op: str, args: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        dev, allowed = await target(ctx, device)
        if allowed is not None:
            if op == "list" and not _root(args.get("path", "")):
                return {"path": "", "items": [{"name": n, "dir": True} for n in sorted(allowed)]}
            if op == "search" and not _root(args.get("path", "")):
                # Пустой путь у моста — «во всех открытых»; в проекте ищем только по его папкам.
                hits, more = [], False
                for name in sorted(allowed):
                    r = await hub.call(dev, op, {**args, "path": name}, timeout)
                    hits += r.get("hits", [])
                    more = more or bool(r.get("more"))
                return {"hits": hits[:200], "more": more or len(hits) > 200}
            if op == "run" and not _root(args.get("cwd", "")):
                args = {**args, "cwd": sorted(allowed)[0]}
            check(args.get("cwd", "") if op == "run" else args.get("path", ""), allowed)
        return await hub.call(dev, op, args, timeout)

    def err(e: Exception) -> ToolResult:
        return ToolResult.error(str(e))

    @tool(title="Папка на компьютере", read_only=True, group="device", source="builtin")
    async def pc_list(ctx: ToolContext, path: Annotated[str, "Папка: «»/«.» — открытые папки, иначе «папка/путь»"] = "",
                      device: Annotated[str, "Какой компьютер (если их несколько); пусто — последний активный"] = "") -> ToolResult:
        """Показывает содержимое папки на компьютере пользователя."""
        try:
            r = await run(ctx, device, "list", {"path": path})
        except Exception as e:  # noqa: BLE001
            return err(e)
        rows = [f"{it['name']}/" if it["dir"] else f"{it['name']}  ({_size(it.get('size', 0))})" for it in r.get("items", [])]
        more = f"\n… и ещё {r['more']}" if r.get("more") else ""
        return ToolResult(content=f"{r.get('path') or 'Открытые папки'}:\n" + ("\n".join(rows) or "(пусто)") + more, data=r)

    @tool(title="Файл с компьютера", read_only=True, group="device", source="builtin")
    async def pc_read(ctx: ToolContext, path: Annotated[str, "«папка/путь/файл»"],
                      offset: Annotated[int, "С какой строки (с 0)"] = 0,
                      limit: Annotated[int, "Сколько строк"] = 400, device: Annotated[str, "Какой компьютер (если их несколько); пусто — последний активный"] = "") -> ToolResult:
        """Читает текстовый файл с компьютера пользователя (PDF/DOCX/XLSX — текстом)."""
        try:
            r = await run(ctx, device, "read", {"path": path, "offset": offset, "limit": limit})
        except Exception as e:  # noqa: BLE001
            return err(e)
        tail = f"\n[ещё строк: {r['more']} — продолжи с offset={offset + r['lines']}]" if r.get("more") else ""
        return ToolResult(content=r.get("text", "") + tail, data={k: v for k, v in r.items() if k != "text"})

    @tool(title="Поиск на компьютере", read_only=True, group="device", source="builtin", timeout=120)
    async def pc_search(ctx: ToolContext, query: Annotated[str, "Текст (или имя файла при names_only)"],
                        path: Annotated[str, "Где искать: «папка/подпапка»; пусто — во всех открытых"] = "",
                        glob: Annotated[str, "Фильтр имён, например *.py или *.docx"] = "",
                        names_only: Annotated[bool, "Искать по именам файлов, а не по содержимому"] = False,
                        device: Annotated[str, "Какой компьютер (если их несколько); пусто — последний активный"] = "") -> ToolResult:
        """Ищет файлы по имени или текст внутри файлов на компьютере пользователя."""
        try:
            r = await run(ctx, device, "search", {"query": query, "path": path, "glob": glob,
                                                  "names_only": names_only}, timeout=110)
        except Exception as e:  # noqa: BLE001
            return err(e)
        hits = r.get("hits", [])
        if not hits:
            return ToolResult(content="Ничего не нашлось.")
        out = [h["path"] + (f":{h['line']}: {h['text']}" if "line" in h else "") for h in hits]
        return ToolResult(content="\n".join(out) + ("\n… есть ещё совпадения" if r.get("more") else ""), data=r)

    @tool(title="Запись файла на компьютер", group="device", source="builtin",
          describe=lambda a: [["Файл", a.get("path", "")], ["Размер", f"{len(a.get('content', ''))} символов"]])
    async def pc_write(ctx: ToolContext, path: Annotated[str, "«папка/путь/файл»"],
                       content: Annotated[str, "Полное содержимое файла"], device: Annotated[str, "Какой компьютер (если их несколько); пусто — последний активный"] = "") -> ToolResult:
        """Создаёт или перезаписывает текстовый файл на компьютере пользователя."""
        try:
            r = await run(ctx, device, "write", {"path": path, "content": content})
        except Exception as e:  # noqa: BLE001
            return err(e)
        return ToolResult(content=f"Записано: {r.get('path', path)} ({r.get('size', 0)} Б)", data=r)

    @tool(title="Правка файла на компьютере", group="device", source="builtin",
          describe=lambda a: [["Файл", a.get("path", "")], ["Было", a.get("old", "")[:300]], ["Станет", a.get("new", "")[:300]]])
    async def pc_edit(ctx: ToolContext, path: Annotated[str, "«папка/путь/файл»"],
                      old: Annotated[str, "Точный фрагмент, который заменить (должен встречаться один раз)"],
                      new: Annotated[str, "Чем заменить"], device: Annotated[str, "Какой компьютер (если их несколько); пусто — последний активный"] = "") -> ToolResult:
        """Заменяет фрагмент в текстовом файле на компьютере пользователя."""
        try:
            r = await run(ctx, device, "edit", {"path": path, "old": old, "new": new})
        except Exception as e:  # noqa: BLE001
            return err(e)
        return ToolResult(content=f"Изменено: {r.get('path', path)}", data=r)

    @tool(title="Команда на компьютере", group="device", destructive=True, source="builtin", timeout=330,
          describe=lambda a: [["Команда", a.get("command", "")], ["Где", a.get("cwd") or "первая открытая папка"]])
    async def pc_run(ctx: ToolContext, command: Annotated[str, "Команда оболочки"],
                     cwd: Annotated[str, "Рабочая папка «папка/путь»; пусто — первая открытая (в проекте — папка проекта)"] = "",
                     timeout: Annotated[int, "Сколько ждать, секунд (до 300)"] = 60, device: Annotated[str, "Какой компьютер (если их несколько); пусто — последний активный"] = "") -> ToolResult:
        """Выполняет команду в оболочке на компьютере пользователя (если он разрешил это при запуске моста)."""
        t = max(5, min(int(timeout), 300))
        try:
            r = await run(ctx, device, "run", {"command": command, "cwd": cwd, "timeout": t}, timeout=t + 10)
        except Exception as e:  # noqa: BLE001
            return err(e)
        body = [f"Код выхода: {r.get('code')}", f"stdout:\n{r.get('stdout', '')}" if r.get("stdout") else "",
                f"stderr:\n{r.get('stderr', '')}" if r.get("stderr") else ""]
        return ToolResult(content="\n".join(b for b in body if b), is_error=r.get("code") not in (0, None), data=r)

    @tool(title="Доступ к папке на компьютере", read_only=True, group="device", source="builtin", timeout=330)
    async def pc_request_access(
        ctx: ToolContext,
        reason: Annotated[str, "Зачем нужна папка — человек увидит это в окне выбора, например «чтобы найти договоры за 2025 год»"],
        device: Annotated[str, "Какой компьютер (если их несколько); пусто — последний активный"] = "",
    ) -> ToolResult:
        """Просит человека открыть папку: на его компьютере появляется обычное окно выбора папки.
        Используй, когда нужных файлов нет в уже открытых папках. Ждёт, пока человек выберет (до 5 минут)."""
        try:
            pid = await session_project_id(ctx.store, ctx.session_id)
            scope = hub.scope_of(await hub.session_folders(ctx.user, ctx.session_id))
            dev = await hub.choose(ctx.user, scope, device)
            r = await hub.call(dev, "pick_folder", {"reason": reason}, timeout=300)
        except Exception as e:  # noqa: BLE001
            return err(e)
        mode = "только чтение" if r.get("read_only") else "чтение и запись"
        note = ""
        if pid and (await ctx.store.project_role(pid, ctx.user.id))[1]:
            # Папку, открытую из разговора проекта, сразу привязываем к нему — иначе в разговоре
            # с ограничением по папкам проекта она открылась бы, но осталась недоступной.
            await hub.bind_folder(pid, ctx.user.id, dev["id"], r["path"])
            note = " Привязана к проекту."
        return ToolResult(content=f"Открыта папка «{r['name']}» ({mode}). Пути: {r['name']}/….{note}", data=r)

    return [pc_list, pc_read, pc_search, pc_write, pc_edit, pc_run, pc_request_access]


def _size(n: int) -> str:
    return f"{n} Б" if n < 1024 else f"{n / 1024:.0f} КБ" if n < 1048576 else f"{n / 1048576:.1f} МБ"
