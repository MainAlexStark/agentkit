"""SQL-хранилище на SQLAlchemy Core: SQLite (разработка, тесты) и PostgreSQL (прод).

Схема создаётся сама (create_all). Миграции — когда схема начнёт меняться у
развёрнутых сервисов: тогда подключить Alembic к этому же metadata.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from agentkit.events import Event, EventType
from agentkit.messages import Message, new_id
from agentkit.store.base import (
    ApprovalRec,
    MemoryRec,
    ProjectFileRec,
    ProjectRec,
    RunRec,
    ScheduleRec,
    SessionRec,
    Store,
)

md = MetaData()

sessions = Table(
    "ak_sessions", md,
    Column("id", String(40), primary_key=True),
    Column("user_id", String(120), index=True, nullable=False),
    Column("title", String(300), default=""),
    Column("agent", String(80), default="main"),
    Column("parent_id", String(40), ForeignKey("ak_sessions.id", ondelete="CASCADE"), nullable=True),
    Column("state", JSON, default=dict),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("project_id", String(40), nullable=True, index=True),
)

projects = Table(
    "ak_projects", md,
    Column("id", String(40), primary_key=True),
    Column("owner_id", String(120), index=True, nullable=False),
    Column("name", String(200), nullable=False),
    Column("description", Text, default=""),
    Column("instructions", Text, default=""),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)

# Доступ к проекту: user_id="*" — всем пользователям сервиса. role: viewer | editor.
project_members = Table(
    "ak_project_members", md,
    Column("project_id", String(40), ForeignKey("ak_projects.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", String(120), primary_key=True),
    Column("role", String(20), nullable=False),
    Column("added_at", Float, nullable=False),
)

project_files = Table(
    "ak_project_files", md,
    Column("id", String(40), primary_key=True),
    Column("project_id", String(40), ForeignKey("ak_projects.id", ondelete="CASCADE"), index=True),
    Column("name", String(300), nullable=False),
    Column("path", String(500), nullable=False),
    Column("size", Integer, default=0),
    Column("kind", String(20), default="file"),
    Column("text_chars", Integer, default=0),
    Column("added_by", String(120), default=""),
    Column("created_at", Float, nullable=False),
)

messages = Table(
    "ak_messages", md,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String(40), ForeignKey("ak_sessions.id", ondelete="CASCADE"), nullable=False),
    Column("run_id", String(40), nullable=True),
    Column("data", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
    Index("ix_ak_messages_session_seq", "session_id", "seq"),
)

runs = Table(
    "ak_runs", md,
    Column("id", String(40), primary_key=True),
    Column("session_id", String(40), ForeignKey("ak_sessions.id", ondelete="CASCADE"), index=True),
    Column("status", String(30), nullable=False),
    Column("model", String(200), default=""),
    Column("steps", Integer, default=0),
    Column("usage", JSON, default=dict),
    Column("error", Text, nullable=True),
    Column("trigger", String(20), default="user"),
    Column("started_at", Float, nullable=False),
    Column("finished_at", Float, nullable=True),
)

events = Table(
    "ak_events", md,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(40), index=True),
    Column("session_id", String(40), index=True),
    Column("type", String(40), nullable=False),
    Column("agent", String(80), nullable=True),
    Column("data", JSON, nullable=False),
    Column("ts", Float, nullable=False),
)

approvals = Table(
    "ak_approvals", md,
    Column("id", String(40), primary_key=True),
    Column("session_id", String(40), ForeignKey("ak_sessions.id", ondelete="CASCADE"), index=True),
    Column("run_id", String(40), index=True),
    Column("tool_call_id", String(80), nullable=False),
    Column("tool_name", String(120), nullable=False),
    Column("args", JSON, nullable=False),
    Column("view", JSON, default=list),
    Column("reason", Text, default=""),
    Column("status", String(20), default="pending"),
    Column("decided_by", String(120), nullable=True),
    Column("decided_at", Float, nullable=True),
    Column("comment", Text, default=""),
    Column("created_at", Float, nullable=False),
)

memory = Table(
    "ak_memory", md,
    Column("id", String(40), primary_key=True),
    Column("scope", String(160), index=True, nullable=False),
    Column("text", Text, nullable=False),
    Column("created_by", String(120), default=""),
    Column("created_at", Float, nullable=False),
)

blobs = Table(
    "ak_blobs", md,
    Column("id", String(40), primary_key=True),
    Column("session_id", String(40), index=True),
    Column("kind", String(40), nullable=False),
    Column("content", Text, nullable=False),
    Column("meta", JSON, default=dict),
    Column("created_at", Float, nullable=False),
)

schedules = Table(
    "ak_schedules", md,
    Column("id", String(40), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("cron", String(120), nullable=False),
    Column("prompt", Text, nullable=False),
    Column("user_id", String(120), nullable=False, index=True),
    Column("agent", String(80), default="main"),
    Column("enabled", Boolean, default=True),
    Column("session_id", String(40), nullable=True),
    Column("last_run_at", Float, nullable=True),
    Column("next_run_at", Float, nullable=True),
)

# Сообщения, присланные, пока агент работал: агент подхватывает их между шагами.
inbox = Table(
    "ak_inbox", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String(40), ForeignKey("ak_sessions.id", ondelete="CASCADE"), index=True),
    Column("content", JSON, nullable=False),
    Column("user", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
)

# Ключ-значение для служебного состояния (OAuth-клиенты и токены MCP-серверов и т. п.).
kv = Table(
    "ak_kv", md,
    Column("ns", String(60), primary_key=True),
    Column("key", String(200), primary_key=True),
    Column("value", JSON, nullable=False),
    Column("updated_at", Float, nullable=False),
)


def _add_missing_columns(conn: Any) -> None:
    """create_all не добавляет колонки в уже существующие таблицы — досоздаём новые.

    Без этого у развёрнутого сервиса (база от прошлой версии) обновление падало бы
    на первом запросе к ak_sessions.project_id. Только добавление nullable-колонок;
    переименования и удаления — уже Alembic.
    """
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(conn)
    for table in md.sorted_tables:
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in have and col.nullable:
                ddl = col.type.compile(dialect=conn.dialect)
                conn.exec_driver_sql(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}')


def _url(url: str) -> str:
    # Привычные DSN «postgres://…» из docker-окружения → асинхронный драйвер.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        url = url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


class SQLStore(Store):
    def __init__(self, url: str = "sqlite+aiosqlite:///:memory:", engine: AsyncEngine | None = None):
        url = _url(url)
        if engine is None:
            kw: dict[str, Any] = {}
            if ":memory:" in url:
                # Одна общая in-memory база на все соединения — иначе каждое видит пустую.
                kw = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
            engine = create_async_engine(url, **kw)
        self.engine = engine

    async def init(self) -> SQLStore:
        async with self.engine.begin() as c:
            if self.engine.dialect.name == "sqlite":
                await c.exec_driver_sql("PRAGMA foreign_keys=ON")
            await c.run_sync(md.create_all)
            await c.run_sync(_add_missing_columns)
        return self

    async def close(self) -> None:
        await self.engine.dispose()

    # ── сессии ───────────────────────────────────────────────────────
    async def create_session(self, user_id, *, title="", agent="main", parent_id=None, state=None, project_id=None):
        now = time.time()
        rec = SessionRec(id=new_id("s_"), user_id=user_id, title=title, agent=agent, parent_id=parent_id,
                         state=state or {}, created_at=now, updated_at=now, project_id=project_id)
        async with self.engine.begin() as c:
            await c.execute(insert(sessions).values(**rec.__dict__))
        return rec

    async def get_session(self, session_id):
        async with self.engine.connect() as c:
            r = (await c.execute(select(sessions).where(sessions.c.id == session_id))).mappings().first()
        return SessionRec(**r) if r else None

    async def list_sessions(self, user_id, *, limit=50, include_children=False, project_id=None):
        q = select(sessions).where(sessions.c.user_id == user_id)
        if project_id:
            q = q.where(sessions.c.project_id == project_id)
        if not include_children:
            q = q.where(sessions.c.parent_id.is_(None))
        q = q.order_by(sessions.c.updated_at.desc()).limit(limit)
        async with self.engine.connect() as c:
            return [SessionRec(**r) for r in (await c.execute(q)).mappings()]

    async def update_session(self, session_id, **fields):
        fields.setdefault("updated_at", time.time())
        async with self.engine.begin() as c:
            await c.execute(update(sessions).where(sessions.c.id == session_id).values(**fields))

    async def delete_session(self, session_id):
        async with self.engine.begin() as c:
            if self.engine.dialect.name == "sqlite":
                await c.exec_driver_sql("PRAGMA foreign_keys=ON")
            for t in (messages, runs, approvals, inbox):
                await c.execute(delete(t).where(t.c.session_id == session_id))
            await c.execute(delete(events).where(events.c.session_id == session_id))
            await c.execute(delete(blobs).where(blobs.c.session_id == session_id))
            await c.execute(delete(sessions).where(sessions.c.parent_id == session_id))
            await c.execute(delete(sessions).where(sessions.c.id == session_id))

    async def get_state(self, session_id):
        s = await self.get_session(session_id)
        return dict(s.state or {}) if s else {}

    async def set_state(self, session_id, state):
        async with self.engine.begin() as c:
            await c.execute(update(sessions).where(sessions.c.id == session_id).values(state=state))

    # ── сообщения ────────────────────────────────────────────────────
    async def append_message(self, session_id, msg, run_id=None):
        now = time.time()
        async with self.engine.begin() as c:
            r = await c.execute(insert(messages).values(session_id=session_id, run_id=run_id,
                                                        data=msg.to_dict(), created_at=now))
            await c.execute(update(sessions).where(sessions.c.id == session_id).values(updated_at=now))
            return int(r.inserted_primary_key[0])

    async def list_messages(self, session_id, *, after_seq=0):
        q = (select(messages.c.seq, messages.c.data)
             .where(messages.c.session_id == session_id, messages.c.seq > after_seq)
             .order_by(messages.c.seq))
        async with self.engine.connect() as c:
            return [(r.seq, Message.from_dict(r.data)) for r in await c.execute(q)]

    async def message_times(self, session_id):
        q = select(messages.c.seq, messages.c.created_at).where(messages.c.session_id == session_id)
        async with self.engine.connect() as c:
            return {r.seq: r.created_at for r in await c.execute(q)}

    async def delete_messages_after(self, session_id, seq):
        async with self.engine.begin() as c:
            r = await c.execute(delete(messages).where(messages.c.session_id == session_id, messages.c.seq > seq))
            return r.rowcount or 0

    # ── прогоны ──────────────────────────────────────────────────────
    async def create_run(self, session_id, *, model="", trigger="user"):
        rec = RunRec(id=new_id("r_"), session_id=session_id, status="running", model=model,
                     started_at=time.time(), trigger=trigger)
        async with self.engine.begin() as c:
            await c.execute(insert(runs).values(**rec.__dict__))
        return rec

    async def get_run(self, run_id):
        async with self.engine.connect() as c:
            r = (await c.execute(select(runs).where(runs.c.id == run_id))).mappings().first()
        return RunRec(**r) if r else None

    async def update_run(self, run_id, **fields):
        async with self.engine.begin() as c:
            await c.execute(update(runs).where(runs.c.id == run_id).values(**fields))

    async def fail_stale_runs(self, reason: str = "прогон прерван перезапуском сервиса") -> int:
        """После перезапуска процесса «running» в базе — сироты: их задачи умерли вместе с процессом."""
        async with self.engine.begin() as c:
            r = await c.execute(update(runs).where(runs.c.status == "running")
                                .values(status="error", error=reason, finished_at=time.time()))
            return r.rowcount or 0

    async def list_running_runs(self):
        async with self.engine.connect() as c:
            return [RunRec(**r) for r in (await c.execute(select(runs).where(runs.c.status == "running"))).mappings()]

    async def fail_runs(self, run_ids, reason: str) -> int:
        if not run_ids:
            return 0
        async with self.engine.begin() as c:
            r = await c.execute(update(runs).where(runs.c.id.in_(run_ids), runs.c.status == "running")
                                .values(status="error", error=reason, finished_at=time.time()))
            return r.rowcount or 0

    async def list_runs(self, session_id, *, limit=50):
        q = select(runs).where(runs.c.session_id == session_id).order_by(runs.c.started_at.desc()).limit(limit)
        async with self.engine.connect() as c:
            return [RunRec(**r) for r in (await c.execute(q)).mappings()]

    async def add_event(self, ev):
        async with self.engine.begin() as c:
            r = await c.execute(insert(events).values(run_id=ev.run_id, session_id=ev.session_id, type=str(ev.type),
                                                      agent=ev.agent, data=ev.data, ts=ev.ts))
            ev.seq = int(r.inserted_primary_key[0])
            return ev.seq

    async def last_event_seq(self, session_id):
        async with self.engine.connect() as c:
            v = (await c.execute(select(func.max(events.c.seq)).where(events.c.session_id == session_id))).scalar()
        return int(v or 0)

    async def list_events(self, *, run_id=None, session_id=None, after_seq=0, limit=1000):
        q = select(events).where(events.c.seq > after_seq)
        if run_id:
            q = q.where(events.c.run_id == run_id)
        if session_id:
            q = q.where(events.c.session_id == session_id)
        q = q.order_by(events.c.seq).limit(limit)
        async with self.engine.connect() as c:
            return [Event(type=EventType(r["type"]), data=r["data"], run_id=r["run_id"], session_id=r["session_id"],
                          agent=r["agent"], ts=r["ts"], seq=r["seq"]) for r in (await c.execute(q)).mappings()]

    # ── подтверждения ────────────────────────────────────────────────
    async def create_approval(self, rec):
        rec.created_at = rec.created_at or time.time()
        async with self.engine.begin() as c:
            await c.execute(insert(approvals).values(**rec.__dict__))
        return rec

    async def get_approval(self, approval_id):
        async with self.engine.connect() as c:
            r = (await c.execute(select(approvals).where(approvals.c.id == approval_id))).mappings().first()
        return ApprovalRec(**r) if r else None

    async def list_approvals(self, session_id, *, status=None, run_id=None):
        q = select(approvals).where(approvals.c.session_id == session_id)
        if status:
            q = q.where(approvals.c.status == status)
        if run_id:
            q = q.where(approvals.c.run_id == run_id)
        async with self.engine.connect() as c:
            return [ApprovalRec(**r) for r in (await c.execute(q.order_by(approvals.c.created_at))).mappings()]

    async def claim_approval(self, approval_id):
        """pending → resolving одним UPDATE: двойной клик или два процесса не выполнят действие дважды."""
        async with self.engine.begin() as c:
            r = await c.execute(update(approvals).where(approvals.c.id == approval_id, approvals.c.status == "pending")
                                .values(status="resolving"))
            return bool(r.rowcount)

    async def update_approval(self, approval_id, **fields):
        async with self.engine.begin() as c:
            await c.execute(update(approvals).where(approvals.c.id == approval_id).values(**fields))

    # ── память ───────────────────────────────────────────────────────
    async def memory_add(self, scope, text, *, created_by=""):
        rec = MemoryRec(id=new_id("m_"), scope=scope, text=text, created_by=created_by, created_at=time.time())
        async with self.engine.begin() as c:
            await c.execute(insert(memory).values(**rec.__dict__))
        return rec

    async def memory_list(self, scopes):
        q = select(memory).where(memory.c.scope.in_(scopes)).order_by(memory.c.created_at)
        async with self.engine.connect() as c:
            return [MemoryRec(**r) for r in (await c.execute(q)).mappings()]

    async def memory_delete(self, memory_id):
        async with self.engine.begin() as c:
            return bool((await c.execute(delete(memory).where(memory.c.id == memory_id))).rowcount)

    async def memory_update(self, memory_id, text):
        async with self.engine.begin() as c:
            return bool((await c.execute(update(memory).where(memory.c.id == memory_id).values(text=text))).rowcount)

    # ── blobs ────────────────────────────────────────────────────────
    async def blob_put(self, session_id, kind, content, *, meta=None):
        bid = new_id("b_")
        async with self.engine.begin() as c:
            await c.execute(insert(blobs).values(id=bid, session_id=session_id, kind=kind, content=content,
                                                 meta=meta or {}, created_at=time.time()))
        return bid

    async def blob_get(self, blob_id):
        async with self.engine.connect() as c:
            r = (await c.execute(select(blobs.c.content, blobs.c.meta).where(blobs.c.id == blob_id))).first()
        return (r.content, r.meta or {}) if r else None

    async def blob_list(self, session_id, kind):
        q = select(blobs.c.id, blobs.c.meta).where(blobs.c.session_id == session_id, blobs.c.kind == kind)
        async with self.engine.connect() as c:
            return [(r.id, r.meta or {}) for r in await c.execute(q.order_by(blobs.c.created_at))]

    # ── расписание ───────────────────────────────────────────────────
    async def claim_schedule(self, schedule_id, expected_next, new_next, now):
        """Запуск достаётся одному процессу: сдвигаем next_run_at, только если он не сдвинут другим."""
        async with self.engine.begin() as c:
            r = await c.execute(update(schedules)
                                .where(schedules.c.id == schedule_id, schedules.c.next_run_at == expected_next)
                                .values(next_run_at=new_next, last_run_at=now))
            return bool(r.rowcount)

    # ── входящие во время прогона ────────────────────────────────────
    async def inbox_push(self, session_id, content, user):
        async with self.engine.begin() as c:
            r = await c.execute(insert(inbox).values(session_id=session_id, content=content, user=user,
                                                     created_at=time.time()))
            return int(r.inserted_primary_key[0])

    async def inbox_pop(self, session_id):
        async with self.engine.begin() as c:
            rows = (await c.execute(select(inbox).where(inbox.c.session_id == session_id)
                                    .order_by(inbox.c.id))).mappings().all()
            if not rows:
                return []
            # Удаляем ровно прочитанные id: сообщение, пришедшее между SELECT и DELETE, не теряется.
            await c.execute(delete(inbox).where(inbox.c.id.in_([r["id"] for r in rows])))
            return [{"content": r["content"], "user": r["user"], "created_at": r["created_at"]} for r in rows]

    async def inbox_count(self, session_id):
        async with self.engine.connect() as c:
            return int((await c.execute(select(func.count()).select_from(inbox)
                                        .where(inbox.c.session_id == session_id))).scalar_one())

    # ── ключ-значение ────────────────────────────────────────────────
    async def kv_get(self, ns, key):
        async with self.engine.connect() as c:
            r = (await c.execute(select(kv.c.value).where(kv.c.ns == ns, kv.c.key == key))).first()
        return r.value if r else None

    async def kv_set(self, ns, key, value):
        async with self.engine.begin() as c:
            exists = (await c.execute(select(kv.c.key).where(kv.c.ns == ns, kv.c.key == key))).first()
            if exists:
                await c.execute(update(kv).where(kv.c.ns == ns, kv.c.key == key)
                                .values(value=value, updated_at=time.time()))
            else:
                await c.execute(insert(kv).values(ns=ns, key=key, value=value, updated_at=time.time()))

    async def kv_list(self, ns):
        async with self.engine.connect() as c:
            return {r.key: r.value for r in await c.execute(select(kv.c.key, kv.c.value).where(kv.c.ns == ns))}

    async def known_user_ids(self):
        async with self.engine.connect() as c:
            rows = await c.execute(select(sessions.c.user_id).distinct())
            return sorted({r.user_id for r in rows})

    # ── проекты ──────────────────────────────────────────────────────
    async def project_create(self, owner_id, name, *, description="", instructions=""):
        now = time.time()
        rec = ProjectRec(id=new_id("p_"), owner_id=owner_id, name=name, description=description,
                         instructions=instructions, created_at=now, updated_at=now)
        async with self.engine.begin() as c:
            await c.execute(insert(projects).values(**rec.__dict__))
        return rec

    async def project_get(self, project_id):
        async with self.engine.connect() as c:
            r = (await c.execute(select(projects).where(projects.c.id == project_id))).mappings().first()
        return ProjectRec(**r) if r else None

    async def project_update(self, project_id, **fields):
        fields.setdefault("updated_at", time.time())
        async with self.engine.begin() as c:
            await c.execute(update(projects).where(projects.c.id == project_id).values(**fields))

    async def project_delete(self, project_id):
        async with self.engine.begin() as c:
            if self.engine.dialect.name == "sqlite":
                await c.exec_driver_sql("PRAGMA foreign_keys=ON")
            await c.execute(delete(project_files).where(project_files.c.project_id == project_id))
            await c.execute(delete(project_members).where(project_members.c.project_id == project_id))
            # Разговоры проекта остаются у людей — просто без проекта.
            await c.execute(update(sessions).where(sessions.c.project_id == project_id).values(project_id=None))
            await c.execute(delete(memory).where(memory.c.scope == f"project:{project_id}"))
            await c.execute(delete(projects).where(projects.c.id == project_id))

    async def project_list_for(self, user_id):
        """Свои, открытые лично и открытые всем — с ролью: owner | editor | viewer."""
        async with self.engine.connect() as c:
            own = (await c.execute(select(projects).where(projects.c.owner_id == user_id))).mappings().all()
            shared = (await c.execute(
                select(projects, project_members.c.role, project_members.c.user_id.label("member"))
                .join(project_members, project_members.c.project_id == projects.c.id)
                .where(project_members.c.user_id.in_([user_id, "*"])))).mappings().all()
        out: dict[str, tuple[ProjectRec, str]] = {r["id"]: (ProjectRec(**r), "owner") for r in own}
        rank = {"viewer": 1, "editor": 2, "owner": 3}
        for r in shared:
            rec = ProjectRec(**{k: r[k] for k in ProjectRec.__dataclass_fields__})
            prev = out.get(rec.id)
            if prev is None or rank[r["role"]] > rank[prev[1]]:
                out[rec.id] = (rec, r["role"])
        return sorted(out.values(), key=lambda x: -x[0].updated_at)

    async def project_role(self, project_id, user_id):
        p = await self.project_get(project_id)
        if p is None:
            return None, None
        if p.owner_id == user_id:
            return p, "owner"
        async with self.engine.connect() as c:
            rows = (await c.execute(select(project_members.c.role).where(
                project_members.c.project_id == project_id, project_members.c.user_id.in_([user_id, "*"])))).all()
        roles = {r.role for r in rows}
        return p, ("editor" if "editor" in roles else "viewer" if "viewer" in roles else None)

    async def project_members_list(self, project_id):
        async with self.engine.connect() as c:
            rows = await c.execute(select(project_members).where(project_members.c.project_id == project_id)
                                   .order_by(project_members.c.added_at))
            return [dict(r) for r in rows.mappings()]

    async def project_member_set(self, project_id, user_id, role):
        async with self.engine.begin() as c:
            await c.execute(delete(project_members).where(project_members.c.project_id == project_id,
                                                          project_members.c.user_id == user_id))
            if role:
                await c.execute(insert(project_members).values(project_id=project_id, user_id=user_id, role=role,
                                                               added_at=time.time()))

    async def project_file_add(self, rec):
        async with self.engine.begin() as c:
            await c.execute(insert(project_files).values(**rec.__dict__))
            await c.execute(update(projects).where(projects.c.id == rec.project_id).values(updated_at=time.time()))
        return rec

    async def project_files_list(self, project_id):
        async with self.engine.connect() as c:
            rows = await c.execute(select(project_files).where(project_files.c.project_id == project_id)
                                   .order_by(project_files.c.created_at))
            return [ProjectFileRec(**r) for r in rows.mappings()]

    async def project_file_delete(self, project_id, file_id):
        async with self.engine.begin() as c:
            res = await c.execute(delete(project_files).where(project_files.c.project_id == project_id,
                                                              project_files.c.id == file_id))
            return res.rowcount > 0

    async def kv_delete(self, ns, key):
        async with self.engine.begin() as c:
            await c.execute(delete(kv).where(kv.c.ns == ns, kv.c.key == key))

    async def schedule_upsert(self, rec):
        async with self.engine.begin() as c:
            exists = (await c.execute(select(schedules.c.id).where(schedules.c.id == rec.id))).first()
            if exists:
                await c.execute(update(schedules).where(schedules.c.id == rec.id).values(**rec.__dict__))
            else:
                await c.execute(insert(schedules).values(**rec.__dict__))
        return rec

    async def schedule_list(self, *, user_id=None):
        q = select(schedules)
        if user_id:
            q = q.where(schedules.c.user_id == user_id)
        async with self.engine.connect() as c:
            return [ScheduleRec(**r) for r in (await c.execute(q.order_by(schedules.c.name))).mappings()]

    async def schedule_delete(self, schedule_id):
        async with self.engine.begin() as c:
            await c.execute(delete(schedules).where(schedules.c.id == schedule_id))

    async def usage_summary(self, *, user_id=None, since=0):
        q = select(runs.c.usage, runs.c.model).where(runs.c.started_at >= since)
        if user_id:
            q = q.join(sessions, sessions.c.id == runs.c.session_id).where(sessions.c.user_id == user_id)
        total: dict[str, float] = {"runs": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cost": 0.0}
        by_model: dict[str, float] = {}
        async with self.engine.connect() as c:
            for r in await c.execute(q):
                u = r.usage or {}
                total["runs"] += 1
                for k in ("input_tokens", "output_tokens", "cached_tokens", "cost"):
                    total[k] += u.get(k, 0) or 0
                by_model[r.model or "?"] = by_model.get(r.model or "?", 0) + (u.get("cost", 0) or 0)
        total["cost"] = round(total["cost"], 6)
        return {**total, "cost_by_model": by_model}


async def count_rows(store: SQLStore, table: Table) -> int:
    async with store.engine.connect() as c:
        return int((await c.execute(select(func.count()).select_from(table))).scalar_one())
