"""AgentService — всё, что нужно агенту-сервису, собранное из настроек.

Проект на шаблоне описывает только своё: промпт, доменные инструменты, хуки,
правила. Хранилище, MCP, skills, права, расписание, трейсы и HTTP API
собираются здесь одинаково для всех агентов.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agentkit.agent import Agent, AgentConfig, AgentSpec, RunOutcome
from agentkit.builder import make_router, standard_tools
from agentkit.bus import Bus, make_bus
from agentkit.events import EventType, RunStatus
from agentkit.hooks import Hooks
from agentkit.llm.router import ModelRouter
from agentkit.mcp import MCPManager, MCPServerConfig
from agentkit.messages import Usage
from agentkit.oauth import OAuthBroker
from agentkit.permissions import Mode, Permissions
from agentkit.runs import Busy, RunManager
from agentkit.scheduler import Scheduler
from agentkit.server.auth import Auth
from agentkit.server.settings import Settings, split_rules
from agentkit.skills import SkillLibrary
from agentkit.store.base import ScheduleRec
from agentkit.store.sql import SQLStore
from agentkit.subagents import DEFAULT_SUBAGENTS, task_tool
from agentkit.tools.base import Tool, User
from agentkit.tools.registry import ToolRegistry
from agentkit.tracing import JSONLTracer, LogTracer

log = logging.getLogger("agentkit.service")

_ENV = re.compile(r"\$\{([A-Z0-9_]+)\}")


def load_mcp_config(path: Path) -> list[MCPServerConfig]:
    """mcp.json: {"servers": {"имя": {"url": "…", "token": "${ENV}", …}}}. ${VAR} берутся из окружения."""
    if not path or not Path(path).is_file():
        return []
    raw = _ENV.sub(lambda m: os.environ.get(m.group(1), ""), Path(path).read_text(encoding="utf-8"))
    data = json.loads(raw)
    out = []
    for name, cfg in (data.get("servers") or {}).items():
        fields = MCPServerConfig.__dataclass_fields__
        out.append(MCPServerConfig(name=name, **{k: v for k, v in cfg.items() if k in fields and k != "name"}))
    return out


@dataclass
class Domain:
    """Предметная часть конкретного агента — единственное, что пишется в новом проекте."""

    spec: AgentSpec
    tools: list[Tool] = field(default_factory=list)
    hooks: Hooks | None = None
    subagents: dict[str, AgentSpec] = field(default_factory=lambda: dict(DEFAULT_SUBAGENTS))
    rules: Callable[[User], str] | str = ""
    deps: dict[str, Any] = field(default_factory=dict)
    memory_global_roles: tuple[str, ...] = ("admin",)
    # Пустой экран чата: что умеет агент и примеры запросов (кнопками).
    description: str = ""
    suggestions: list[str] = field(default_factory=list)
    # Вызывается после старта: подключить БД хоста, прогреть кэш и т. п.
    on_start: Callable[[AgentService], Any] | None = None


class AgentService:
    def __init__(self, settings: Settings, domain: Domain, *, router: ModelRouter | None = None):
        self.settings = settings
        self._router = router  # подмена моделей (тесты, нестандартные провайдеры)
        self.domain = domain
        self.auth = Auth(settings.tokens, settings.service_token)
        self.store: SQLStore | None = None
        self.bus: Bus | None = None
        self.oauth: OAuthBroker | None = None
        self.mcp: MCPManager | None = None
        self.agent: Agent | None = None
        self.runs: RunManager | None = None
        self.scheduler: Scheduler | None = None
        self.registry: ToolRegistry | None = None

    async def start(self) -> None:
        s = self.settings
        if s.workers > 1 and not s.redis_url:
            # Без общей шины каждый процесс думал бы, что он один: два прогона в одной
            # сессии, отмена «мимо», SSE без живых событий. Лучше не стартовать.
            raise RuntimeError("AGENT_WORKERS > 1 требует AGENT_REDIS_URL")
        s.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = await SQLStore(s.database_url).init()
        self.bus = make_bus(s.redis_url)
        await self.bus.start()
        self.bus.on_broadcast(self._on_broadcast)
        self.oauth = OAuthBroker(self.store, s.public_url.rstrip("/") + "/oauth/callback", bus=self.bus,
                                 client_name=s.name)
        self.runs = RunManager(self.store, self.bus)
        self.runs.on_idle = self._drain_inbox
        await self._fail_orphans()
        await self._build_agent()
        if s.scheduler:
            self.scheduler = Scheduler(self.store, lambda name: self.agent, self.user_for, tz=s.tz,
                                       runner=self._scheduled_run)
            self.scheduler.start()
        if self.domain.on_start:
            res = self.domain.on_start(self)
            if hasattr(res, "__await__"):
                await res

    async def _fail_orphans(self) -> None:
        """«running» без живой блокировки — прогон умер вместе с процессом.

        Смотрим именно на блокировку, а не просто на статус: при нескольких
        процессах перезапуск одного не должен ронять прогоны соседей.
        """
        orphans = [r.id for r in await self.store.list_running_runs()
                   if await self.bus.active(r.session_id) != r.id]
        if n := await self.store.fail_runs(orphans, "прогон прерван перезапуском сервиса"):
            log.warning("помечено прерванными прогонов: %d", n)

    async def _on_broadcast(self, msg: dict[str, Any]) -> None:
        if msg.get("cmd") == "reload":
            await self._build_agent()

    async def _build_agent(self) -> None:
        if self.mcp:
            await self.mcp.close()
        self.mcp = MCPManager(load_mcp_config(self.settings.mcp_config), oauth=self.oauth)
        mcp_tools = await self.mcp.connect()
        self.agent = assemble_agent(self.settings, self.domain, self.store, router=self._router,
                                    mcp_tools=mcp_tools, mcp_instructions=self.mcp.instructions)
        self.registry = self.agent.tools
        log.info("агент готов: %d инструментов (MCP: %s)", len(self.registry),
                 ", ".join(f"{k}={'ok' if v.ok else 'ошибка'}" for k, v in self.mcp.status.items()) or "нет")

    async def reload(self) -> None:
        """Перечитать mcp.json и skills без перезапуска — во всех процессах сервиса."""
        await self.bus.broadcast({"cmd": "reload"})

    # ── сообщения ────────────────────────────────────────────────────
    async def send(self, session_id: str, user: User, content: Any, *, model: str | None = None,
                   mode: str | None = None) -> dict[str, Any]:
        """Новое сообщение: запустить прогон или, если агент занят, поставить в очередь входящих.

        Сообщение из очереди агент подхватит между шагами текущего прогона, а
        если тот уже заканчивается — сразу после него (on_idle).
        """
        try:
            run_id = await self.runs.start(
                session_id, lambda: self.agent.run(session_id, user, content, model=model, mode=mode),
                cancel=self.agent.cancel)
            return {"run_id": run_id, "queued": False}
        except Busy:
            pass
        await self.store.inbox_push(session_id, content, _user_dict(user))
        # Прогон мог закончиться между попыткой запуска и записью в очередь — тогда
        # on_idle уже отработал и сообщение никто не заберёт. Проверяем ещё раз.
        if not await self.runs.active_run(session_id):
            await self._drain_inbox(session_id)
        return {"run_id": await self.runs.active_run(session_id), "queued": True}

    async def _drain_inbox(self, session_id: str) -> None:
        items = await self.store.inbox_pop(session_id)
        if not items:
            return
        # Возвращаем в очередь в том же порядке: прогон без input заберёт её сам
        # на первом шаге — с хуками и событием user_message, как и на ходу.
        for it in items:
            await self.store.inbox_push(session_id, it["content"], it["user"])
        user = _user_from(items[-1]["user"])
        try:
            await self.runs.start(session_id, lambda: self.agent.run(session_id, user, None),
                                  cancel=self.agent.cancel)
        except Busy:
            pass  # кто-то успел запустить прогон — он и заберёт очередь

    async def _scheduled_run(self, rec: ScheduleRec, session_id: str, user: User) -> RunOutcome:
        """Запуск по расписанию — через RunManager: виден в чате, его можно остановить."""
        result = await self.send(session_id, user, rec.prompt)
        await self.runs.wait(session_id, timeout=3600)
        run = await self.store.get_run(result["run_id"]) if result.get("run_id") else None
        text = ""
        if run:
            for e in reversed(await self.store.list_events(run_id=run.id)):
                if e.type == EventType.MESSAGE and not e.agent and e.data.get("content"):
                    text = e.data["content"]
                    break
        return RunOutcome(run_id=run.id if run else "", status=RunStatus(run.status) if run else RunStatus.ERROR,
                          text=text, usage=Usage(**(run.usage or {})) if run else Usage(), events=[])

    def user_for(self, user_id: str) -> User:
        for u in self.auth.users.values():
            if u.id == user_id:
                return u
        return User(id=user_id, name=user_id)

    async def stop(self) -> None:
        if self.scheduler:
            await self.scheduler.stop()
        if self.runs:
            await self.runs.shutdown()
        if self.mcp:
            await self.mcp.close()
        if self.bus:
            await self.bus.close()
        if self.store:
            await self.store.close()


def _user_dict(u: User) -> dict[str, Any]:
    return {"id": u.id, "name": u.name, "roles": list(u.roles), "extra": u.extra}


def _user_from(d: dict[str, Any]) -> User:
    return User(id=d["id"], name=d.get("name", ""), roles=d.get("roles") or [], extra=d.get("extra") or {})


def assemble_agent(
    s: Settings,
    domain: Domain,
    store: SQLStore,
    *,
    router: ModelRouter | None = None,
    mcp_tools: list[Tool] | None = None,
    mcp_instructions: Callable[[], str] | None = None,
) -> Agent:
    """Собирает агента из настроек и домена. Один и тот же код для сервиса, evals и тестов."""
    reg = standard_tools(
        files=s.enable_files, shell=s.enable_shell, web_fetch=s.enable_web, tavily_key=s.tavily_api_key,
        memory_global_roles=domain.memory_global_roles, subagents=False,
        extra=[*domain.tools, *(mcp_tools or [])],
    )
    if domain.subagents:
        reg.add(task_tool(domain.subagents))
    spec = replace(domain.spec)
    if s.max_steps and spec.max_steps == AgentSpec().max_steps:
        spec.max_steps = s.max_steps
    if s.reasoning_effort and not spec.reasoning_effort:
        spec.reasoning_effort = s.reasoning_effort
    ws_root = s.workspace_dir or (s.data_dir / "workspaces")
    tracers: list[Any] = [LogTracer()]
    if s.trace_dir:
        tracers.append(JSONLTracer(s.trace_dir))

    def workspace(session_id: str) -> Path:
        p = ws_root / session_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    router = router or make_router(
        openrouter_key=s.openrouter_api_key, yandex_key=s.yandex_api_key, yandex_folder=s.yandex_folder_id,
        openai_key=s.openai_api_key, ollama_url=s.ollama_url, main=s.model_main, small=s.model_small,
        fallbacks=s.fallback_map(), app_name=s.name, app_url=s.public_url,
    )
    return Agent(
        router=router,
        store=store,
        tools=reg,
        spec=spec,
        subagents=domain.subagents,
        permissions=Permissions(mode=Mode(s.default_mode), allow=split_rules(s.allow_rules),
                                ask=split_rules(s.ask_rules), deny=split_rules(s.deny_rules)),
        hooks=domain.hooks,
        skills=SkillLibrary.from_dirs(s.skills_dir),
        mcp_instructions=mcp_instructions,
        config=AgentConfig(context_window=s.context_window, tz=s.tz, max_output_chars=s.max_output_chars,
                           vision_models=[x.strip() for x in s.vision_models.split(",") if x.strip()] or None),
        deps=domain.deps,
        workspace=workspace if s.enable_files or s.enable_shell else None,
        rules=domain.rules,
        tracers=tracers,
    )
