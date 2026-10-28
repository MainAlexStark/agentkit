"""Агент для набора evals/harness — проверка самого ядра, без предметной части.

    AGENT_OPENROUTER_API_KEY=… agentkit eval evals/harness --app agentkit.bench:make_agent --budget 0.5

По умолчанию — дешёвые модели: main — DeepSeek V4.1 Flash (≈ $0.1–0.2 за весь
набор), small (сжатие, explore, судья) — бесплатная Gemma 4 31B с запасной
DeepSeek Flash, если бесплатную упрёт в лимит. Сильные модели — только
осознанно: `--model openrouter:anthropic/claude-sonnet-4.5` стоит ≈ $0.7 за набор,
поэтому вместе с --budget.

Набор проверяет то, что одинаково у любого агента: работу с файлами, shell,
план и вопросы, подтверждения, устойчивость к инструкциям из данных, память,
длинный контекст и сжатие, субагентов, экономность. Провал здесь — повод
смотреть в ядро (промпт CORE_RULES, описания инструментов, context.py), а не в
домен конкретного агента.

Ключи — те же переменные, что у сервиса (AGENT_OPENROUTER_API_KEY, AGENT_OPENAI_API_KEY,
AGENT_OLLAMA_URL, AGENT_YANDEX_API_KEY + AGENT_YANDEX_FOLDER_ID); модели — свои:
AGENTKIT_EVAL_MAIN / AGENTKIT_EVAL_SMALL (AGENT_MODEL_* сервиса намеренно не читаются,
чтобы прогон не ушёл на дорогую модель из .env). --model переопределяет main на прогон, small
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


# Закреплённые версии, а не «~…-latest»: алиас провайдер переставляет на новую модель,
# и сравнение с baseline тихо стало бы сравнением разных моделей.
CHEAP_MAIN = "openrouter:deepseek/deepseek-v4.1-flash"
FREE_SMALL = "openrouter:google/gemma-4-31b-it:free"


def make_router_from_env() -> ModelRouter:
    # Свои переменные набора сильнее сервисных: в .env сервиса main может быть дорогой моделью.
    main = _env("AGENTKIT_EVAL_MAIN", default=CHEAP_MAIN)
    small = _env("AGENTKIT_EVAL_SMALL", default=FREE_SMALL)
    return make_router(
        openrouter_key=_env("AGENT_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
        openai_key=_env("AGENT_OPENAI_API_KEY", "OPENAI_API_KEY"),
        ollama_url=_env("AGENT_OLLAMA_URL"),
        yandex_key=_env("AGENT_YANDEX_API_KEY"), yandex_folder=_env("AGENT_YANDEX_FOLDER_ID"),
        main=main, small=small,
        # У бесплатных моделей лимит запросов в минуту/день: упёрлись — судья и сжатие идут на дешёвую.
        fallbacks={small: [CHEAP_MAIN]} if small.endswith(":free") and small != CHEAP_MAIN else None,
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
