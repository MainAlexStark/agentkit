"""HTTP API проектов и компьютера пользователя (моста). Подключается из build_app."""

from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agentkit.devices import DeviceHub
from agentkit.tools.base import User

BRIDGE_FILE = Path(__file__).resolve().parent.parent / "bridge.py"


class ProjectIn(BaseModel):
    name: str
    description: str = ""
    instructions: str = ""


class ProjectPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    instructions: str | None = None


class MemberIn(BaseModel):
    user_id: str  # «*» — все пользователи сервиса
    role: Literal["viewer", "editor"] | None = None  # None — убрать доступ


class DeviceIn(BaseModel):
    name: str = ""


class BridgePoll(BaseModel):
    info: dict[str, Any] = {}
    hello: bool = False  # первый опрос: ответить сразу, чтобы мост показал «подключено»


class BridgeResult(BaseModel):
    call_id: str
    ok: bool
    result: Any = None
    error: str = ""


ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}


def register(api: APIRouter, app: Any, svc: Any, auth: Any) -> None:
    # ── пользователи (кому открыть проект) ───────────────────────────
    @api.get("/users")
    async def users(user: User = Depends(auth)):
        ids = {u.id for u in svc.auth.users.values()} | set(await svc.store.known_user_ids())
        out = []
        for uid in sorted(ids):
            prof = await svc.store.kv_get("profile", uid) or {}
            out.append({"id": uid, "name": prof.get("name") or uid})
        return out

    # ── проекты ──────────────────────────────────────────────────────
    def lib():
        if svc.projects is None:
            raise HTTPException(404, "проекты выключены (AGENT_ENABLE_PROJECTS)")
        return svc.projects

    async def access(project_id: str, user: User, need: str = "viewer"):
        lib()
        p, role = await svc.store.project_role(project_id, user.id)
        if p is None or role is None:
            raise HTTPException(404, "нет такого проекта")
        if ROLE_RANK[role] < ROLE_RANK[need]:
            raise HTTPException(403, {"editor": "менять проект могут только редакторы",
                                      "owner": "это может только владелец проекта"}[need])
        return p, role

    @api.get("/projects")
    async def projects(user: User = Depends(auth)):
        lib()
        out = []
        for p, role in await svc.store.project_list_for(user.id):
            files = await svc.store.project_files_list(p.id)
            chats = await svc.store.list_sessions(user.id, project_id=p.id, limit=500)
            members = await svc.store.project_members_list(p.id)
            out.append({**p.__dict__, "role": role, "files": len(files), "chats": len(chats),
                        "shared": bool(members), "public": any(m["user_id"] == "*" for m in members)})
        return out

    @api.post("/projects")
    async def create_project(body: ProjectIn, user: User = Depends(auth)):
        lib()
        if not body.name.strip():
            raise HTTPException(400, "нужно название")
        p = await svc.store.project_create(user.id, body.name.strip()[:200], description=body.description.strip(),
                                           instructions=body.instructions.strip())
        return p.__dict__

    @api.get("/projects/{project_id}")
    async def project(project_id: str, user: User = Depends(auth)):
        p, role = await access(project_id, user)
        files = await svc.store.project_files_list(project_id)
        chats = await svc.store.list_sessions(user.id, project_id=project_id, limit=200)
        members = await svc.store.project_members_list(project_id) if role in ("owner", "editor") else []
        mem = await svc.store.memory_list([f"project:{project_id}"])
        owner = await svc.store.kv_get("profile", p.owner_id) or {}
        return {**p.__dict__, "role": role, "owner_name": owner.get("name") or p.owner_id,
                "files": [f.__dict__ for f in files],
                "chats": [{"id": s.id, "title": s.title or "Новый разговор", "updated_at": s.updated_at} for s in chats],
                "members": members, "memory": [m.__dict__ for m in mem],
                "inline": sum(f.text_chars for f in files) <= svc.settings.project_inline_chars}

    @api.patch("/projects/{project_id}")
    async def patch_project(project_id: str, body: ProjectPatch, user: User = Depends(auth)):
        await access(project_id, user, "editor")
        fields = {k: v.strip() for k, v in body.model_dump().items() if v is not None}
        if "name" in fields and not fields["name"]:
            raise HTTPException(400, "нужно название")
        await svc.store.project_update(project_id, **fields)
        return {"ok": True}

    @api.delete("/projects/{project_id}")
    async def delete_project(project_id: str, user: User = Depends(auth)):
        await access(project_id, user, "owner")
        await lib().delete_project(project_id)
        return {"ok": True}

    @api.post("/projects/{project_id}/files")
    async def add_file(project_id: str, file: UploadFile, user: User = Depends(auth)):
        await access(project_id, user, "editor")
        limit = svc.settings.max_project_file_mb * 1024 * 1024
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, f"файл больше {svc.settings.max_project_file_mb} МБ")
        rec = await lib().add_file(project_id, file.filename or "file", data, file.content_type or "", added_by=user.id)
        return rec.__dict__

    @api.get("/projects/{project_id}/files/{file_id}")
    async def get_file(project_id: str, file_id: str, user: User = Depends(auth)):
        await access(project_id, user)
        rec = await lib().find_file(project_id, file_id)
        if rec is None or rec.id != file_id or not lib().file_path(rec).is_file():
            raise HTTPException(404, "нет файла")
        return FileResponse(lib().file_path(rec), filename=rec.name,
                            media_type=mimetypes.guess_type(rec.name)[0] or "application/octet-stream")

    @api.delete("/projects/{project_id}/files/{file_id}")
    async def del_file(project_id: str, file_id: str, user: User = Depends(auth)):
        await access(project_id, user, "editor")
        if not await lib().delete_file(project_id, file_id):
            raise HTTPException(404, "нет файла")
        return {"ok": True}

    @api.put("/projects/{project_id}/members")
    async def set_member(project_id: str, body: MemberIn, user: User = Depends(auth)):
        p, _ = await access(project_id, user, "owner")
        uid = body.user_id.strip()
        if not uid or uid == p.owner_id:
            raise HTTPException(400, "укажите другого пользователя")
        await svc.store.project_member_set(project_id, uid, body.role)
        return {"ok": True, "members": await svc.store.project_members_list(project_id)}

    @api.delete("/projects/{project_id}/memory/{memory_id}")
    async def del_project_memory(project_id: str, memory_id: str, user: User = Depends(auth)):
        await access(project_id, user, "editor")
        if memory_id not in {m.id for m in await svc.store.memory_list([f"project:{project_id}"])}:
            raise HTTPException(404, "нет такой записи")
        await svc.store.memory_delete(memory_id)
        return {"ok": True}

    app.state.project_access = access

    # ── компьютер пользователя ───────────────────────────────────────
    def hub() -> DeviceHub:
        if svc.devices is None:
            raise HTTPException(404, "компьютер выключен (AGENT_ENABLE_DEVICES)")
        return svc.devices

    def view(d: dict[str, Any]) -> dict[str, Any]:
        return {"id": d["id"], "name": d["name"], "online": DeviceHub.online(d), "last_seen": d.get("last_seen", 0),
                "created_at": d.get("created_at", 0), "info": d.get("info") or {}}

    async def own_device(device_id: str, user: User) -> dict[str, Any]:
        d = await hub().get(device_id)
        if d is None or d["user_id"] != user.id:
            raise HTTPException(404, "нет такого компьютера")
        return d

    @api.get("/devices")
    async def devices(user: User = Depends(auth)):
        return [view(d) for d in await hub().list(user.id)]

    @api.post("/devices")
    async def add_device(body: DeviceIn, user: User = Depends(auth)):
        rec, key = await hub().create(user.id, body.name)
        base = svc.settings.public_url.rstrip("/")
        return {"device": view(rec), "key": key, "server": base, "bridge_url": f"{base}/bridge.py"}

    @api.patch("/devices/{device_id}")
    async def rename_device(device_id: str, body: DeviceIn, user: User = Depends(auth)):
        await own_device(device_id, user)
        await hub().update(device_id, name=body.name.strip()[:80] or "Мой компьютер")
        return {"ok": True}

    @api.delete("/devices/{device_id}")
    async def del_device(device_id: str, user: User = Depends(auth)):
        await own_device(device_id, user)
        await hub().delete(device_id)  # ключ перестаёт работать — мост увидит 401 и остановится
        return {"ok": True}

    async def bridge_device(request: Request) -> dict[str, Any]:
        key = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        d = await hub().auth(key) if key else None
        if d is None:
            raise HTTPException(401, "ключ моста не подошёл")
        return d

    @api.post("/bridge/poll")
    async def bridge_poll(body: BridgePoll, request: Request):
        """Мост ждёт здесь команду до 25 с; пустой ответ — просто спросить снова."""
        d = await bridge_device(request)
        await hub().touch(d, body.info)
        cmd = None if body.hello else await hub().next_command(d["id"])
        return {"cmd": cmd, "ts": time.time()}

    @api.post("/bridge/result")
    async def bridge_result(body: BridgeResult, request: Request):
        d = await bridge_device(request)
        await hub().deliver(d["id"], body.call_id, {"ok": body.ok, "result": body.result, "error": body.error})
        return {"ok": True}

    @app.get("/bridge.py")
    async def bridge_script():
        return FileResponse(BRIDGE_FILE, media_type="text/x-python", filename="bridge.py")
