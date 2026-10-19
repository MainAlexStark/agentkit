"""agentkit — ядро агента (harness).

Быстрый старт:

    from agentkit import Agent, AgentSpec, SQLStore, User, make_router, standard_tools, tool

    @tool(read_only=True)
    async def weather(ctx, city: str) -> str:
        "Погода в городе."
        return "солнечно"

    store = await SQLStore("sqlite:///agent.db").init()
    agent = Agent(router=make_router(openrouter_key=KEY, main="anthropic/claude-sonnet-4.5"),
                  store=store, tools=standard_tools(extra=[weather]),
                  spec=AgentSpec(prompt="Ты — помощник по погоде."))
    s = await store.create_session("u1")
    async for ev in agent.run(s.id, User(id="u1"), "Какая погода в Кирове?"):
        print(ev.type, ev.data)
"""

from agentkit.agent import Agent, AgentConfig, AgentSpec, RunOutcome
from agentkit.builder import make_router, standard_tools
from agentkit.events import Event, EventType, RunStatus
from agentkit.hooks import HookEvent, HookResult, Hooks
from agentkit.llm import FakeLLM, ModelRouter
from agentkit.mcp import MCPManager, MCPServerConfig
from agentkit.messages import Message, ToolCall, Usage
from agentkit.permissions import Decision, Mode, Permissions
from agentkit.skills import Skill, SkillLibrary
from agentkit.store import SQLStore, Store
from agentkit.subagents import DEFAULT_SUBAGENTS, EXPLORE, GENERAL, PLAN, task_tool
from agentkit.tools.base import Tool, ToolContext, ToolDenied, ToolResult, User, tool
from agentkit.tools.registry import ToolRegistry

__version__ = "0.2.0"

__all__ = [
    "DEFAULT_SUBAGENTS",
    "EXPLORE",
    "GENERAL",
    "PLAN",
    "Agent",
    "AgentConfig",
    "AgentSpec",
    "Decision",
    "Event",
    "EventType",
    "FakeLLM",
    "HookEvent",
    "HookResult",
    "Hooks",
    "MCPManager",
    "MCPServerConfig",
    "Message",
    "Mode",
    "ModelRouter",
    "Permissions",
    "RunOutcome",
    "RunStatus",
    "SQLStore",
    "Skill",
    "SkillLibrary",
    "Store",
    "Tool",
    "ToolCall",
    "ToolContext",
    "ToolDenied",
    "ToolRegistry",
    "ToolResult",
    "Usage",
    "User",
    "make_router",
    "standard_tools",
    "task_tool",
    "tool",
]
