"""run_python / run_bash — код на «компьютере агента» (agentkit.sandbox), а не в процессе сервиса.

read_only=True намеренно: песочница без секретов, интернет — только по белому
списку, меняет только рабочую папку разговора — подтверждать каждый расчёт было
бы шумом. Всё, что выходит наружу (файлы на компьютер человека, письма, записи
в системы), идёт другими инструментами со своими подтверждениями.

url="local" — без отдельного сервера, процессы на этой же машине (разработка):
без этого локальный запуск оставался без run_python, и агент, не найдя его,
«проверял» код чтением файла. Изоляции там нет, поэтому в local инструменты
НЕ read_only: каждый запуск — по подтверждению (в режиме default), иначе код из
чужого файла выполнился бы на ноутбуке разработчика без спроса.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import httpx

from agentkit.tools.base import Tool, ToolContext, ToolResult, tool


def network_note(url: str, egress: str) -> str:
    """Что модели знать о сети: без этого она тратит шаги на pip install там, где сети нет."""
    if url == "local":
        return "Песочница локальная (режим разработки): интернет — как у машины."
    hosts = [h.strip() for h in egress.split(",") if h.strip()]
    if not hosts:
        return "Интернета нет: pip install не сработает — обходись установленным."
    if "*" in hosts:
        return "Интернет есть (кроме внутренних адресов); pip install ставит пакеты в разговор."
    return ("Интернет — только к: " + ", ".join(hosts) + ". pip install работает, если пакет оттуда; "
            "пакеты и файлы сохраняются в разговоре.")


def sandbox_tools(url: str, token: str = "", *, client: httpx.AsyncClient | None = None, egress: str = "",
                  preview: Any = None) -> list[Tool]:
    """preview — объект с link(session, port, path) и async check(session, port) (server/preview.py);
    без него sandbox_preview не выдаётся."""
    base = url.rstrip("/")
    local = url == "local"

    async def call(ctx: ToolContext, kind: str, code: str, timeout: int, background: bool = False,
                   reset: bool = False) -> ToolResult:
        # Папка разговора — та же, что у файловых инструментов: у субагента она своя, и имя
        # папки, а не session_id, гарантирует, что run_python видит файлы, записанные write_file.
        session = ctx.workspace.name if ctx.workspace is not None else ctx.session_id
        if ctx.workspace is not None:
            ctx.workspace.mkdir(parents=True, exist_ok=True)
        if local:
            from agentkit.sandbox import execute  # noqa: PLC0415

            root = ctx.workspace.parent if ctx.workspace is not None else Path("data/workspaces")
            try:
                d = await asyncio.to_thread(execute, root, session, kind, code, timeout, background=background,
                                            reset=reset)
            except (ValueError, OSError) as e:
                return ToolResult.error(f"песочница: {e}")
            return _result(d)
        payload = {"session": session, "kind": kind, "code": code, "timeout": timeout, "background": background,
                   "reset": reset}
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            if client is not None:
                r = await client.post(base + "/run", json=payload, headers=headers, timeout=timeout + 45)
            else:
                async with httpx.AsyncClient() as c:
                    r = await c.post(base + "/run", json=payload, headers=headers, timeout=timeout + 45)
        except httpx.HTTPError as e:
            return ToolResult.error(f"песочница недоступна ({e}); проверьте сервис sandbox и AGENT_SANDBOX_URL")
        if r.status_code != 200:
            try:
                msg = r.json().get("error", r.text)
            except ValueError:
                msg = r.text or str(r.status_code)
            return ToolResult.error(f"песочница: {msg}")
        return _result(r.json())

    net = network_note(url, egress)
    py_doc = (
        "Выполняет код Python на твоём компьютере в песочнице. Текущая папка — рабочая папка разговора: "
        "вложения пользователя (uploads/) и всё, что ты создал (в том числе write_file). Есть pandas, numpy, "
        "matplotlib, openpyxl, python-docx, reportlab, pillow, pypdf. " + net + " Созданные файлы человек "
        "сразу видит и может скачать. Это ядро, как в Jupyter: переменные, импорты и загруженные данные "
        "сохраняются между вызовами (последнее выражение печатается само) — не перечитывай файлы заново. "
        "reset=true — начать с чистого листа. Если ядро перезапустилось, вывод об этом скажет. "
        "Используй, чтобы посчитать, проверить код или файл, построить график, собрать документ.")
    bash_doc = (
        "Выполняет команду bash в песочнице в рабочей папке разговора. " + net + " "
        "background=true — для долгого: команда работает дальше после ответа, вывод пишется в .jobs/<id>.log "
        "(смотри tail, останавливай kill <pid>). Процессы живут, пока разговор активен.")

    @tool(title="Python", description=py_doc, read_only=not local, concurrency_safe=False, group="sandbox", source="builtin", timeout=660)
    async def run_python(
        ctx: ToolContext,
        code: Annotated[str, "Программа на Python 3. Печатай результат через print; файлы сохраняй в текущую папку"],
        timeout: Annotated[int, "Сколько ждать, секунд (до 600)"] = 120,
        reset: Annotated[bool, "Перезапустить ядро перед выполнением: все переменные сбросятся"] = False,
    ) -> ToolResult:
        return await call(ctx, "python", code, max(1, min(timeout, 600)), reset=reset)

    @tool(title="Команда в песочнице", description=bash_doc, read_only=not local, concurrency_safe=False, group="sandbox", source="builtin",
          timeout=660)
    async def run_bash(
        ctx: ToolContext,
        command: Annotated[str, "Команда bash"],
        timeout: Annotated[int, "Сколько ждать, секунд (до 600)"] = 60,
        background: Annotated[bool, "Запустить в фоне и сразу вернуть номер задачи (сервер, долгий расчёт)"] = False,
    ) -> ToolResult:
        return await call(ctx, "bash", command, max(1, min(timeout, 600)), background)

    tools = [run_python, run_bash]
    if preview is not None:
        host = "127.0.0.1 или 0.0.0.0" if local else "0.0.0.0 (не 127.0.0.1 — иначе снаружи контейнера не видно)"

        @tool(title="Открыть сервер человеку", read_only=True, group="sandbox", source="builtin", timeout=30,
              description="Даёт человеку ссылку на сервер, который ты запустил в песочнице: сайт, дашборд "
                          "(Streamlit, Dash), API. Сначала запусти сервер run_bash(background=true) на хосте "
                          f"{host}, потом вызови это с его портом. Ссылка живёт 8 часов, видна только этот "
                          "разговор и этот порт; относительные пути работают, абсолютные (/static/…) — нет, "
                          "WebSocket не проксируется — Streamlit и Gradio не заработают, бери Flask, Dash, FastAPI "
                          "или статику (python -m http.server).")
        async def sandbox_preview(
            ctx: ToolContext,
            port: Annotated[int, "Порт сервера в песочнице"],
            path: Annotated[str, "Страница, например / или /report"] = "/",
            title: Annotated[str, "Подпись на карточке"] = "",
        ) -> ToolResult:
            if not 0 < port < 65536:
                return ToolResult.error("порт — от 1 до 65535")
            session = ctx.workspace.name if ctx.workspace is not None else ctx.session_id
            if problem := await preview.check(session, port):
                return ToolResult.error(f"{problem}. Запусти сервер run_bash(background=true) на хосте {host} "
                                        "и проверь лог в .jobs/.")
            url = preview.link(session, port, path)
            return ToolResult(content=f"Ссылка для человека: {url}\nОна уже показана карточкой — "
                                      "в ответе можно не повторять.",
                              ui=[{"type": "link", "url": url, "title": title or f"Сервер на порту {port}",
                                   "hint": "Компьютер агента · открыть в новой вкладке"}])

        tools.append(sandbox_preview)
    return tools


def _result(d: dict[str, Any]) -> ToolResult:
    code = d.get("exit_code")
    parts = [f"Код выхода: {code if code is not None else 'прервано по времени'} · {d.get('seconds', 0)} с"]
    if job := d.get("job"):
        parts = [f"Запущено в фоне: задача {job['id']}, pid {job['pid']}. Вывод — {job['log']} "
                 f"(tail -n 50 {job['log']}), остановить — kill {job['pid']}."]
    if d.get("stdout") and not job:
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
