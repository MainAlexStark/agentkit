"""show_visual — графики, сводки KPI и дашборды прямо в чате.

Модель описывает, ЧТО показать (блоки с данными), а рисует веб-чат — в общем
стиле, в светлой и тёмной теме, с подсказками при наведении. Так дешевле по
токенам и надёжнее, чем просить модель писать SVG: оси, масштаб и цвета всегда
правильные. Для нестандартного есть html — он рисуется в изолированном iframe
без доступа к странице, куки и сети (кроме библиотек с CDN).

Схема параметров написана руками и без $ref: так её понимают все провайдеры.
"""

from __future__ import annotations

import math
from typing import Any

from agentkit.tools.base import Tool, ToolContext, ToolResult

CHART_KINDS = ["line", "area", "bar", "stacked_bar", "hbar", "pie", "donut", "scatter"]
MAX_BLOCKS = 24
MAX_POINTS = 5000
MAX_HTML = 200_000

_SERIES = {
    "type": "array",
    "description": "Ряды данных. Для line/area/bar/stacked_bar/hbar: {name, values} — values по одному на каждую "
                   "подпись x. Для pie/donut: один ряд, values по долям. Для scatter: {name, points: [[x, y], …]}.",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "values": {"type": "array", "items": {"type": ["number", "null"]}},
            "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
        },
        "required": ["name"],
    },
}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Заголовок всей визуализации"},
        "blocks": {
            "type": "array",
            "description": "Блоки сверху вниз, по два в ряд (span=2 — на всю ширину). Типы: kpi, chart, table, text.",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["kpi", "chart", "table", "text"]},
                    "span": {"type": "integer", "enum": [1, 2], "description": "2 — на всю ширину"},
                    "title": {"type": "string"},
                    # kpi
                    "label": {"type": "string", "description": "kpi: подпись, например «Выручка за октябрь»"},
                    "value": {"type": ["number", "string"], "description": "kpi: значение"},
                    "unit": {"type": "string", "description": "Единица: «₽», «шт.», «%»"},
                    "delta": {"type": "number", "description": "kpi: изменение в процентах к прошлому периоду, например -4.2"},
                    "good": {"type": "string", "enum": ["up", "down"],
                             "description": "kpi: что хорошо — рост (up, по умолчанию) или снижение (down, например для расходов)"},
                    "hint": {"type": "string", "description": "kpi: мелкая подпись, например «к сентябрю»"},
                    # chart
                    "kind": {"type": "string", "enum": CHART_KINDS,
                             "description": "line — динамика, bar — сравнение, stacked_bar — части по периодам, "
                                            "hbar — длинные подписи или рейтинг, donut/pie — доли (до 6 частей), "
                                            "area — объём во времени, scatter — связь двух величин"},
                    "x": {"type": "array", "items": {"type": ["string", "number"]},
                          "description": "chart: подписи по оси X (даты, категории)"},
                    "series": _SERIES,
                    "y_label": {"type": "string"},
                    "x_label": {"type": "string", "description": "scatter: подпись оси X"},
                    # table
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "rows": {"type": "array", "items": {"type": "array", "items": {"type": ["string", "number", "null"]}}},
                    # text
                    "markdown": {"type": "string", "description": "text: пояснение или вывод (markdown)"},
                },
                "required": ["type"],
            },
        },
        "html": {"type": "string",
                 "description": "Вместо blocks — свой HTML/SVG/JS для нестандартной визуализации (схема, карта, "
                                "интерактивный расчёт). Рисуется в изолированном окне без доступа к сети; "
                                "библиотеки можно подключать с cdnjs.cloudflare.com или cdn.jsdelivr.net. "
                                "Цвета бери из CSS-переменных --text, --muted, --line, --surface, --viz-1…--viz-8."},
        "height": {"type": "integer", "description": "html: высота в пикселях (по умолчанию — по содержимому)"},
    },
}

DESCRIPTION = """Показывает человеку в чате графики, сводку цифр (KPI) или целый дашборд.
Используй, когда динамику, сравнение, доли или набор показателей понятнее увидеть, чем прочитать:
отчёт за период, сравнение по объектам, структура расходов. Данные — только настоящие (из инструментов
или от человека), не придумывай. Не повторяй потом те же цифры текстом — коротко напиши вывод.
Один график — одна мысль; не больше 6 рядов на графике; разные единицы — разные графики (никогда две оси Y).
Визуализация встаёт прямо в ответ: напиши вводную фразу, вызови show_visual, потом вывод — читается одним текстом.
Обычно хватает blocks; html — только для того, что blocks не умеют.
Если нужен файл (отчёт PDF, Excel, HTML-страница) — его создают run_python или write_file: готовый файл сам
откроется у человека в панели сбоку."""


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate(title: str, blocks: list[dict[str, Any]], html: str) -> list[dict[str, Any]]:
    """Проверка до показа: ошибка уходит модели, и она исправит данные, а не человек увидит пустой график."""
    if not blocks and not html:
        raise ValueError("нужно blocks или html")
    if html and len(html) > MAX_HTML:
        raise ValueError(f"html длиннее {MAX_HTML} символов — упрости или покажи блоками")
    if len(blocks) > MAX_BLOCKS:
        raise ValueError(f"больше {MAX_BLOCKS} блоков — раздели на несколько визуализаций")
    points = 0
    out = []
    for i, b in enumerate(blocks, 1):
        t = b.get("type")
        where = f"блок {i} ({t})"
        if t == "kpi":
            if b.get("value") in (None, ""):
                raise ValueError(f"{where}: нет value")
        elif t == "chart":
            kind = b.get("kind") or "line"
            if kind not in CHART_KINDS:
                raise ValueError(f"{where}: kind — один из {', '.join(CHART_KINDS)}")
            series = b.get("series") or []
            if not series:
                raise ValueError(f"{where}: нет series")
            if kind == "scatter":
                for s in series:
                    pts = s.get("points") or []
                    if not pts or any(len(p) != 2 or not all(_num(v) for v in p) for p in pts):
                        raise ValueError(f"{where}: у scatter в каждом ряду points — пары чисел [x, y]")
                    points += len(pts)
            else:
                x = b.get("x") or []
                if kind in ("pie", "donut"):
                    if len(series) != 1:
                        raise ValueError(f"{where}: у {kind} ровно один ряд")
                    if len(x) > 8:
                        raise ValueError(f"{where}: больше 8 долей не читается — сгруппируй мелкие в «Прочее»")
                    if any(_num(v) and v < 0 for v in series[0].get("values") or []):
                        raise ValueError(f"{where}: в долях не бывает отрицательных значений")
                if len(series) > 8:
                    raise ValueError(f"{where}: больше 8 рядов — раздели на несколько графиков или сложи мелкие в «Прочее»")
                for s in series:
                    vals = s.get("values") or []
                    if len(vals) != len(x):
                        raise ValueError(f"{where}: в ряду «{s.get('name')}» {len(vals)} значений, а подписей x — {len(x)}")
                    if any(v is not None and not _num(v) for v in vals):
                        raise ValueError(f"{where}: values — числа (или null для пропуска)")
                    points += len(vals)
            b = {**b, "kind": kind}
        elif t == "table":
            cols = b.get("columns") or []
            if not cols:
                raise ValueError(f"{where}: нет columns")
            if any(len(r) != len(cols) for r in b.get("rows") or []):
                raise ValueError(f"{where}: в каждой строке должно быть {len(cols)} ячеек")
            points += len(b.get("rows") or [])
        elif t == "text":
            if not (b.get("markdown") or "").strip():
                raise ValueError(f"{where}: пустой markdown")
        else:
            raise ValueError(f"{where}: type — kpi, chart, table или text")
        if points > MAX_POINTS:
            raise ValueError(f"больше {MAX_POINTS} точек — агрегируй (по неделям, месяцам) перед показом")
        out.append(b)
    return out


def _summary(title: str, blocks: list[dict[str, Any]], html: str) -> str:
    if html:
        return f"Показано: «{title or 'визуализация'}» (свой HTML)."
    kinds = []
    for b in blocks:
        if b["type"] == "chart":
            kinds.append(f"график {b['kind']}" + (f" «{b['title']}»" if b.get("title") else ""))
        elif b["type"] == "kpi":
            kinds.append(f"KPI «{b.get('label') or b.get('title') or ''}»")
        else:
            kinds.append(b["type"])
    return f"Показано человеку{f' «{title}»' if title else ''}: " + ", ".join(kinds) + ". Цифры он уже видит — напиши вывод."


async def _show_visual(ctx: ToolContext, title: str = "", blocks: list[dict[str, Any]] | None = None, html: str = "",
                       height: int = 0) -> ToolResult:
    try:
        clean = validate(title, blocks or [], html)
    except ValueError as e:
        return ToolResult.error(str(e))
    ui = {"type": "visual", "title": title, "blocks": clean}
    if html:
        ui.update(html=html, height=max(0, min(int(height or 0), 2000)))
    return ToolResult(content=_summary(title, clean, html), ui=[ui])


def visual_tool() -> Tool:
    return Tool(name="show_visual", description=DESCRIPTION, parameters=SCHEMA, handler=_show_visual,
                read_only=True, concurrency_safe=True, group="ui", source="builtin", title="Визуализация",
                timeout=30)
