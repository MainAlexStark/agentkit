"""HTTP API агента и веб-чат.

Прогоны идут в фоне (RunManager), клиент следит за ними по SSE
(/api/sessions/{id}/events) или опросом (/api/sessions/{id}/poll) — второй
вариант для хостов, где прокси не держит долгие соединения.
"""

from __future__ import annotations

import html
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agentkit.attachments import UPLOADS, save_upload
from agentkit.events import EventType
from agentkit.oauth import OAuthError, OAuthOptions
from agentkit.permissions import Mode
from agentkit.runs import Busy
from agentkit.scheduler import next_run, validate_cron
from agentkit.server.auth import require_admin
from agentkit.server.service import AgentService
from agentkit.store.base import ScheduleRec
from agentkit.tools.base import User

STATIC = Path(__file__).parent / "static"


class NewSession(BaseModel):
    title: str = ""
    model: str | None = None
    mode: str | None = None


class PatchSession(BaseModel):
    title: str | None = None
    model: str | None = None
    mode: str | None = None


class SendMessage(BaseModel):
    text: str = ""
    files: list[dict[str, Any]] = []  # то, что вернул POST /files
    model: str | None = None
    mode: str | None = None


class Decide(BaseModel):
    approve: bool
    comment: str = ""
    args: dict[str, Any] | None = None


class Rewind(BaseModel):
    seq: int


class ProfileIn(BaseModel):
    name: str = ""  # полное имя
    call_me: str = ""  # как обращаться
    work: str = ""  # чем занимается
    instructions: str = ""  # пожелания к ответам — уходят в системный промпт


class MemoryIn(BaseModel):
    text: str
    scope: str = "user"  # user | global


class ScheduleIn(BaseModel):
    name: str
    cron: str
    prompt: str
    enabled: bool = True
    keep_session: bool = False


class HeadlessRun(BaseModel):
    text: str
    session_id: str | None = None
    model: str | None = None
    mode: str | None = None


def build_app(svc: AgentService, *, extra_routers: list[APIRouter] | None = None,
              static_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await svc.start()
        yield
        await svc.stop()

    app = FastAPI(title=svc.settings.name, lifespan=lifespan)
    if svc.settings.allowed_origins:
        app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in svc.settings.allowed_origins.split(",")],
                           allow_methods=["*"], allow_headers=["*"])
    auth = svc.auth
    api = APIRouter(prefix="/api")

    async def own_session(session_id: str, user: User):
        s = await svc.store.get_session(session_id)
        if s is None or (s.user_id != user.id and not user.has_role("admin")):
            raise HTTPException(404, "нет такой сессии")
        return s

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "agent": svc.settings.name, "tools": len(svc.registry or [])}

    @api.get("/me")
    async def me(user: User = Depends(auth)):
        prof = await svc.store.kv_get("profile", user.id) or {}
        return {"id": user.id, "name": prof.get("name") or user.name, "call_me": prof.get("call_me", ""),
                "roles": user.roles}

    @api.get("/profile")
    async def profile(user: User = Depends(auth)):
        return {"name": user.name, "call_me": "", "work": "", "instructions": "",
                **(await svc.store.kv_get("profile", user.id) or {})}

    @api.put("/profile")
    async def save_profile(body: ProfileIn, user: User = Depends(auth)):
        data = {k: v.strip()[:4000] for k, v in body.model_dump().items()}
        await svc.store.kv_set("profile", user.id, data)
        return data

    @api.get("/plan")
    async def plan(user: User = Depends(auth)):
        now = time.localtime()
        month_start = time.mktime((now.tm_year, now.tm_mon, 1, 0, 0, 0, 0, 0, -1))
        usage = await svc.store.usage_summary(user_id=user.id, since=month_start)
        s = svc.settings
        info: dict[str, Any] = {"name": s.plan_name or "Без ограничений", "status": "active",
                                "limit_usd": s.monthly_budget_usd or None, "manage_url": s.billing_url or None,
                                "features": []}
        if svc.domain.plan is not None:
            res = svc.domain.plan(user, usage)
            if hasattr(res, "__await__"):
                res = await res
            info.update(res or {})
        return {**info, "usage": usage, "period_start": month_start}

    @api.get("/config")
    async def config(user: User = Depends(auth)):
        s = svc.settings
        return {"name": s.name, "models": s.model_list(), "default_model": s.model_main,
                "modes": [m.value for m in Mode if m != Mode.BYPASS or user.has_role("admin")],
                "default_mode": s.default_mode, "is_admin": user.has_role("admin"),
                "description": svc.domain.description or s.description,
                "suggestions": svc.domain.suggestions or [x.strip() for x in s.suggestions.split("|") if x.strip()],
                "titles": {t.name: t.title for t in svc.registry.all() if t.title},
                "help_url": s.help_url or None, "plan_name": s.plan_name or None}

    # ── сессии ───────────────────────────────────────────────────────
    @api.get("/sessions")
    async def list_sessions(user: User = Depends(auth)):
        items = await svc.store.list_sessions(user.id, limit=100)
        return [{"id": s.id, "title": s.title or "Новый разговор", "updated_at": s.updated_at,
                 "running": await svc.runs.active_run(s.id) is not None} for s in items]

    @api.post("/sessions")
    async def create_session(body: NewSession, user: User = Depends(auth)):
        state = {k: v for k, v in {"model": body.model, "mode": _check_mode(body.mode, user)}.items() if v}
        s = await svc.store.create_session(user.id, title=body.title, agent=svc.agent.spec.name, state=state)
        return {"id": s.id}

    @api.get("/sessions/{session_id}")
    async def get_session(session_id: str, user: User = Depends(auth)):
        s = await own_session(session_id, user)
        msgs = await svc.store.list_messages(session_id)
        pending = await svc.store.list_approvals(session_id, status="pending")
        return {
            "id": s.id, "title": s.title, "state": s.state,
            "messages": [{"seq": seq, **m.to_dict()} for seq, m in msgs],
            "approvals": [{**a.__dict__, "destructive": bool((t := svc.registry.get(a.tool_name)) and t.destructive),
                           "title": t.title if t else ""} for a in pending],
            "running_run": await svc.runs.active_run(session_id),
            "queued": await svc.store.inbox_count(session_id),
            "last_seq": await svc.store.last_event_seq(session_id),
            "runs": [r.__dict__ for r in await svc.store.list_runs(session_id, limit=20)],
        }

    @api.patch("/sessions/{session_id}")
    async def patch_session(session_id: str, body: PatchSession, user: User = Depends(auth)):
        s = await own_session(session_id, user)
        if body.title is not None:
            await svc.store.update_session(session_id, title=body.title)
        if body.model is not None or body.mode is not None:
            state = dict(s.state or {})
            if body.model is not None:
                state["model"] = body.model
            if body.mode is not None:
                state["mode"] = _check_mode(body.mode, user)
            await svc.store.set_state(session_id, state)
        return {"ok": True}

    @api.delete("/sessions/{session_id}")
    async def delete_session(session_id: str, user: User = Depends(auth)):
        await own_session(session_id, user)
        if await svc.runs.active_run(session_id):
            raise HTTPException(409, "сначала остановите прогон")
        await svc.store.delete_session(session_id)
        return {"ok": True}

    @api.post("/sessions/{session_id}/messages")
    async def send(session_id: str, body: SendMessage, user: User = Depends(auth)):
        """Если агент занят — сообщение встаёт в очередь, и он подхватит его между шагами (queued: true)."""
        await own_session(session_id, user)
        mode = _check_mode(body.mode, user)
        content = _content(session_id, body)
        if not content:
            raise HTTPException(400, "пустое сообщение")
        return await svc.send(session_id, user, content, model=body.model, mode=mode)

    @api.post("/sessions/{session_id}/cancel")
    async def cancel(session_id: str, user: User = Depends(auth)):
        await own_session(session_id, user)
        return {"ok": await svc.runs.cancel(session_id)}

    @api.post("/sessions/{session_id}/rewind")
    async def rewind(session_id: str, body: Rewind, user: User = Depends(auth)):
        await own_session(session_id, user)
        if await svc.runs.active_run(session_id):
            raise HTTPException(409, "сначала остановите прогон")
        return await svc.agent.rewind(session_id, body.seq)

    @api.get("/sessions/{session_id}/events")
    async def events(session_id: str, after: int = 0, user: User = Depends(auth)):
        await own_session(session_id, user)

        async def gen():
            yield ": ok\n\n"
            async for ev in svc.runs.subscribe(session_id, after):
                yield f"id: {ev.get('seq', 0)}\nevent: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
            yield "event: idle\ndata: {}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @api.get("/sessions/{session_id}/poll")
    async def poll(session_id: str, after: int = 0, user: User = Depends(auth)):
        await own_session(session_id, user)
        evs = await svc.store.list_events(session_id=session_id, after_seq=after, limit=500)
        return {"events": [e.to_dict() for e in evs], "running": await svc.runs.active_run(session_id) is not None}

    # ── подтверждения ────────────────────────────────────────────────
    @api.post("/approvals/{approval_id}")
    async def decide(approval_id: str, body: Decide, user: User = Depends(auth)):
        rec = await svc.store.get_approval(approval_id)
        if rec is None:
            raise HTTPException(404, "нет такого подтверждения")
        await own_session(rec.session_id, user)
        if rec.status != "pending":
            raise HTTPException(409, f"уже решено: {rec.status}")
        try:
            run_id = await svc.runs.start(
                rec.session_id,
                lambda: svc.agent.resolve_approval(approval_id, user, approve=body.approve, comment=body.comment,
                                                   edited_args=body.args),
                cancel=svc.agent.cancel)
        except Busy:
            raise HTTPException(409, "агент ещё работает в этом разговоре") from None
        return {"run_id": run_id}

    # ── синхронный запуск для интеграций ─────────────────────────────
    @api.post("/run")
    async def headless(body: HeadlessRun, user: User = Depends(auth)):
        """Для других сервисов: отправить задачу и дождаться итога одним запросом."""
        sid = body.session_id
        if sid:
            await own_session(sid, user)
        else:
            sid = (await svc.store.create_session(user.id, title=body.text[:80])).id
        mode = _check_mode(body.mode, user)
        try:
            # Через RunManager, а не напрямую: прогон виден в веб-чате, его можно
            # остановить, и второй запрос в ту же сессию получит 409, а не гонку.
            run_id = await svc.runs.start(
                sid, lambda: svc.agent.run(sid, user, body.text, model=body.model, mode=mode, trigger="api"),
                cancel=svc.agent.cancel)
        except Busy:
            raise HTTPException(409, "в сессии уже идёт прогон") from None
        await svc.runs.wait(sid)
        run = await svc.store.get_run(run_id)
        evs = await svc.store.list_events(session_id=sid, limit=100_000)
        text = next((e.data.get("content") for e in reversed(evs)
                     if e.run_id == run_id and e.type == EventType.MESSAGE and not e.agent and e.data.get("content")), "")
        pending = await svc.store.list_approvals(sid, status="pending")
        return {"session_id": sid, "run_id": run_id, "status": run.status if run else "unknown", "text": text,
                "usage": run.usage if run else {}, "approvals": [a.__dict__ for a in pending]}

    # ── память ───────────────────────────────────────────────────────
    @api.get("/memory")
    async def memory_list(user: User = Depends(auth)):
        items = await svc.store.memory_list(["global", f"user:{user.id}"])
        return [m.__dict__ for m in items]

    @api.post("/memory")
    async def memory_add(body: MemoryIn, user: User = Depends(auth)):
        if body.scope == "global":
            require_admin(user)
        rec = await svc.store.memory_add("global" if body.scope == "global" else f"user:{user.id}", body.text,
                                         created_by=user.id)
        return rec.__dict__

    @api.delete("/memory/{memory_id}")
    async def memory_delete(memory_id: str, user: User = Depends(auth)):
        mine = {m.id for m in await svc.store.memory_list([f"user:{user.id}"])}
        if memory_id not in mine:
            require_admin(user)
        return {"ok": await svc.store.memory_delete(memory_id)}

    # ── инструменты, MCP, расход ─────────────────────────────────────
    @api.get("/tools")
    async def tools(user: User = Depends(auth)):
        return {
            "tools": [{"name": t.name, "title": t.title, "description": t.description.split("\n")[0], "read_only": t.read_only,
                       "destructive": t.destructive, "deferred": t.deferred, "group": t.group, "source": t.source}
                      for t in svc.registry.all()],
            "mcp": [{"name": s.name, "ok": s.ok, "tools": s.tools, "error": s.error, "needs_auth": s.needs_auth,
                     "oauth": s.oauth} for s in svc.mcp.status.values()],
            "skills": [{"name": k.name, "description": k.description} for k in (svc.agent.skills.skills.values()
                                                                               if svc.agent.skills else [])],
        }

    @api.post("/reload")
    async def reload(user: User = Depends(auth)):
        require_admin(user)
        await svc.reload()
        return {"ok": True, "tools": len(svc.registry)}

    @api.get("/usage")
    async def usage(days: int = 30, user: User = Depends(auth)):
        since = time.time() - days * 86400
        return await svc.store.usage_summary(user_id=None if user.has_role("admin") else user.id, since=since)

    # ── расписание ───────────────────────────────────────────────────
    @api.get("/schedules")
    async def schedules(user: User = Depends(auth)):
        return [s.__dict__ for s in await svc.store.schedule_list(user_id=None if user.has_role("admin") else user.id)]

    @api.post("/schedules")
    async def add_schedule(body: ScheduleIn, user: User = Depends(auth)):
        if not validate_cron(body.cron):
            raise HTTPException(400, "неверное cron-выражение (5 полей: минута час день месяц день_недели)")
        from agentkit.messages import new_id

        sid = None
        if body.keep_session:
            sid = (await svc.store.create_session(user.id, title=f"⏰ {body.name}")).id
        rec = ScheduleRec(id=new_id("sc_"), name=body.name, cron=body.cron, prompt=body.prompt, user_id=user.id,
                          enabled=body.enabled, session_id=sid, next_run_at=next_run(body.cron, svc.settings.tz))
        await svc.store.schedule_upsert(rec)
        return rec.__dict__

    @api.delete("/schedules/{schedule_id}")
    async def del_schedule(schedule_id: str, user: User = Depends(auth)):
        items = {s.id: s for s in await svc.store.schedule_list()}
        s = items.get(schedule_id)
        if s is None or (s.user_id != user.id and not user.has_role("admin")):
            raise HTTPException(404, "нет такого расписания")
        await svc.store.schedule_delete(schedule_id)
        return {"ok": True}

    # ── файлы ────────────────────────────────────────────────────────
    def _workspace(session_id: str) -> Path:
        ws = svc.agent.workspace_for(session_id)
        if ws is None:
            raise HTTPException(400, "у агента нет рабочей папки (AGENT_ENABLE_FILES=false)")
        return ws

    def _content(session_id: str, body: SendMessage) -> Any:
        if not body.files:
            return body.text.strip()
        ws = _workspace(session_id)
        parts: list[dict[str, Any]] = [{"type": "text", "text": body.text}] if body.text.strip() else []
        for f in body.files:
            path = str(f.get("path", ""))
            target = (ws / path).resolve()
            # Ссылку присылает клиент — проверяем, что это действительно загруженный файл этой сессии.
            if not path.startswith(UPLOADS + "/") or ws.resolve() not in target.parents or not target.is_file():
                raise HTTPException(400, f"нет загруженного файла {path}")
            parts.append({k: f[k] for k in ("type", "path", "name", "kind", "size", "mime") if k in f} | {"type": "file"})
        return parts

    @api.post("/sessions/{session_id}/files")
    async def upload(session_id: str, file: UploadFile, user: User = Depends(auth)):
        await own_session(session_id, user)
        data = await file.read(svc.settings.max_upload_mb * 1024 * 1024 + 1)
        if len(data) > svc.settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"файл больше {svc.settings.max_upload_mb} МБ")
        return save_upload(_workspace(session_id), file.filename or "file", data, file.content_type or "")

    @api.get("/sessions/{session_id}/files")
    async def download(session_id: str, path: str, user: User = Depends(auth)):
        await own_session(session_id, user)
        ws = _workspace(session_id).resolve()
        target = (ws / path).resolve()
        if ws not in target.parents or not target.is_file():
            raise HTTPException(404, "нет такого файла")
        return FileResponse(target, filename=target.name)

    # ── OAuth MCP-серверов ───────────────────────────────────────────
    @api.post("/mcp/{name}/connect")
    async def mcp_connect(name: str, user: User = Depends(auth)):
        require_admin(user)
        cfg = svc.mcp.config(name)
        if cfg is None or not cfg.url:
            raise HTTPException(404, f"нет HTTP MCP-сервера {name} в mcp.json")
        try:
            url = await svc.oauth.start(name, cfg.url, OAuthOptions.from_config(cfg.oauth), user_id=user.id)
        except OAuthError as e:
            raise HTTPException(400, str(e)) from None
        return {"url": url, "redirect_uri": svc.oauth.redirect_uri}

    @api.delete("/mcp/{name}/connect")
    async def mcp_disconnect(name: str, user: User = Depends(auth)):
        require_admin(user)
        await svc.oauth.disconnect(name)
        await svc.reload()
        return {"ok": True}

    @app.get("/oauth/callback", response_class=HTMLResponse)
    async def oauth_callback(state: str = "", code: str = "", error: str = "", error_description: str = ""):
        # Без авторизации по токену: сюда возвращает браузер с сервера OAuth.
        # Защита — одноразовый state, выданный только администратору.
        if error:
            return _oauth_page(False, f"{error}: {error_description}")
        try:
            server = await svc.oauth.finish(state, code)
        except OAuthError as e:
            return _oauth_page(False, str(e))
        await svc.reload()
        return _oauth_page(True, f"{server} подключён")

    app.include_router(api)
    for r in extra_routers or []:
        app.include_router(r)

    static = static_dir if static_dir and static_dir.is_dir() else STATIC

    @app.get("/")
    async def index():
        return FileResponse(static / "index.html")

    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.exception_handler(ValueError)
    async def value_error(_, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    return app


def _oauth_page(ok: bool, text: str) -> str:
    # Окно согласия открывалось из чата — сообщаем ему результат и закрываемся.
    payload = json.dumps({"type": "agentkit-oauth", "ok": ok, "text": text}, ensure_ascii=False)
    return f"""<!doctype html><meta charset="utf-8"><title>{'Готово' if ok else 'Ошибка'}</title>
<body style="font:16px system-ui;padding:40px;text-align:center">
<p>{'✓' if ok else '✕'} {html.escape(text)}</p><p style="color:#888">Окно можно закрыть.</p>
<script>try{{window.opener&&window.opener.postMessage({payload},"*");setTimeout(()=>window.close(),1200)}}catch(e){{}}</script>"""


def _check_mode(mode: str | None, user: User) -> str | None:
    if mode is None or mode == "":
        return None
    try:
        m = Mode(mode)
    except ValueError:
        raise HTTPException(400, f"неизвестный режим {mode}") from None
    if m == Mode.BYPASS and not user.has_role("admin"):
        raise HTTPException(403, "режим bypass — только для admin")
    return m.value

