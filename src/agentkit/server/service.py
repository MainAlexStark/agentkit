"""AgentService — всё, что нужно агенту-сервису, собранное из настроек.

Проект на шаблоне описывает только своё: промпт, доменные инструменты, хуки,
правила. Хранилище, MCP, skills, права, расписание, трейсы и HTTP API
собираются здесь одинаково для всех агентов.
"""

from __future__ import annotations

import asyncio
import fnmatch
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
from agentkit.devices import DeviceHub, device_tools
from agentkit.events import EventType, RunStatus
from agentkit.hooks import Hooks
from agentkit.llm.catalog import ModelCatalog
from agentkit.llm.router import ModelRouter
from agentkit.mcp import MCPManager, MCPServerConfig
from agentkit.messages import Usage
from agentkit.oauth import OAuthBroker
from agentkit.permissions import Mode, Permissions
from agentkit.projects import ProjectLibrary, project_tools
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
from agentkit.tools.sandbox import sandbox_tools
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


MCP_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def mcp_from_dict(name: str, d: dict[str, Any]) -> MCPServerConfig:
    """Сервер, добавленный в настройках чата (хранится в базе, не в mcp.json).

    Из интерфейса — только HTTP: stdio-сервер — это запуск команды на машине
    сервиса, такое добавляется только руками в mcp.json.
    """
    auth = d.get("auth", "none")
    return MCPServerConfig(
        name=name,
        url=d["url"],
        token=(d.get("token") or None) if auth == "token" else None,
        headers=dict(d.get("headers") or {}),
        enabled=bool(d.get("enabled", True)),
        deferred=d.get("deferred"),
        oauth=True if auth == "oauth" else None,
    )


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
    # Подписка пользователя: (user, расход за месяц) → {"name", "status", "renews_at", "limit_usd",
    # "features": [...], "manage_url"}. None — из настроек AGENT_PLAN_NAME / AGENT_MONTHLY_BUDGET_USD.
    plan: Callable[[User, dict[str, Any]], Any] | None = None
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
        self.catalog: ModelCatalog | None = None
        self.devices: DeviceHub | None = None
        self.projects: ProjectLibrary | None = None
        self.mcp_all: list[MCPServerConfig] = []  # вместе с выключенными — для списка в настройках
        self.mcp_file_names: set[str] = set()

    async def start(self) -> None:
        s = self.settings
        if s.workers > 1 and not s.redis_url:
            # Без общей шины каждый процесс думал бы, что он один: два прогона в одной
            # сессии, отмена «мимо», SSE без живых событий. Лучше не стартовать.
            raise RuntimeError("AGENT_WORKERS > 1 требует AGENT_REDIS_URL")
        s.data_dir.mkdir(parents=True, exist_ok=True)
        # Папка рабочих пространств нужна до первого разговора: песочница монтирует её при старте.
        (s.workspace_dir or s.data_dir / "workspaces").mkdir(parents=True, exist_ok=True)
        self.store = await SQLStore(s.database_url).init()
        self.bus = make_bus(s.redis_url)
        await self.bus.start()
        self.bus.on_broadcast(self._on_broadcast)
        self.oauth = OAuthBroker(self.store, s.public_url.rstrip("/") + "/oauth/callback", bus=self.bus,
                                 client_name=s.name)
        self.devices = DeviceHub(self.store, self.bus) if s.enable_devices else None
        self.projects = (ProjectLibrary(self.store, s.data_dir / "projects", inline_limit=s.project_inline_chars)
                         if s.enable_projects else None)
        self.runs = RunManager(self.store, self.bus)
        self.runs.on_idle = self._drain_inbox
        await self._fail_orphans()
        await self._build_agent()
        # Планировщик создаётся всегда — «Запустить сейчас» работает и при выключенном
        # AGENT_SCHEDULER; тикать по расписанию он начинает только если включён.
        self.scheduler = Scheduler(self.store, lambda name: self.agent, self.user_for, tz=s.tz,
                                   runner=self._scheduled_run)
        if s.scheduler:
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
        self.mcp_all = await self.mcp_configs()
        self.mcp = MCPManager(self.mcp_all, oauth=self.oauth)
        mcp_tools = await self.mcp.connect()
        self.agent = assemble_agent(self.settings, self.domain, self.store, router=self._router,
                                    mcp_tools=mcp_tools, mcp_instructions=self.mcp.instructions,
                                    extra_tools=self._platform_tools())
        if self.projects:
            self.agent.context_providers.append(self.projects.context)
        if self.devices:
            self.agent.context_providers.append(self.devices.context)
        self.registry = self.agent.tools
        s = self.settings
        if self.catalog is None:
            self.catalog = ModelCatalog(self.agent.router, configured=[s.model_main, s.model_small, *s.model_list()])
            # Каталог — сетевой запрос на секунды; старт сервиса его не ждёт.
            self._catalog_task = asyncio.create_task(self.catalog.refresh())
        else:
            self.catalog.router = self.agent.router
        self.agent.vision_for = self.catalog.vision
        log.info("агент готов: %d инструментов (MCP: %s)", len(self.registry),
                 ", ".join(f"{k}={'ok' if v.ok else 'ошибка'}" for k, v in self.mcp.status.items()) or "нет")

    def _platform_tools(self) -> list[Tool]:
        s = self.settings
        out: list[Tool] = []
        if self.projects:
            out += project_tools(self.projects)
        if self.devices:
            out += device_tools(self.devices)
        if s.sandbox_url:
            out += sandbox_tools(s.sandbox_url, s.sandbox_token)
        return out

    # ── MCP-серверы: из mcp.json и добавленные в настройках ──────────
    async def mcp_custom(self) -> dict[str, dict[str, Any]]:
        return await self.store.kv_get("config", "mcp_servers") or {}

    async def mcp_configs(self) -> list[MCPServerConfig]:
        """mcp.json главнее: сервер с тем же именем из базы игнорируется (его нельзя «перебить» из UI)."""
        file = load_mcp_config(self.settings.mcp_config)
        self.mcp_file_names = {c.name for c in file}
        custom = [mcp_from_dict(n, d) for n, d in (await self.mcp_custom()).items() if n not in self.mcp_file_names]
        return [*file, *custom]

    async def save_mcp_custom(self, servers: dict[str, dict[str, Any]]) -> None:
        await self.store.kv_set("config", "mcp_servers", servers)
        await self.reload()

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

    # ── модели: избранное пользователя и список доступных ────────────
    async def model_prefs(self, user: User) -> dict[str, Any]:
        """Избранные модели, «показывать только избранные» и модель по умолчанию — у каждого свои."""
        p = await self.store.kv_get("model_prefs", user.id) or {}
        return {"favorites": p.get("favorites", self.settings.model_list()),
                "only_favorites": bool(p.get("only_favorites", False)),
                "default": p.get("default") or self.settings.model_main}

    async def model_policy(self) -> list[str] | None:
        """Список, который админ оставил в настройках; None — действует AGENT_MODELS_ALLOW."""
        v = await self.store.kv_get("config", "models_allowed")
        return v.get("ids") if isinstance(v, dict) else None

    def model_allowed(self, model_id: str, policy: list[str] | None) -> bool:
        s = self.settings
        mid = self.agent.router.resolve(model_id)
        # Основную и малую модели не отключить: на них работают сжатие истории, субагенты и расписание.
        if mid in (self.agent.router.resolve("main"), self.agent.router.resolve("small"), s.model_main, s.model_small):
            return True
        if policy is not None:
            return mid in policy
        patterns = [x.strip() for x in s.models_allow.split(",") if x.strip()]
        return not patterns or any(fnmatch.fnmatchcase(mid, p) for p in patterns)

    async def check_model(self, model: str | None) -> str | None:
        """Модель из запроса пользователя: провайдер должен существовать, а модель — быть разрешена.

        Проверка на сервере, а не только в выпадающем списке: иначе любой мог бы прислать
        в API самую дорогую модель каталога.
        """
        if not model:
            return None
        from agentkit.llm.router import split_model

        mid = self.agent.router.resolve(model)
        provider, _ = split_model(mid, self.agent.router.default_provider)
        if provider not in self.agent.router.providers or not self.model_allowed(mid, await self.model_policy()):
            raise ValueError(f"модель {model} недоступна — выберите другую в списке моделей")
        return model

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
    extra_tools: list[Tool] | None = None,
) -> Agent:
    """Собирает агента из настроек и домена. Один и тот же код для сервиса, evals и тестов."""
    reg = standard_tools(
        files=s.enable_files, shell=s.enable_shell, web_fetch=s.enable_web, tavily_key=s.tavily_api_key,
        memory_global_roles=domain.memory_global_roles, subagents=False,
        extra=[*domain.tools, *(extra_tools or []), *(mcp_tools or [])],
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
