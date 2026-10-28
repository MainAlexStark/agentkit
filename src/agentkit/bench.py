"""Агент для набора evals/harness — проверка самого ядра, без предметной части.

    AGENT_OPENROUTER_API_KEY=… agentkit eval evals/harness --app agentkit.bench:make_agent \\
        --model openrouter:anthropic/claude-sonnet-4.5 --model openrouter:openai/gpt-5-mini --repeat 2

Набор проверяет то, что одинаково у любого агента: работу с файлами, shell,
план и вопросы, подтверждения, устойчивость к инструкциям из данных, память,
длинный контекст и сжатие, субагентов, экономность. Провал здесь — повод
смотреть в ядро (промпт CORE_RULES, описания инструментов, context.py), а не в
домен конкретного агента.

Модели — те же переменные, что у сервиса (AGENT_OPENROUTER_API_KEY, AGENT_OPENAI_API_KEY,
AGENT_OLLAMA_URL, AGENT_YANDEX_API_KEY + AGENT_YANDEX_FOLDER_ID); main/small —
AGENT_MODEL_MAIN / AGENT_MODEL_SMALL. --model переопределяет main на прогон, small
(сжатие, explore, судья) остаётся своим, чтобы сравнение моделей не путалось с
качеством судьи.

Shell здесь включён и работает без подтверждения (режим auto): набор гоняйте в CI
или в контейнере, а не на рабочей машине с важными файлами.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from agentkit.agent import Agent, AgentConfig, AgentSpec
from agentkit.builder import make_router, standard_tools
from agentkit.llm.fake import FakeLLM
from agentkit.llm.router import ModelRouter, RetryPolicy
from agentkit.permissions import Mode, Permissions
from agentkit.store.sql import SQLStore

PROMPT = """Ты — агент общего назначения. У тебя есть рабочая папка с файлами пользователя,
команды оболочки в ней, память и субагенты. Отвечай по-русски, если пользователь не пишет на другом языке."""


def _env(*names: str, default: str = "") -> str:
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return default


def make_router_from_env() -> ModelRouter:
    main = _env("AGENT_MODEL_MAIN", "AGENTKIT_EVAL_MAIN", default="openrouter:anthropic/claude-sonnet-4.5")
    return make_router(
        openrouter_key=_env("AGENT_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
        openai_key=_env("AGENT_OPENAI_API_KEY", "OPENAI_API_KEY"),
        ollama_url=_env("AGENT_OLLAMA_URL"),
        yandex_key=_env("AGENT_YANDEX_API_KEY"), yandex_folder=_env("AGENT_YANDEX_FOLDER_ID"),
        main=main, small=_env("AGENT_MODEL_SMALL", "AGENTKIT_EVAL_SMALL", default="openrouter:anthropic/claude-haiku-4.5"),
        app_name="agentkit-evals",
    )


def make_agent(store: SQLStore, fake: FakeLLM | None = None) -> Agent:
    root = Path(tempfile.mkdtemp(prefix="ak-bench-"))

    def workspace(session_id: str) -> Path:
        p = root / session_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    if fake is not None:
        router = ModelRouter(providers={"fake": fake}, roles={"main": "fake:main", "small": "fake:small"},
                             retry=RetryPolicy(attempts=1))
    else:
        router = make_router_from_env()
    return Agent(
        router=router,
        store=store,
        tools=standard_tools(files=True, shell=True, web_fetch=False, memory=True, subagents=True, visuals=False),
        spec=AgentSpec(prompt=PROMPT, max_steps=40),
        permissions=Permissions(mode=Mode.AUTO),
        workspace=workspace,
        config=AgentConfig(context_window=128_000),
    )
