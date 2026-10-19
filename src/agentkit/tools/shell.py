"""Команды оболочки в рабочей папке. По умолчанию НЕ подключается.

Shell — самый опасный инструмент: через него обходится любое правило про
файлы. Подключайте его только агенту, который работает в изолированном
контейнере, и держите в режиме default (каждая команда — с подтверждением)
или с allow-правилами на конкретные безопасные команды:
    Permissions(allow=["shell(git status*)", "shell(ls*)", "shell(pytest*)"])
"""

from __future__ import annotations

import asyncio
import os
from typing import Annotated

from agentkit.context import truncate_text
from agentkit.tools.base import Tool, ToolContext, ToolDenied, ToolResult, tool


def shell_tool(*, timeout: float = 120.0, env: dict[str, str] | None = None) -> Tool:
    @tool(title="Команда", group="shell", destructive=True, timeout=timeout + 5, source="builtin",
          describe=lambda a: [["Команда", a["command"]]])
    async def shell(
        ctx: ToolContext,
        command: Annotated[str, "Команда bash; выполняется в корне рабочей папки"],
        timeout_s: Annotated[float, "Таймаут, секунд"] = timeout,
    ) -> ToolResult:
        """Выполняет команду оболочки в рабочей папке и возвращает stdout+stderr и код выхода.
        Не используй для чтения и поиска по файлам — для этого есть read_file, grep, glob_files."""
        if ctx.workspace is None:
            raise ToolDenied("у агента нет рабочей папки")
        proc = await asyncio.create_subprocess_shell(
            command, cwd=str(ctx.workspace), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, **(env or {})}, start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), min(timeout_s, timeout))
        except TimeoutError:
            proc.kill()
            return ToolResult.error(f"команда не уложилась в {timeout_s:.0f} с и остановлена")
        text, _ = truncate_text(out.decode("utf-8", "replace"), 30_000)
        return ToolResult(content=f"[код выхода {proc.returncode}]\n{text}", is_error=proc.returncode != 0)

    return shell
