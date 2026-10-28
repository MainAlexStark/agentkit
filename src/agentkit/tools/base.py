"""Инструмент агента: схема, обработчик и свойства, по которым работают права.

Свойства (read_only, destructive, needs_approval) — единственное, на что
смотрит слой прав. Поэтому они задаются в одном месте, у инструмента, а не
разбросаны по промпту: промпт — не граница безопасности.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, get_args, get_origin, get_type_hints

import jsonschema
from pydantic import BaseModel, Field, ValidationError, create_model

if TYPE_CHECKING:
    from agentkit.agent import Agent
    from agentkit.events import Event
    from agentkit.store.base import Store


@dataclass
class ToolResult:
    content: str  # то, что увидит модель
    is_error: bool = False
    data: Any = None  # структурированный результат для UI / тестов
    ui: list[dict[str, Any]] = field(default_factory=list)  # виджеты для человека (таблица, карточки, график)
    # "waiting_input" — закончить прогон после этого шага (агент задал вопрос человеку).
    stop: str | None = None
    elapsed_ms: int = 0

    @classmethod
    def error(cls, message: str) -> ToolResult:
        return cls(content=f"Ошибка: {message}", is_error=True)


@dataclass
class User:
    id: str
    name: str = ""
    roles: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def has_role(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)


@dataclass
class ToolContext:
    session_id: str
    run_id: str
    user: User
    agent: Agent
    store: Store
    emit: Callable[[Event], Awaitable[None]]
    tool_call_id: str = ""
    workspace: Path | None = None
    # Зависимости хост-приложения: подключение к БД, клиент API, настройки.
    deps: dict[str, Any] = field(default_factory=dict)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    # Номер сообщения, после которого сделан вызов — для чекпоинтов файлов.
    message_seq: int = 0
    agent_name: str | None = None


Handler = Callable[..., Awaitable[Any]]
ApprovalRule = bool | Callable[[dict[str, Any], ToolContext], bool | str | None]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    read_only: bool = False
    destructive: bool = False
    # True — всегда спрашивать; функция — решить по аргументам (вернуть строку = причина).
    needs_approval: ApprovalRule = False
    # Отложенный: в промпт попадает только имя, схема — через tool_search.
    deferred: bool = False
    concurrency_safe: bool | None = None  # по умолчанию = read_only
    timeout: float | None = 300.0
    max_output_chars: int | None = None  # None — общий лимит агента
    group: str = ""  # «files», «web», «mcp:<сервер>», доменная тема
    # Как показать вызов человеку в карточке подтверждения: [[подпись, значение], …]
    describe: Callable[[dict[str, Any]], list[list[str]]] | None = None
    # Pydantic-модель аргументов, если инструмент собран декоратором.
    args_model: type[BaseModel] | None = None
    source: str = "builtin"  # builtin | domain | mcp | skill
    title: str = ""  # человеческое название для интерфейса: «Поиск заметок» вместо notes_search

    @property
    def parallel_ok(self) -> bool:
        return self.read_only if self.concurrency_safe is None else self.concurrency_safe

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        """Проверяет аргументы до вызова; ошибка уходит модели, чтобы она исправилась."""
        args = _unstring(args, self.parameters)
        if self.args_model is not None:
            try:
                return self.args_model.model_validate(args).model_dump()
            except ValidationError as e:
                raise ToolArgsError(_short_validation(e)) from e
        try:
            jsonschema.validate(args, self.parameters)
        except jsonschema.ValidationError as e:
            path = "/".join(str(p) for p in e.absolute_path) or "аргументы"
            raise ToolArgsError(f"{path}: {e.message}") from e
        return args

    async def call(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        res = self.handler(ctx, **args)
        if inspect.isawaitable(res):
            res = await (asyncio.wait_for(res, self.timeout) if self.timeout else res)
        return coerce_result(res)


def _unstring(args: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Список или объект, присланный JSON-строкой, — разворачиваем.

    Дешёвые модели (DeepSeek Flash) кладут массив в аргумент строкой: options="[{…}]".
    Без этого — лишний шаг на «Input should be a valid list» и повтор
    (evals/harness planning/ask-when-ambiguous). Только верхний уровень и только
    там, где схема ждёт array/object, а строка действительно разбирается.
    """
    props = (schema or {}).get("properties") or {}
    out = None
    for k, v in args.items():
        if not isinstance(v, str) or not v.lstrip().startswith(("[", "{")):
            continue
        sch = props.get(k) or {}
        types = {sch.get("type")} | {x.get("type") for x in sch.get("anyOf", []) if isinstance(x, dict)}
        if "$ref" in sch or "items" in sch:
            types.add("array" if "items" in sch else "object")
        if not types & {"array", "object"} or "string" in types:
            continue
        try:
            parsed = json.loads(v)
        except ValueError:
            continue
        if isinstance(parsed, list | dict):
            out = out if out is not None else dict(args)
            out[k] = parsed
    return out if out is not None else args


class ToolArgsError(ValueError):
    pass


class ToolDenied(Exception):
    """Инструмент сам отказал (нет прав у пользователя и т. п.) — текст уходит модели."""


def coerce_result(res: Any) -> ToolResult:
    if isinstance(res, ToolResult):
        return res
    if res is None:
        return ToolResult(content="ok")
    if isinstance(res, str):
        return ToolResult(content=res)
    return ToolResult(content=json.dumps(res, ensure_ascii=False, default=str, indent=1), data=res)


def _short_validation(e: ValidationError) -> str:
    parts = []
    for err in e.errors()[:5]:
        loc = ".".join(str(x) for x in err["loc"]) or "аргументы"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def _strip_titles(schema: Any) -> Any:
    """Pydantic добавляет title на каждое поле — это лишние токены в каждом запросе."""
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


def tool(
    fn: Handler | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    read_only: bool = False,
    destructive: bool = False,
    needs_approval: ApprovalRule = False,
    deferred: bool = False,
    concurrency_safe: bool | None = None,
    timeout: float | None = 300.0,
    group: str = "",
    describe: Callable[[dict[str, Any]], list[list[str]]] | None = None,
    source: str = "domain",
    title: str = "",
) -> Any:
    """Собирает Tool из функции `async def f(ctx, a: int, b: Annotated[str, "описание"] = "")`.

    Описание инструмента — docstring (или description=). Описание аргумента —
    строка в Annotated или pydantic.Field. Первый параметр — всегда ctx.
    """

    def wrap(f: Handler) -> Tool:
        sig = inspect.signature(f)
        hints = get_type_hints(f, include_extras=True)
        fields: dict[str, Any] = {}
        params = list(sig.parameters.values())[1:]  # без ctx
        for p in params:
            ann = hints.get(p.name, Any)
            default = ... if p.default is inspect.Parameter.empty else p.default
            if get_origin(ann) is Annotated:
                base, *meta = get_args(ann)
                desc = next((m for m in meta if isinstance(m, str)), None)
                finfo = next((m for m in meta if hasattr(m, "json_schema_extra")), None)
                if finfo is not None:
                    fields[p.name] = (ann, default if default is not ... else ...)
                else:
                    fields[p.name] = (base, Field(default, description=desc))
            else:
                fields[p.name] = (ann, default)
        model = create_model(f"{f.__name__}_args", **fields)  # type: ignore[call-overload]
        schema = _strip_titles(model.model_json_schema())
        schema.setdefault("properties", {})
        doc = inspect.cleandoc(description or f.__doc__ or "").strip()
        return Tool(
            name=name or f.__name__,
            description=doc,
            parameters=schema,
            handler=f,
            read_only=read_only,
            destructive=destructive,
            needs_approval=needs_approval,
            deferred=deferred,
            concurrency_safe=concurrency_safe,
            timeout=timeout,
            group=group,
            describe=describe,
            args_model=model,
            source=source,
            title=title,
        )

    return wrap(fn) if fn is not None else wrap
