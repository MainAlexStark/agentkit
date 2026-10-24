"""run_python / run_bash — код в песочнице (agentkit.sandbox), а не в процессе сервиса.

read_only=True намеренно: песочница без сети и без секретов, меняет только
рабочую папку разговора — подтверждать каждый расчёт было бы шумом. Всё, что
выходит наружу (файлы на компьютер, письма, записи в системы), идёт другими
инструментами со своими подтверждениями.
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx

from agentkit.tools.base import Tool, ToolContext, ToolResult, tool


def sandbox_tools(url: str, token: str = "", *, client: httpx.AsyncClient | None = None) -> list[Tool]:
    base = url.rstrip("/")

    async def call(ctx: ToolContext, kind: str, code: str, timeout: int) -> ToolResult:
        if ctx.workspace is not None:
            ctx.workspace.mkdir(parents=True, exist_ok=True)
        payload = {"session": ctx.session_id, "kind": kind, "code": code, "timeout": timeout}
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            if client is not None:
                r = await client.post(base + "/run", json=payload, headers=headers, timeout=timeout + 15)
            else:
                async with httpx.AsyncClient() as c:
                    r = await c.post(base + "/run", json=payload, headers=headers, timeout=timeout + 15)
        except httpx.HTTPError as e:
            return ToolResult.error(f"песочница недоступна ({e}); проверьте сервис sandbox и AGENT_SANDBOX_URL")
        if r.status_code != 200:
            return ToolResult.error(f"песочница: {r.json().get('error', r.text) if r.content else r.status_code}")
        return _result(r.json())

    @tool(title="Python", read_only=True, concurrency_safe=False, group="sandbox", source="builtin", timeout=660)
    async def run_python(
        ctx: ToolContext,
        code: Annotated[str, "Программа на Python 3. Печатай результат через print; файлы сохраняй в текущую папку"],
        timeout: Annotated[int, "Сколько ждать, секунд (до 600)"] = 120,
    ) -> ToolResult:
        """Выполняет код Python в изолированной песочнице (без интернета). Текущая папка — рабочая папка
        разговора: там вложения пользователя (uploads/) и всё, что ты создал. Есть pandas, numpy,
        matplotlib, openpyxl, python-docx, reportlab. Созданные файлы человек сможет скачать.
        Между вызовами переменные не сохраняются — только файлы."""
        return await call(ctx, "python", code, max(1, min(timeout, 600)))

    @tool(title="Команда в песочнице", read_only=True, concurrency_safe=False, group="sandbox", source="builtin", timeout=660)
    async def run_bash(
        ctx: ToolContext,
        command: Annotated[str, "Команда bash"],
        timeout: Annotated[int, "Сколько ждать, секунд (до 600)"] = 60,
    ) -> ToolResult:
        """Выполняет команду bash в изолированной песочнице (без интернета) в рабочей папке разговора."""
        return await call(ctx, "bash", command, max(1, min(timeout, 600)))

    return [run_python, run_bash]


def _result(d: dict[str, Any]) -> ToolResult:
    code = d.get("exit_code")
    parts = [f"Код выхода: {code if code is not None else 'прервано по времени'} · {d.get('seconds', 0)} с"]
    if d.get("stdout"):
        parts.append("stdout:\n" + d["stdout"])
    if d.get("stderr"):
        parts.append("stderr:\n" + d["stderr"])
    files = d.get("files") or []
    if files:
        parts.append("Файлы: " + ", ".join(f"{f['path']} ({'новый' if f.get('new') else 'изменён'})" for f in files)
                     + (f" и ещё {d['more_files']}" if d.get("more_files") else ""))
    ui = [{"type": "files", "files": [{"path": f["path"], "name": f["path"].rsplit("/", 1)[-1], "size": f["size"]}
                                      for f in files]}] if files else []
    return ToolResult(content="\n\n".join(parts), is_error=code not in (0,), data=d, ui=ui)
