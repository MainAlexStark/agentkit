"""Replay — разбор прогона по журналу и превращение его в eval-кейс.

Зачем: «агент сделал глупость» без журнала не разобрать — в чате видно только
итог. Здесь прогон восстанавливается шаг за шагом (что видела модель, что
вызвала, что получила, сколько стоило), а неудачный прогон одной командой
становится регрессионным кейсом: провал из прода → тест, который больше не даст
ему повториться.

Источники журнала:
  * база сервиса — `sqlite:///data/agent.db` или путь к .db, PostgreSQL-URL (таблица ak_events);
  * JSONL — файлы JSONLTracer (`data/traces/*.jsonl`) или `agentkit eval --save-runs`.

    agentkit replay data/agent.db --list                    # последние прогоны
    agentkit replay data/agent.db r_…                       # ход прогона
    agentkit replay data/traces s_… --full                  # вся сессия, с аргументами и выводом целиком
    agentkit replay data/agent.db r_… --to-eval evals/regress.yaml --judge "…" --workspace data/workspaces/s_…
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import yaml

from agentkit.messages import Message

# Прогоны, которые продолжают ход человека, а не начинают новый.
CONTINUATION = {"approval"}
SERVICE_TOOLS = {"todo_write", "tool_search", "read_output", "show_visual"}


# ── загрузка ──────────────────────────────────────────────────────────

def _is_db(source: str) -> bool:
    return "://" in source or source.endswith((".db", ".sqlite", ".sqlite3"))


async def load(source: str, ident: str | None = None) -> tuple[list[dict[str, Any]], list[tuple[int, Message]]]:
    """События (в порядке записи) и, если журнал — база, сообщения сессии (для истории до прогона)."""
    if _is_db(source):
        from agentkit.store.sql import SQLStore  # noqa: PLC0415

        url = source if "://" in source else f"sqlite:///{source}"
        store = await SQLStore(url).init()
        try:
            session_id = run_id = None
            if ident and ident.startswith("s_"):
                session_id = ident
            elif ident:
                run = await store.get_run(ident)
                if run is None:
                    raise SystemExit(f"в базе нет прогона {ident}")
                session_id = run.session_id
            evs: list[dict[str, Any]] = []
            after = 0
            while True:
                page = await store.list_events(session_id=session_id, run_id=run_id, after_seq=after, limit=1000)
                evs += [e.to_dict() for e in page]
                if len(page) < 1000:
                    break
                after = page[-1].seq
            msgs = await store.list_messages(session_id) if session_id else []
            return evs, msgs
        finally:
            await store.close()
    p = Path(source)
    if not p.exists():
        raise SystemExit(f"нет такого файла или папки: {source}")
    files = sorted(p.glob("*.jsonl")) if p.is_dir() else [p]
    evs = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    evs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if ident:
        sid = ident if ident.startswith("s_") else next((e["session_id"] for e in evs if e.get("run_id") == ident), None)
        evs = [e for e in evs if e.get("session_id") == sid or (e.get("agent") and _parent_known(e, evs, sid))]
    return evs, []


def _parent_known(e: dict, evs: list[dict], sid: str | None) -> bool:
    # События субагента пишутся с его сессией в поле agent; к своей сессии их привязывает run_id.
    return any(x.get("run_id") == e.get("run_id") and x.get("session_id") == sid for x in evs)


def runs(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Прогоны главного агента по порядку: id, сессия, старт, вход, статус, шаги, стоимость, события."""
    out: dict[str, dict[str, Any]] = {}
    current: str | None = None
    for e in events:
        rid = e.get("run_id")
        if e["type"] == "run_started" and not e.get("agent"):
            current = rid
            out[rid] = {"id": rid, "session_id": e.get("session_id"), "ts": e.get("ts", 0),
                        "trigger": e["data"].get("trigger", "user"), "model": e["data"].get("model", ""),
                        "input": e["data"].get("input"), "status": "running", "steps": 0, "cost": 0.0,
                        "events": []}
        key = rid if rid in out else current
        if key is None or key not in out:
            continue
        r = out[key]
        r["events"].append(e)
        if e["type"] == "usage":
            r["cost"] += float(e["data"].get("cost") or 0)
            r["steps"] += 0 if e.get("agent") else 1
        elif e["type"] == "run_finished" and not e.get("agent") and rid == key:
            r["status"] = e["data"].get("status", "")
            r["finished"] = e.get("ts")
    return list(out.values())


def turns(rs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Ход человека = прогон по сообщению + прогоны-продолжения после подтверждений."""
    out: list[list[dict[str, Any]]] = []
    for r in rs:
        if r["trigger"] in CONTINUATION and out and out[-1][0]["session_id"] == r["session_id"]:
            out[-1].append(r)
        else:
            out.append([r])
    return out


# ── вывод ─────────────────────────────────────────────────────────────

def _short(v: Any, n: int) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _args(args: Any, full: bool) -> str:
    if full:
        return json.dumps(args, ensure_ascii=False, indent=1, default=str)
    if not isinstance(args, dict):
        return _short(args, 120)
    return ", ".join(f"{k}={_short(v, 60)}" for k, v in args.items())


def run_list(rs: list[dict[str, Any]], limit: int = 30) -> str:
    lines = []
    for r in rs[-limit:]:
        when = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
        lines.append(f"{r['id']}  {r['session_id']}  {when}  {r['trigger']:<8} {r['status']:<16} "
                     f"{r['steps']:>3} шаг. ${r['cost']:.4f}  {_short(r['input'] or '', 60)}")
    return "\n".join(lines) or "прогонов нет"


def timeline(rs: list[dict[str, Any]], *, full: bool = False) -> str:
    """Ход прогонов: шаги, реплики, вызовы с аргументами, результаты, решения человека, итог."""
    out: list[str] = []
    for r in rs:
        t0 = r["ts"]
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0))
        out.append(f"━━ {r['id']} · {r['session_id']} · {when} · {r['trigger']} · {r['model']}")
        if r["input"]:
            out.append(f"  человек: {r['input'] if full else _short(r['input'], 300)}")
        for e in r["events"]:
            d, ty = e.get("data") or {}, e["type"]
            pad = "    │ " if e.get("agent") else "  "
            t = f"{e.get('ts', t0) - t0:+7.1f}s"
            line = None
            if ty == "usage":
                line = (f"шаг {d.get('step', 0) + 1} · {d.get('input_tokens', 0)}→{d.get('output_tokens', 0)} ток."
                        f"{' (кэш ' + str(d['cached_tokens']) + ')' if d.get('cached_tokens') else ''}"
                        f" · ${float(d.get('cost') or 0):.4f} · {d.get('model', '')}")
            elif ty == "message" and d.get("content"):
                line = "агент: " + (d["content"] if full else _short(d["content"], 300))
            elif ty == "tool_call":
                line = f"→ {d.get('name')}({_args(d.get('args'), full)})"
            elif ty == "tool_result":
                mark = "✗" if d.get("is_error") else "←"
                body = d.get("content") or ""
                line = (f"{mark} {d.get('name')} · {d.get('elapsed_ms', 0)} мс · {len(body)} симв.: "
                        + (body if full else _short(body, 200)))
            elif ty == "approval_required":
                line = f"⏸ ждёт подтверждения: {d.get('tool')} {d.get('reason') or ''}".rstrip()
            elif ty == "approval_resolved":
                line = "⏵ " + ("истекло" if d.get("expired") else "разрешено" if d.get("approved") else "отклонено")
            elif ty == "ask_user":
                line = f"? вопрос человеку: {d.get('question')}"
            elif ty == "user_message":
                line = f"человек (на ходу): {_short(d.get('content'), 200)}"
            elif ty == "todo":
                todos = d.get("todos") or []
                done = sum(1 for x in todos if x.get("status") == "completed")
                line = f"☰ план {done}/{len(todos)}: " + "; ".join(_short(x.get("content", ""), 40) for x in todos)
            elif ty == "subagent_started":
                line = f"⤷ субагент {d.get('type')}: {_short(d.get('description', ''), 120)}"
            elif ty == "subagent_finished":
                line = f"⤶ субагент: {d.get('status')}"
            elif ty == "compacted":
                line = f"⇣ история сжата (было ~{d.get('tokens_before')} ток.)"
            elif ty in ("notice", "error"):
                line = ("! " if ty == "error" else "· ") + str(d.get("message"))
            elif ty == "run_finished" and not e.get("agent"):
                u = d.get("usage") or {}
                dur = (e.get("ts", t0) - t0)
                line = (f"■ {d.get('status')} · {r['steps']} шаг. · {u.get('input_tokens', 0)}→{u.get('output_tokens', 0)}"
                        f" ток. · ${float(u.get('cost') or 0):.4f} · {dur:.1f} с"
                        + (f" · {d['error']}" if d.get("error") else ""))
            if line:
                for i, part in enumerate(line.splitlines() or [""]):
                    out.append(f"{t if i == 0 else ' ' * 8} {pad}{part}")
        out.append("")
    return "\n".join(out)


# ── в eval-кейс ───────────────────────────────────────────────────────

def _tool_args(tc: dict[str, Any]) -> tuple[str, Any]:
    fn = tc.get("function") or {}
    raw = fn.get("arguments")
    try:
        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except json.JSONDecodeError:
        args = {}
    return fn.get("name", ""), args


def to_cases(turn: list[dict[str, Any]], *, case_id: str | None = None, judge: str | None = None,
             messages: list[tuple[int, Message]] | None = None, workspace: Path | None = None) -> list[dict[str, Any]]:
    """Ход → два кейса: «проводка» (ответы модели из журнала, офлайн) и «живой» (та же задача реальной модели).

    Проводка ловит поломки ядра и инструментов: тот же сценарий модели должен
    пройти так же. Живой кейс ловит поведение модели и промпта — для него нужен
    критерий (judge), его знает только человек.
    """
    first = turn[0]
    main = [e for r in turn for e in r["events"] if not e.get("agent")]
    # Подсказки хуков и сводки сжатия — скрытые сообщения; в историю кейса они не идут.
    script: list[dict[str, Any] | str] = []
    tools: list[str] = []
    for e in main:
        if e["type"] == "message":
            calls = [list(_tool_args(tc)) for tc in e["data"].get("tool_calls") or []]
            text = e["data"].get("content") or ""
            script.append({"text": text, "tool_calls": calls} if calls else text)
        elif e["type"] == "tool_call" and e["data"].get("name") not in tools:
            tools.append(e["data"]["name"])
    resolved = [e["data"] for e in main if e["type"] == "approval_resolved" and not e["data"].get("expired")]
    approve = "all" if all(x.get("approved") for x in resolved) else "none"
    status = turn[-1]["status"]
    cid = case_id or f"replay-{first['id'][2:10]}"
    history = _history(messages or [], main)
    # Прогоны до версии с input в run_started: запрос — последнее сообщение человека перед ответом.
    last_user = history.pop()["content"] if history and history[-1]["role"] == "user" else ""
    asked = first["input"] or last_user
    if not asked:
        raise SystemExit("в журнале нет текста запроса (старый JSONL без базы): укажите базу сервиса как источник")
    base: dict[str, Any] = {"input": asked}
    if history:
        base["history"] = history
    files = _files(workspace, main) if workspace else {}
    if files:
        base["files"] = files
    if resolved:
        base["approve"] = approve
    wiring = {"id": f"{cid}-wiring", "tags": ["replay", "wiring"], **base,
              "expect": {"status": status, "tools_called": tools}, "script": script}
    live_expect: dict[str, Any] = {"tools_called": [t for t in tools if t not in SERVICE_TOOLS]}
    if judge:
        live_expect["judge"] = judge
    live = {"id": f"{cid}-live", "tags": ["replay", "live"], **base, "expect": live_expect}
    return [wiring, live]


def _history(messages: list[tuple[int, Message]], main: list[dict[str, Any]]) -> list[dict[str, str]]:
    first_seq = next((e["data"].get("seq") for e in main if e["type"] == "message" and e["data"].get("seq")), None)
    if first_seq is None:
        return []
    out = [{"role": m.role, "content": m.text} for seq, m in messages
           if seq < first_seq and m.role in ("user", "assistant") and m.text and not m.meta.get("hidden")]
    return out  # последнее сообщение человека — сам запрос; его снимает to_cases


def _files(ws: Path, main: list[dict[str, Any]], max_bytes: int = 100_000) -> dict[str, str]:
    out: dict[str, str] = {}
    seen: set[str] = set()
    for e in main:
        if e["type"] != "tool_call":
            continue
        rel = (e["data"].get("args") or {}).get("path")
        if not isinstance(rel, str) or rel in seen:
            continue
        seen.add(rel)
        if e["data"].get("name") == "write_file":
            continue  # первым делом записал — файл создал агент, во входные данные он не идёт
        p = (ws / rel).resolve()
        if not p.is_file() or ws.resolve() not in p.parents or p.stat().st_size > max_bytes:
            continue
        try:
            out[rel] = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
    return out


def dump_cases(cases: list[dict[str, Any]], source: str) -> str:
    head = (f"# Из журнала: {source}. files — состояние папки сейчас, а не до прогона: проверьте.\n"
            "# Живому кейсу нужен критерий judge — каким должен быть правильный ответ.\n")
    return head + yaml.safe_dump(cases, allow_unicode=True, sort_keys=False, width=110)


def append_cases(path: Path, cases: list[dict[str, Any]], source: str) -> None:
    """Дописывает кейсы в набор (создаёт его, если нет); id не должны повторяться.

    Дописываем текстом в конец списка cases, а не пересобираем файл: иначе
    пропали бы комментарии в наборе, а они там — основная документация кейсов.
    """
    body = yaml.safe_dump(cases, allow_unicode=True, sort_keys=False, width=110)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_cases([], source).rsplit("[]", 1)[0] + f"name: {path.stem}\ncases:\n"
                        + _indent(body), encoding="utf-8")
        return
    old = path.read_text(encoding="utf-8")
    data = yaml.safe_load(old) or {}
    existing = data if isinstance(data, list) else data.get("cases") or []
    clash = [c["id"] for c in cases if c["id"] in {x.get("id") for x in existing}]
    if clash:
        raise SystemExit(f"в {path} уже есть кейсы {', '.join(clash)} — задайте другой --id")
    note = f"\n  # ── из журнала {source} ──\n"
    new = old.rstrip("\n") + "\n" + (note + _indent(body) if isinstance(data, dict) else "\n" + body)
    check = yaml.safe_load(new)
    got = check if isinstance(check, list) else (check or {}).get("cases") or []
    if len(got) != len(existing) + len(cases):
        raise SystemExit(f"не получилось дописать в {path}: cases — не последний ключ файла. "
                         "Выведите кейсы без --to-eval и вставьте руками")
    path.write_text(new, encoding="utf-8")


def _indent(text: str) -> str:
    return "".join("  " + line if line.strip() else line for line in text.splitlines(keepends=True))
