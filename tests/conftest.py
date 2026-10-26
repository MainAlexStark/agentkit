from __future__ import annotations

import os

import pytest

from agentkit import Agent, AgentConfig, AgentSpec, FakeLLM, ModelRouter, SQLStore, User, standard_tools
from agentkit.llm.router import RetryPolicy


async def _nosleep(_: float) -> None:
    return None


# Ядра Python из тестов песочницы (agentkit.kernel) не должны жить после прогона.
os.environ.setdefault("AK_KERNEL_IDLE", "15")


@pytest.fixture
async def store():
    s = await SQLStore().init()
    yield s
    await s.close()


@pytest.fixture
def user():
    return User(id="u1", name="Алекс", roles=["admin"])


def make_agent(store, fake: FakeLLM, *, tools=None, spec=None, workspace=None, **kw) -> Agent:
    router = ModelRouter(providers={"fake": fake}, roles={"main": "fake:m", "small": "fake:s"},
                         retry=RetryPolicy(attempts=2, base_delay=0), sleep=_nosleep)
    return Agent(router=router, store=store,
                 tools=tools if tools is not None else standard_tools(files=workspace is not None),
                 spec=spec or AgentSpec(), workspace=workspace, config=kw.pop("config", AgentConfig()), **kw)


async def collect(gen):
    return [ev async for ev in gen]


def types(evs):
    return [str(e.type) for e in evs]


def status(evs):
    return [e.data["status"] for e in evs if str(e.type) == "run_finished"][-1]
