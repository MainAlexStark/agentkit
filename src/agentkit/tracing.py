"""Трейсы: каждый прогон — журнал событий, при желании — спаны OpenTelemetry.

События уже лежат в Store (ak_events) — это основной журнал, по нему UI
показывает историю, а replay восстанавливает прогон. Трейсеры здесь — для
внешних систем: файл JSONL для разбора глазами и OTel для Grafana/Jaeger.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from agentkit.events import Event, EventType

log = logging.getLogger("agentkit.trace")


class LogTracer:
    """Коротко в стандартный лог: старт/финиш прогона, вызовы инструментов, ошибки."""

    def on_event(self, ev: Event) -> None:
        if ev.type == EventType.RUN_STARTED:
            log.info("run %s started model=%s", ev.run_id, ev.data.get("model"))
        elif ev.type == EventType.TOOL_CALL:
            log.info("run %s tool %s %s", ev.run_id, ev.data.get("name"), json.dumps(ev.data.get("args"), ensure_ascii=False)[:200])
        elif ev.type == EventType.ERROR:
            log.warning("run %s error %s", ev.run_id, ev.data.get("message"))
        elif ev.type == EventType.RUN_FINISHED:
            log.info("run %s finished %s usage=%s", ev.run_id, ev.data.get("status"), ev.data.get("usage"))


class JSONLTracer:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def on_event(self, ev: Event) -> None:
        if ev.type in (EventType.TEXT_DELTA, EventType.THINKING_DELTA):
            return
        with open(self.dir / f"{time.strftime('%Y-%m-%d')}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(ev.to_dict(), ensure_ascii=False, default=str) + "\n")


class OTelTracer:
    """Спаны: run → tool.<имя>. Требует пакет opentelemetry (extra «otel»)."""

    def __init__(self, service_name: str = "agentkit"):
        from opentelemetry import trace  # noqa: PLC0415 — зависимость необязательная

        self.tracer = trace.get_tracer(service_name)
        self.trace = trace
        self.runs: dict[str, Any] = {}
        self.tools: dict[str, Any] = {}

    def on_event(self, ev: Event) -> None:
        if ev.type == EventType.RUN_STARTED:
            self.runs[ev.run_id] = self.tracer.start_span("agent.run", attributes={
                "agent.session": ev.session_id or "", "agent.model": ev.data.get("model", "")})
        elif ev.type == EventType.TOOL_CALL and ev.run_id in self.runs:
            ctx = self.trace.set_span_in_context(self.runs[ev.run_id])
            self.tools[ev.data["id"]] = self.tracer.start_span(f"tool.{ev.data['name']}", context=ctx)
        elif ev.type == EventType.TOOL_RESULT and ev.data.get("id") in self.tools:
            span = self.tools.pop(ev.data["id"])
            span.set_attribute("tool.is_error", bool(ev.data.get("is_error")))
            span.end()
        elif ev.type == EventType.USAGE and ev.run_id in self.runs:
            span = self.runs[ev.run_id]
            for k in ("input_tokens", "output_tokens", "cost"):
                span.add_event("usage", {k: ev.data.get(k, 0)})
        elif ev.type == EventType.RUN_FINISHED and ev.run_id in self.runs:
            span = self.runs.pop(ev.run_id)
            span.set_attribute("agent.status", ev.data.get("status", ""))
            span.end()
