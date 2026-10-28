"""Агентный цикл.

    модель → вызовы инструментов → права/хуки → выполнение → результаты → модель …

Ключевые решения:

* Прогон, которому нужно подтверждение человека, ОСТАНАВЛИВАЕТСЯ (статус
  waiting_approval), а не ждёт в памяти. Решение человека продолжает работу
  новым прогоном. Так ожидание переживает перезапуск сервиса и может длиться
  сколько угодно — человек ответит завтра с телефона.
* Всё состояние — в Store. Агент без состояния между вызовами: один объект
  обслуживает все сессии, его можно держать в любом количестве процессов.
* Ошибка инструмента — не исключение цикла, а текст для модели: «Ошибка: …».
  Модель исправляет аргументы сама.
* Последний разрешённый шаг идёт без инструментов: модель обязана подвести
  итог, а не оборваться на полуслове.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agentkit.attachments import expand_parts, is_vision
from agentkit.context import (
    PromptParts,
    build_system_prompt,
    compact,
    effective_history,
    estimate_tokens,
    truncate_text,
)
from agentkit.events import Event, EventType, RunStatus
from agentkit.hooks import HookEvent, Hooks, fold
from agentkit.llm.base import LLMError, LLMRequest
from agentkit.llm.router import ModelRouter
from agentkit.messages import Message, ToolCall, Usage, new_id
from agentkit.permissions import Decision, Mode, Permissions
from agentkit.skills import SkillLibrary
from agentkit.store.base import ApprovalRec, Store
from agentkit.tools.base import Tool, ToolArgsError, ToolContext, ToolDenied, ToolResult, User
from agentkit.tools.registry import LOADED_KEY, ToolRegistry, make_tool_search

log = logging.getLogger("agentkit.agent")

EPHEMERAL = {EventType.TEXT_DELTA, EventType.THINKING_DELTA}


@dataclass
class AgentSpec:
    """Описание агента: главного или субагента (explore, plan, …)."""

    name: str = "main"
    description: str = ""  # для списка субагентов в инструменте task
    prompt: str = ""
    model: str = "main"  # роль из ModelRouter.roles или «провайдер:модель»
    tools: list[str] | None = None  # шаблоны разрешённых инструментов; None — все
    deny_tools: list[str] = field(default_factory=list)
    max_steps: int = 30
    mode: Mode | None = None  # принудительный режим прав (explore → readonly)
    reasoning_effort: str | None = None
    temperature: float | None = None
    max_tokens: int | None = 8000
    include_core_rules: bool = True


@dataclass
class AgentConfig:
    max_output_chars: int = 30_000  # длиннее — обрезка + полный текст в blob, читается read_output
    context_window: int = 128_000
    compact_threshold: float = 0.75
    keep_user_turns: int = 2
    compact_model: str = "small"
    tz: str = "Europe/Moscow"
    loop_guard: int = 3  # одинаковый вызов N раз подряд → предупреждение модели
    max_stop_continues: int = 3  # сколько раз stop-хук может не дать закончить
    persist_thinking: bool = False
    vision_models: list[str] | None = None  # шаблоны моделей, которым отдавать картинки; None — по умолчанию


@dataclass
class TurnContext:
    """Что подмешать в конкретный шаг: текст в системный промпт и инструменты, которые спрятать.

    Так ядро узнаёт о проекте разговора или подключённом компьютере, не зная о них
    самих: поставщик контекста решает по пользователю и сессии.
    """

    text: str = ""
    hide_tools: set[str] = field(default_factory=set)


ContextProvider = Callable[[User, str], Awaitable["TurnContext | str | None"]]


@dataclass
class RunOutcome:
    run_id: str
    status: RunStatus
    text: str
    usage: Usage
    events: list[Event]


class Agent:
    def __init__(
        self,
        *,
        router: ModelRouter,
        store: Store,
        tools: ToolRegistry | None = None,
        spec: AgentSpec | None = None,
        subagents: dict[str, AgentSpec] | None = None,
        permissions: Permissions | None = None,
        hooks: Hooks | None = None,
        skills: SkillLibrary | None = None,
        mcp_instructions: Callable[[], str] | None = None,
        config: AgentConfig | None = None,
        deps: dict[str, Any] | None = None,
        workspace: Callable[[str], Path | None] | Path | None = None,
        rules: Callable[[User], str] | str = "",
        memory_scopes: Callable[[User], list[str]] | None = None,
        tracers: list[Any] | None = None,
        can_ask: bool = True,
    ):
        self.router = router
        self.store = store
        self.tools = tools or ToolRegistry()
        self.spec = spec or AgentSpec()
        self.subagents = subagents or {}
        self.permissions = permissions or Permissions()
        self.hooks = hooks or Hooks()
        self.skills = skills
        self.mcp_instructions = mcp_instructions
        self.config = config or AgentConfig()
        self.deps = deps or {}
        self._workspace = workspace
        self.rules = rules
        self.memory_scopes = memory_scopes or (lambda u: ["global", f"user:{u.id}"])
        self.tracers = tracers or []
        # Каталог моделей знает, кто умеет картинки, точнее шаблонов; сервис подставляет его сюда.
        self.vision_for: Callable[[str], bool | None] | None = None
        # Проект разговора, подключённый компьютер и т. п. — см. TurnContext.
        self.context_providers: list[ContextProvider] = []
        # Субагент не может остановиться ради подтверждения: продолжить его
        # прогон было бы некому. Вместо «спросить» он получает отказ.
        self.can_ask = can_ask
        self._cancel: dict[str, asyncio.Event] = {}

        if "tool_search" not in self.tools:
            self.tools.add(make_tool_search(lambda ctx: ctx.agent.tools))
        # Инструмент skill — и при пустой папке: навыки пользователь может создать сам в настройках.
        if self.skills is not None and "skill" not in self.tools:
            self.tools.add(self.skills.tool())

    # ── публичное API ────────────────────────────────────────────────

    async def skills_for(self, user: User) -> SkillLibrary:
        """Навыки, которые видит этот пользователь: из папки приложения + свои, без выключенных."""
        base = self.skills or SkillLibrary()
        return base.for_user(await self.store.kv_get("skills", user.id) or {})

    def workspace_for(self, session_id: str) -> Path | None:
        ws = self._workspace
        if callable(ws):
            return ws(session_id)
        return ws

    def cancel(self, run_id: str) -> bool:
        ev = self._cancel.get(run_id)
        if ev:
            ev.set()
            return True
        return False

    async def run(
        self,
        session_id: str,
        user: User,
        input: str | list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        mode: Mode | str | None = None,
        trigger: str = "user",
    ) -> AsyncIterator[Event]:
        """Прогон по новому сообщению пользователя. Отдаёт события по мере работы."""
        session = await self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"нет сессии {session_id}")
        state = session.state or {}
        model = model or state.get("model") or self.spec.model
        mode = Mode(mode or self.spec.mode or state.get("mode") or self.permissions.mode)

        async def body(emit: Callable[[Event], Any], run_id: str) -> RunStatus:
            # Неотвеченные подтверждения при новом сообщении — отклоняются: человек
            # пошёл другим путём, а висящий вызов без результата сломал бы историю.
            await self._expire_pending(session_id, emit, run_id, "пользователь отправил новое сообщение вместо подтверждения")
            if not await self.store.list_messages(session_id):
                started = fold(await self.hooks.run(HookEvent.SESSION_START,
                                                    {"session_id": session_id, "user": user}, self))
                if started.add_context:
                    await self.store.append_message(
                        session_id, Message.user(started.add_context, kind="hook_context", hidden=True), run_id)
            if input is not None:
                text = _plain_text(input)
                hooked = fold(await self.hooks.run(HookEvent.USER_PROMPT_SUBMIT,
                                                   {"prompt": text, "session_id": session_id, "user": user}, self))
                if hooked.decision == Decision.DENY:
                    await emit(Event(EventType.ERROR, {"message": hooked.reason or "сообщение отклонено"}))
                    return RunStatus.ERROR
                await self.store.append_message(session_id, Message.user(input), run_id)
                if hooked.add_context:
                    await self.store.append_message(
                        session_id, Message.user(hooked.add_context, kind="hook_context", hidden=True), run_id)
                if not session.title:
                    title = _plain_text(input).strip()
                    if title:
                        await self.store.update_session(session_id, title=title.splitlines()[0][:80])
            return await self._loop(session_id, user, run_id, emit, model=model, mode=mode)

        async for ev in self._drive(session_id, body, model=model, trigger=trigger):
            yield ev

    async def resolve_approval(
        self,
        approval_id: str,
        user: User,
        *,
        approve: bool,
        comment: str = "",
        edited_args: dict[str, Any] | None = None,
    ) -> AsyncIterator[Event]:
        """Решение человека по карточке. Когда решены все карточки шага — агент продолжает."""
        rec = await self.store.get_approval(approval_id)
        if rec is None:
            raise ValueError("нет такого подтверждения")
        # Атомарно: двойной клик или два процесса не выполнят действие дважды.
        if rec.status != "pending" or not await self.store.claim_approval(approval_id):
            raise ValueError(f"подтверждение уже {rec.status if rec.status != 'pending' else 'решается'}")
        session = await self.store.get_session(rec.session_id)
        assert session is not None
        state = session.state or {}
        model = state.get("model") or self.spec.model
        mode = Mode(self.spec.mode or state.get("mode") or self.permissions.mode)

        async def body(emit: Callable[[Event], Any], run_id: str) -> RunStatus:
            tool = self.tools.get(rec.tool_name)
            args = edited_args if edited_args is not None else rec.args
            if not approve:
                content = "Пользователь отклонил это действие." + (f" Комментарий: {comment}" if comment else "")
                result = ToolResult(content=content, is_error=True)
            elif tool is None:
                result = ToolResult.error(f"инструмент {rec.tool_name} больше недоступен")
            else:
                try:
                    args = tool.validate(args)
                    seq = await self._last_seq(rec.session_id)
                    ctx = self._ctx(rec.session_id, run_id, user, emit, rec.tool_call_id, seq)
                    result = await self._invoke(tool, args, ctx)
                    if comment:
                        result.content += f"\n\nКомментарий пользователя: {comment}"
                except ToolArgsError as e:
                    result = ToolResult.error(f"неверные аргументы после правки: {e}")
            await self.store.update_approval(approval_id, status="approved" if approve else "rejected",
                                             decided_by=user.id, decided_at=time.time(), comment=comment)
            await emit(Event(EventType.APPROVAL_RESOLVED, {"approval_id": approval_id, "approved": approve,
                                                           "tool_call_id": rec.tool_call_id}))
            await self._record_result(rec.session_id, run_id, emit, rec.tool_call_id, rec.tool_name, result)
            still = [a for a in await self.store.list_approvals(rec.session_id)
                     if a.status in ("pending", "resolving") and a.id != approval_id]
            if still:
                return RunStatus.WAITING_APPROVAL
            return await self._loop(rec.session_id, user, run_id, emit, model=model, mode=mode)

        async for ev in self._drive(rec.session_id, body, model=model, trigger="approval"):
            yield ev

    async def run_to_end(self, session_id: str, user: User, input: str | None, **kw: Any) -> RunOutcome:
        """Прогнать до конца и вернуть итог — для субагентов, расписания, evals, CLI."""
        evs: list[Event] = []
        status = RunStatus.DONE
        run_id = ""
        usage = Usage()
        async for ev in self.run(session_id, user, input, **kw):
            evs.append(ev)
            run_id = ev.run_id or run_id
            if ev.type == EventType.USAGE:
                usage.add(Usage(**{k: v for k, v in ev.data.items() if k in Usage.__dataclass_fields__}))
            if ev.type == EventType.RUN_FINISHED:
                status = RunStatus(ev.data["status"])
        text = ""
        for ev in reversed(evs):
            if ev.type == EventType.MESSAGE and ev.data.get("content") and not ev.agent:
                text = ev.data["content"]
                break
        return RunOutcome(run_id=run_id, status=status, text=text, usage=usage, events=evs)

    async def rewind(self, session_id: str, seq: int) -> dict[str, Any]:
        """Откат диалога к сообщению seq (включительно) и файлов рабочей папки к тому моменту."""
        await self._expire_pending(session_id, None, None, "откат диалога")
        restored = await self._restore_files(session_id, seq)
        removed = await self.store.delete_messages_after(session_id, seq)
        return {"messages_removed": removed, "files_restored": restored}

    # ── внутреннее: запуск с очередью событий ─────────────────────────

    async def _drive(self, session_id, body, *, model: str, trigger: str):
        run = await self.store.create_run(session_id, model=self.router.resolve(model), trigger=trigger)
        q: asyncio.Queue[Event | None] = asyncio.Queue()
        cancel = self._cancel.setdefault(run.id, asyncio.Event())
        usage = Usage()
        steps = 0

        async def emit(ev: Event) -> None:
            nonlocal steps
            ev.run_id = ev.run_id or run.id
            ev.session_id = ev.session_id or session_id
            if ev.type not in EPHEMERAL:
                await self.store.add_event(ev)
            if ev.type == EventType.USAGE:
                # Расход субагентов входит в стоимость прогона, шаги — только свои.
                usage.add(Usage(**{k: v for k, v in ev.data.items() if k in Usage.__dataclass_fields__}))
                steps += 0 if ev.agent else 1
            for t in self.tracers:
                try:
                    t.on_event(ev)
                except Exception:  # noqa: BLE001 — трейсер не должен ронять прогон
                    log.exception("tracer failed")
            await q.put(ev)

        async def runner() -> None:
            status = RunStatus.ERROR
            error = None
            try:
                await emit(Event(EventType.RUN_STARTED, {"model": self.router.resolve(model), "trigger": trigger}))
                status = await body(emit, run.id)
            except asyncio.CancelledError:
                status = RunStatus.CANCELLED
            except LLMError as e:
                error = str(e)
                await emit(Event(EventType.ERROR, {"message": f"Модель недоступна: {e}"}))
            except Exception as e:  # noqa: BLE001
                log.exception("run %s failed", run.id)
                error = f"{type(e).__name__}: {e}"
                await emit(Event(EventType.ERROR, {"message": error}))
            finally:
                if cancel.is_set() and status not in (RunStatus.WAITING_APPROVAL,):
                    status = RunStatus.CANCELLED
                await self.store.update_run(run.id, status=str(status), usage=usage.to_dict(), steps=steps,
                                            error=error, finished_at=time.time())
                await emit(Event(EventType.RUN_FINISHED, {"status": str(status), "usage": usage.to_dict(),
                                                          "error": error}))
                self._cancel.pop(run.id, None)
                await q.put(None)

        task = asyncio.create_task(runner())
        try:
            while True:
                ev = await q.get()
                if ev is None:
                    break
                yield ev
        finally:
            # Если потребитель ушёл раньше (закрыл SSE), прогон доводится до конца
            # в фоне: иначе в истории остался бы вызов инструмента без результата.
            if task.done():
                await task

    # ── внутреннее: цикл ──────────────────────────────────────────────

    def _ctx(self, session_id: str, run_id: str, user: User, emit, call_id: str = "", seq: int = 0) -> ToolContext:
        return ToolContext(
            session_id=session_id, run_id=run_id, user=user, agent=self, store=self.store, emit=emit,
            tool_call_id=call_id, workspace=self.workspace_for(session_id), deps=self.deps,
            cancel=self._cancel.get(run_id) or asyncio.Event(), message_seq=seq, agent_name=self.spec.name,
        )

    async def _turn_context(self, user: User, session_id: str) -> TurnContext:
        out = TurnContext()
        texts = []
        for prov in self.context_providers:
            try:
                res = await prov(user, session_id)
            except Exception:  # noqa: BLE001 — сбой поставщика не должен ронять прогон
                log.exception("context provider failed")
                continue
            if isinstance(res, str):
                res = TurnContext(text=res)
            if res:
                if res.text:
                    texts.append(res.text)
                out.hide_tools |= res.hide_tools
        out.text = "\n\n".join(texts)
        return out

    async def _system_prompt(self, user: User, loaded: set[str], mode: Mode, extra: str = "") -> str:
        mem = await self.store.memory_list(self.memory_scopes(user))
        rules = self.rules(user) if callable(self.rules) else self.rules
        parts = PromptParts(
            agent_prompt=self.spec.prompt,
            rules=rules,
            skills_index=(await self.skills_for(user)).index() if self.skills else "",
            mcp_instructions=self.mcp_instructions() if self.mcp_instructions else "",
            deferred_tools=self.tools.deferred_names(loaded),
            memory=[f"[{m.id}] {m.text}" for m in mem],
            user_name=user.name,
            user_roles=user.roles,
            profile=await self.store.kv_get("profile", user.id) or {},
            mode=str(mode),
            tz=self.config.tz,
            include_core_rules=self.spec.include_core_rules,
        )
        if self.subagents:
            parts.extra.append("# Субагенты (инструмент task)\n" + "\n".join(
                f"- {s.name}: {s.description}" for s in self.subagents.values()))
        if extra:
            parts.extra.append(extra)
        return build_system_prompt(parts)

    async def _loop(self, session_id: str, user: User, run_id: str, emit, *, model: str, mode: Mode) -> RunStatus:
        perms = self.permissions.with_mode(mode)
        cancel = self._cancel.get(run_id) or asyncio.Event()
        stop_continues = 0
        seen_calls: dict[str, int] = {}
        spec = self.spec
        compacted_once = False

        for step in range(spec.max_steps):
            if cancel.is_set():
                return RunStatus.CANCELLED
            # Сообщения, присланные во время работы, — между шагами: здесь все
            # вызовы предыдущего шага уже закрыты результатами, и сообщение
            # пользователя не разорвёт пару «вызов — результат».
            await self._take_inbox(session_id, run_id, emit)
            # Выбор пользователя по инструментам (Всегда / Спрашивать / Заблокирован) — каждый шаг:
            # переключил в настройках посреди долгой задачи — действует со следующего вызова.
            perms = perms.with_mode(mode, await self.store.kv_get("tool_policy", user.id) or {})
            state = await self.store.get_state(session_id)
            loaded = set(state.get(LOADED_KEY, []))
            history = effective_history(await self.store.list_messages(session_id))
            turn = await self._turn_context(user, session_id)
            system = await self._system_prompt(user, loaded, mode, turn.text)
            visible = [t for t in self.tools.visible(loaded) if t.name not in turn.hide_tools and not perms.blocked(t.name)]
            schemas = [t.schema() for t in visible]
            msgs = [Message.system(system), *self._prepare(history, session_id, model)]

            # Сжатие: не чаще раза за шаг и только если есть что сжимать.
            if (not compacted_once and estimate_tokens(msgs, schemas)
                    > self.config.context_window * self.config.compact_threshold):
                hooked = fold(await self.hooks.run(HookEvent.PRE_COMPACT, {"session_id": session_id}, self))
                res = await compact(await self.store.list_messages(session_id), self.router,
                                    model=self.config.compact_model, keep_user_turns=self.config.keep_user_turns,
                                    extra_instructions=hooked.add_context or "")
                if res is not None:
                    compacted_once = True
                    await self.store.append_message(
                        session_id,
                        Message.user(f"[Сводка предыдущей части разговора]\n{res.summary}", kind="summary",
                                     covers_upto=res.covers_upto, hidden=True),
                        run_id,
                    )
                    await emit(Event(EventType.COMPACTED, {"tokens_before": res.tokens_before,
                                                           "covers_upto": res.covers_upto}))
                    history = effective_history(await self.store.list_messages(session_id))
                    msgs = [Message.system(system), *self._prepare(history, session_id, model)]

            last_step = step == spec.max_steps - 1
            if last_step:
                msgs.append(Message.user("[Система] Лимит шагов исчерпан. Инструменты отключены: подведи итог — "
                                         "что сделано, что не успел, что делать дальше."))
            req = LLMRequest(
                model=model, messages=msgs, tools=[] if last_step else schemas,
                max_tokens=spec.max_tokens, temperature=spec.temperature, reasoning_effort=spec.reasoning_effort,
            )

            async def notice(text: str) -> None:
                await emit(Event(EventType.NOTICE, {"message": text}))

            resp = None
            async for ch in self.router.stream(req, on_notice=notice):
                if cancel.is_set():
                    break
                if ch.kind == "text":
                    await emit(Event(EventType.TEXT_DELTA, {"text": ch.text}))
                elif ch.kind == "thinking":
                    await emit(Event(EventType.THINKING_DELTA, {"text": ch.text}))
                elif ch.kind == "done":
                    resp = ch.response
            if resp is None:
                return RunStatus.CANCELLED

            await emit(Event(EventType.USAGE, {**resp.usage.to_dict(), "model": resp.model, "step": step}))
            msg = resp.message
            if last_step:
                msg.tool_calls = []
            seq = await self.store.append_message(session_id, msg, run_id)
            await emit(Event(EventType.MESSAGE, {"seq": seq, "content": msg.text,
                                                 "tool_calls": [tc.to_openai() for tc in msg.tool_calls]}))

            if not msg.tool_calls:
                if last_step:
                    return RunStatus.MAX_STEPS
                hooked = fold(await self.hooks.run(HookEvent.STOP, {"session_id": session_id, "text": msg.text}, self))
                if hooked.continue_with and stop_continues < self.config.max_stop_continues:
                    stop_continues += 1
                    await self.store.append_message(
                        session_id, Message.user(hooked.continue_with, kind="hook_context", hidden=True), run_id)
                    continue
                if await self.store.inbox_count(session_id):
                    continue  # пока отвечали, человек дописал — отвечаем и на это
                return RunStatus.DONE

            pending, stop = await self._execute(session_id, user, run_id, emit, msg.tool_calls, perms, seq, seen_calls)
            if pending:
                return RunStatus.WAITING_APPROVAL
            if stop == "waiting_input":
                return RunStatus.WAITING_INPUT
        return RunStatus.MAX_STEPS

    def _prepare(self, history: list[tuple[int, Message]], session_id: str, model: str) -> list[Message]:
        msgs = _for_model(history)
        if not any(isinstance(m.content, list) for m in msgs):
            return msgs
        ws = self.workspace_for(session_id)
        mid = self.router.resolve(model)
        known = self.vision_for(mid) if self.vision_for else None
        vision = known if known is not None else is_vision(mid, self.config.vision_models)
        return [replace(m, content=expand_parts(m.content, ws, vision=vision)) if isinstance(m.content, list) else m
                for m in msgs]

    async def _take_inbox(self, session_id: str, run_id: str, emit) -> int:
        items = await self.store.inbox_pop(session_id)
        taken = 0
        for it in items:
            # Те же хуки, что у обычного сообщения: иначе фильтр обходился бы отправкой «на ходу».
            hooked = fold(await self.hooks.run(HookEvent.USER_PROMPT_SUBMIT, {
                "prompt": _plain_text(it["content"]), "session_id": session_id, "user": it.get("user")}, self))
            if hooked.decision == Decision.DENY:
                await emit(Event(EventType.ERROR, {"message": hooked.reason or "сообщение отклонено"}))
                continue
            seq = await self.store.append_message(session_id, Message.user(it["content"], steered=True), run_id)
            if hooked.add_context:
                await self.store.append_message(
                    session_id, Message.user(hooked.add_context, kind="hook_context", hidden=True), run_id)
            await emit(Event(EventType.USER_MESSAGE, {"seq": seq, "content": it["content"]}))
            taken += 1
        return taken

    async def _execute(self, session_id, user, run_id, emit, calls: list[ToolCall], perms: Permissions,
                       seq: int, seen: dict[str, int]) -> tuple[int, str | None]:
        """Выполняет вызовы одного шага. Подряд идущие безопасные — параллельно."""
        batches: list[list[ToolCall]] = []
        for c in calls:
            t = self.tools.get(c.name)
            par = bool(t and t.parallel_ok)
            if par and batches and all((self.tools.get(x.name) and self.tools.get(x.name).parallel_ok)
                                       for x in batches[-1]):
                batches[-1].append(c)
            else:
                batches.append([c])

        pending = 0
        stop: str | None = None
        for batch in batches:
            outcomes = await asyncio.gather(*(self._one(session_id, user, run_id, emit, c, perms, seq, seen)
                                              for c in batch))
            for kind, st in outcomes:
                pending += kind == "pending"
                stop = stop or st
        return pending, stop

    async def _one(self, session_id, user, run_id, emit, call: ToolCall, perms: Permissions, seq: int,
                   seen: dict[str, int]) -> tuple[str, str | None]:
        t0 = self.tools.get(call.name)
        await emit(Event(EventType.TOOL_CALL, {"id": call.id, "name": call.name, "args": call.arguments,
                                               "title": t0.title if t0 else ""}))
        tool = self.tools.get(call.name)

        async def done(result: ToolResult) -> tuple[str, str | None]:
            await self._record_result(session_id, run_id, emit, call.id, call.name, result)
            return "done", result.stop

        if tool is None:
            hint = ""
            if any(t.name == call.name for t in self.tools.all()):
                hint = " Он отложенный — сначала загрузи его через tool_search."
            return await done(ToolResult.error(f"нет инструмента «{call.name}».{hint}"))
        state = await self.store.get_state(session_id)
        if tool.deferred and tool.name not in set(state.get(LOADED_KEY, [])):
            return await done(ToolResult.error(
                f"инструмент «{call.name}» отложенный: сначала вызови tool_search(\"select:{call.name}\")."))
        if call.raw_arguments is not None:
            return await done(ToolResult.error(f"аргументы — не JSON-объект: {call.raw_arguments[:300]}"))
        try:
            args = tool.validate(call.arguments)
        except ToolArgsError as e:
            return await done(ToolResult.error(f"неверные аргументы: {e}"))

        ctx = self._ctx(session_id, run_id, user, emit, call.id, seq)
        hooked = fold(await self.hooks.run(HookEvent.PRE_TOOL_USE,
                                           {"tool_name": tool.name, "args": args, "session_id": session_id}, ctx))
        if hooked.updated_args is not None:
            try:
                args = tool.validate(hooked.updated_args)
            except ToolArgsError as e:
                return await done(ToolResult.error(f"хук передал неверные аргументы: {e}"))
        verdict = perms.check(tool, args, ctx)
        decision, reason = verdict.decision, verdict.reason
        if hooked.decision == Decision.DENY:
            decision, reason = Decision.DENY, hooked.reason or "запрещено хуком"
        elif hooked.decision == Decision.ALLOW and decision == Decision.ASK:
            decision = Decision.ALLOW
        elif hooked.decision == Decision.ASK and decision == Decision.ALLOW:
            decision, reason = Decision.ASK, hooked.reason

        if decision == Decision.DENY:
            return await done(ToolResult(content=f"Отказано: {reason}. Не повторяй этот вызов; "
                                                 "предложи человеку другой путь.", is_error=True))
        if decision == Decision.ASK:
            if not self.can_ask:
                return await done(ToolResult(content="Отказано: субагент не может запросить подтверждение. "
                                                     "Верни главному агенту, что нужно сделать.", is_error=True))
            view = tool.describe(args) if tool.describe else _default_view(args)
            rec = await self.store.create_approval(ApprovalRec(
                id=new_id("a_"), session_id=session_id, run_id=run_id, tool_call_id=call.id, tool_name=tool.name,
                args=args, view=view, reason=reason))
            await emit(Event(EventType.APPROVAL_REQUIRED, {
                "approval_id": rec.id, "tool_call_id": call.id, "tool": tool.name, "description": tool.description,
                "args": args, "view": view, "reason": reason, "destructive": tool.destructive}))
            return "pending", None

        sig = tool.name + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        seen[sig] = seen.get(sig, 0) + 1
        result = await self._invoke(tool, args, ctx)
        if seen[sig] >= self.config.loop_guard:
            result.content = (f"[Система] Это {seen[sig]}-й одинаковый вызов {tool.name} с теми же аргументами. "
                              "Результат не изменится — смени подход или спроси человека.\n\n" + result.content)
        if hooked.add_context:
            result.content += f"\n\n[Контекст] {hooked.add_context}"
        return await done(result)

    async def _invoke(self, tool: Tool, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        t0 = time.monotonic()
        try:
            result = await tool.call(args, ctx)
        except TimeoutError:
            result = ToolResult.error(f"инструмент не уложился в {tool.timeout:.0f} с")
        except ToolDenied as e:
            result = ToolResult(content=f"Отказано: {e}", is_error=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — любая ошибка инструмента уходит модели текстом
            log.warning("tool %s failed: %s", tool.name, e, exc_info=True)
            result = ToolResult.error(f"{type(e).__name__}: {e}")
        hooked = fold(await self.hooks.run(HookEvent.POST_TOOL_USE, {
            "tool_name": tool.name, "args": args, "result": result.content, "is_error": result.is_error}, ctx))
        if hooked.updated_result is not None:
            result.content = hooked.updated_result
        if hooked.add_context:
            result.content += f"\n\n[Контекст] {hooked.add_context}"
        limit = tool.max_output_chars or self.config.max_output_chars
        text, cut = truncate_text(result.content, limit)
        if cut:
            bid = await self.store.blob_put(ctx.session_id, "tool_output", result.content,
                                            meta={"tool": tool.name, "chars": len(result.content)})
            text += (f"\n\n[Вывод обрезан: {len(result.content)} символов. Полностью — "
                     f"read_output(id=\"{bid}\", offset=…), или сузь запрос.]")
            result.content = text
        result.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return result

    async def _record_result(self, session_id, run_id, emit, call_id: str, name: str, result: ToolResult) -> None:
        await self.store.append_message(session_id, Message.tool(call_id, name, result.content,
                                                                 is_error=result.is_error, ui=result.ui,
                                                                 elapsed_ms=result.elapsed_ms), run_id)
        await emit(Event(EventType.TOOL_RESULT, {
            "id": call_id, "name": name, "content": result.content[:4000], "is_error": result.is_error,
            "data": _jsonable(result.data), "ui": result.ui, "elapsed_ms": result.elapsed_ms}))

    async def _expire_pending(self, session_id, emit, run_id, why: str) -> None:
        for rec in await self.store.list_approvals(session_id, status="pending"):
            await self.store.update_approval(rec.id, status="expired", decided_at=time.time(), comment=why)
            await self.store.append_message(session_id, Message.tool(
                rec.tool_call_id, rec.tool_name, f"Действие не выполнено: {why}.", is_error=True), run_id)
            if emit:
                await emit(Event(EventType.APPROVAL_RESOLVED, {"approval_id": rec.id, "approved": False,
                                                               "expired": True, "tool_call_id": rec.tool_call_id}))

    async def _last_seq(self, session_id: str) -> int:
        msgs = await self.store.list_messages(session_id)
        return msgs[-1][0] if msgs else 0

    async def _restore_files(self, session_id: str, seq: int) -> list[str]:
        ws = self.workspace_for(session_id)
        if ws is None:
            return []
        first: dict[str, tuple[str, dict]] = {}
        for bid, meta in await self.store.blob_list(session_id, "file_checkpoint"):
            if meta.get("message_seq", 0) >= seq and meta["path"] not in first:
                first[meta["path"]] = (bid, meta)
        restored = []
        for path, (bid, meta) in first.items():
            target = (ws / path).resolve()
            if not str(target).startswith(str(ws.resolve())):
                continue
            if meta.get("existed"):
                got = await self.store.blob_get(bid)
                if got:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(got[0], encoding="utf-8")
            elif target.exists():
                target.unlink()
            restored.append(path)
        return restored

    # ── субагенты ─────────────────────────────────────────────────────

    def child(self, spec: AgentSpec) -> Agent:
        """Агент-субагент: свой промпт и инструменты, общие модель, хранилище и хуки."""
        tools = self.tools.filtered(spec.tools, [*spec.deny_tools, "task"])
        child = Agent(
            router=self.router, store=self.store, tools=tools, spec=spec, subagents={},
            permissions=self.permissions if spec.mode is None else self.permissions.with_mode(spec.mode),
            hooks=self.hooks, skills=self.skills, mcp_instructions=self.mcp_instructions, config=self.config,
            deps=self.deps, workspace=self._workspace, rules=self.rules, memory_scopes=self.memory_scopes,
            tracers=self.tracers, can_ask=False,
        )
        child.context_providers = self.context_providers
        child.vision_for = self.vision_for
        return child


def _for_model(history: list[tuple[int, Message]]) -> list[Message]:
    """История для запроса: без служебных записей и с «заплатками» на оборванные вызовы.

    Если процесс упал между вызовом инструмента и записью результата, в
    истории остаётся assistant с tool_calls без ответа — и провайдер отвергает
    весь запрос. Заплатка в запросе (в базе история не трогается) это лечит.
    """
    out: list[Message] = []
    answered = {m.tool_call_id for _, m in history if m.role == "tool"}
    for _, m in history:
        if m.meta.get("kind") == "event":
            continue
        clean = replace(m, meta={})
        out.append(clean)
        if m.role == "assistant":
            for tc in m.tool_calls:
                if tc.id not in answered:
                    out.append(Message.tool(tc.id, tc.name, "Результат отсутствует: вызов был прерван."))
    return _reorder_tool_results(out)


def _reorder_tool_results(msgs: list[Message]) -> list[Message]:
    """Результаты инструментов должны идти сразу за своим assistant-сообщением.

    Результат подтверждённого действия записывается позже (когда человек
    нажал кнопку), и между вызовом и результатом могли оказаться другие
    записи. Переставляем результаты к своим вызовам.
    """
    by_call: dict[str, Message] = {}
    for m in msgs:
        if m.role == "tool" and m.tool_call_id:
            by_call.setdefault(m.tool_call_id, m)
    out: list[Message] = []
    placed: set[str] = set()
    for m in msgs:
        if m.role == "tool":
            continue
        out.append(m)
        if m.role == "assistant":
            for tc in m.tool_calls:
                r = by_call.get(tc.id)
                if r is not None and tc.id not in placed:
                    out.append(r)
                    placed.add(tc.id)
    return out


def _default_view(args: dict[str, Any]) -> list[list[str]]:
    rows = []
    for k, v in args.items():
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        rows.append([k, s if len(s) < 500 else s[:500] + "…"])
    return rows


def _jsonable(v: Any) -> Any:
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return json.loads(json.dumps(v, default=str))


def _plain_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return json.dumps(content, ensure_ascii=False)
