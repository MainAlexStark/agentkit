"""Сборка агента из типовых частей — чтобы новый проект начинался с 20 строк, а не с 200."""

from __future__ import annotations

from collections.abc import Iterable

from agentkit.llm.openai_compat import OpenAICompatProvider, ollama, openai, openrouter, yandex
from agentkit.llm.router import ModelRouter
from agentkit.subagents import DEFAULT_SUBAGENTS, task_tool
from agentkit.tools.base import Tool
from agentkit.tools.core import core_tools, memory_tools
from agentkit.tools.files import file_tools
from agentkit.tools.registry import ToolRegistry
from agentkit.tools.shell import shell_tool
from agentkit.tools.visual import visual_tool
from agentkit.tools.web import TavilySearch, web_fetch_tool, web_search_tool


def make_router(
    *,
    openrouter_key: str = "",
    yandex_key: str = "",
    yandex_folder: str = "",
    openai_key: str = "",
    ollama_url: str = "",
    main: str = "",
    small: str = "",
    fallbacks: dict[str, list[str]] | None = None,
    app_name: str = "agentkit",
    app_url: str = "",
) -> ModelRouter:
    providers = {}
    if openrouter_key:
        providers["openrouter"] = OpenAICompatProvider(openrouter(openrouter_key, app_name=app_name, app_url=app_url))
    if yandex_key and yandex_folder:
        providers["yandex"] = OpenAICompatProvider(yandex(yandex_key, yandex_folder))
    if openai_key:
        providers["openai"] = OpenAICompatProvider(openai(openai_key))
    if ollama_url:
        providers["ollama"] = OpenAICompatProvider(ollama(ollama_url))
    if not providers:
        raise ValueError("не настроен ни один провайдер моделей: задайте ключ OpenRouter, Yandex, OpenAI или адрес Ollama")
    roles = {}
    if main:
        roles["main"] = main
    roles["small"] = small or main
    return ModelRouter(providers=providers, roles=roles, fallbacks=fallbacks or {})


def standard_tools(
    *,
    files: bool = True,
    files_read_only: bool = False,
    shell: bool = False,
    web_fetch: bool = True,
    tavily_key: str = "",
    memory: bool = True,
    memory_global_roles: tuple[str, ...] = ("admin",),
    subagents: bool = True,
    visuals: bool = True,
    extra: Iterable[Tool] = (),
) -> ToolRegistry:
    reg = ToolRegistry(core_tools())
    if visuals:
        reg.add(visual_tool())
    if files:
        reg.extend(file_tools(read_only=files_read_only))
    if shell:
        reg.add(shell_tool())
    if web_fetch:
        reg.add(web_fetch_tool())
    if tavily_key:
        reg.add(web_search_tool(TavilySearch(tavily_key)))
    if memory:
        reg.extend(memory_tools(global_roles=memory_global_roles))
    if subagents:
        reg.add(task_tool(DEFAULT_SUBAGENTS))
    reg.extend(extra)
    return reg
