"""Фабрика агента для evals: `agentkit eval evals/ --app app.evals:make_agent`.

Тот же assemble_agent, что и в сервисе, — evals проверяют ровно того агента,
который поедет в прод, а не похожую копию.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from agentkit import FakeLLM, ModelRouter
from agentkit.llm.router import RetryPolicy
from agentkit.server.service import assemble_agent
from agentkit.server.settings import Settings

from app.domain import build_domain


def make_agent(store, fake: FakeLLM | None):
    settings = Settings(data_dir=Path(tempfile.mkdtemp(prefix="eval-")), scheduler=False, default_mode="default")
    router = None
    if fake is not None:
        router = ModelRouter(providers={"fake": fake}, roles={"main": "fake:main", "small": "fake:small"},
                             retry=RetryPolicy(attempts=1))
    return assemble_agent(settings, build_domain(settings), store, router=router)
