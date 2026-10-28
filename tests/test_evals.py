"""Evals и replay: новые поля кейсов, сводка по моделям, сравнение с baseline, журнал → кейс."""

from __future__ import annotations

import json
import textwrap

import yaml
from conftest import make_agent

from agentkit import FakeLLM, standard_tools
from agentkit import replay as rp
from agentkit.evals import (
    CaseResult,
    compare,
    load_suite,
    make_text,
    markdown,
    run_case,
    run_cases,
    save_run,
    suite_files,
    summarize,
)


def factory(tmp_path, **kw):
    def mk(store, fake):
        return make_agent(store, fake or FakeLLM(["нет модели"]),
                          tools=standard_tools(subagents=False, shell=kw.get("shell", False)),
                          workspace=lambda sid: (tmp_path / sid).mkdir(exist_ok=True) or tmp_path / sid)
    return mk


def test_make_text_generators():
    assert make_text({"repeat": "ab", "times": 3}) == "ababab"
    log = make_text({"lines": 5, "line": "ok {i}", "insert": {4: "ERROR"}})
    assert log.splitlines() == ["ok 1", "ok 2", "ok 3", "ERROR", "ok 5"]


async def test_case_files_turns_and_file_checks(tmp_path):
    case = {
        "id": "edit", "mode": "auto",
        "input": "поправь", "turns": ["и допиши"],
        "files": {"a.txt": "hello world\n", "sub/b.txt": "x"},
        "expect": {"status": "done", "tools_called": ["edit_file"],
                   "tool_args": [{"tool": "edit_file", "args": {"new_string": "kirov"}}],
                   "files": {"a.txt": {"contains": ["hello Kirov", "!"], "not_contains": ["world"]},
                             "sub/b.txt": {"exists": True}, "gone.txt": {"exists": False}},
                   "max_tool_calls": 3, "max_tool_errors": 0},
        "script": [
            {"tool_calls": [["edit_file", {"path": "a.txt", "old_string": "world", "new_string": "Kirov"}]]},
            "Поправил.",
            {"tool_calls": [["edit_file", {"path": "a.txt", "old_string": "Kirov", "new_string": "Kirov!"}]]},
            "Дописал.",
        ],
    }
    r = await run_case(case, factory(tmp_path))
    assert r.passed, r.failures
    assert r.tools == ["edit_file", "edit_file"] and r.text == "Дописал."
    assert any(e["type"] == "run_started" and e["data"].get("input") == "поправь" for e in r.events)


async def test_case_failures_are_named(tmp_path):
    case = {"id": "bad", "input": "x", "files": {"a.txt": "1"},
            "expect": {"files": {"a.txt": "2", "b.txt": {"exists": True}}, "regex": "^\\d+$",
                       "tools_called_any": ["grep", "glob_files"], "asked_user": True, "compacted": True},
            "script": ["нет"]}
    r = await run_case(case, factory(tmp_path))
    assert not r.passed
    text = "\n".join(r.failures)
    for part in ("в a.txt нет «2»", "нет файла b.txt", "не совпал", "ни один из grep", "не задал вопрос",
                 "не сжималась"):
        assert part in text, text


async def test_answers_history_memory_policy(tmp_path):
    seen = {}

    def first(req):
        seen["prompt"] = req.messages[0].text
        seen["history"] = [m.text for m in req.messages[1:]]
        seen["tools"] = {t["function"]["name"] for t in req.tools}
        return {"tool_calls": [["ask_user", {"question": "Какой?"}]]}

    case = {"id": "ask", "input": "сделай", "answers": ["второй"],
            "history": [{"role": "user", "content": "заказ 58213"}, {"role": "assistant", "content": "ок"}],
            "memory": ["отвечать кратко"], "tool_policy": {"write_file": "deny"},
            "expect": {"status": "done", "asked_user": True, "contains": ["второй"]},
            "script": [first, lambda req: f"Выбран {req.messages[-1].text}"]}
    r = await run_case(case, factory(tmp_path))
    assert r.passed, r.failures
    assert "отвечать кратко" in seen["prompt"]
    assert seen["history"][:2] == ["заказ 58213", "ок"]
    assert "write_file" not in seen["tools"]


async def test_deny_config_and_max_steps_override(tmp_path):
    case = {"id": "deny", "input": "x", "deny": ["write_file"], "max_steps": 2, "config": {"loop_guard": 9},
            "expect": {"status": "max_steps", "files": {"a.txt": {"exists": False}}},
            "script": [{"tool_calls": [["write_file", {"path": "a.txt", "content": "1"}]]}, "итог"]}
    r = await run_case(case, factory(tmp_path))
    assert r.passed, r.failures


async def test_models_repeat_parallel_and_summary(tmp_path):
    live = {"id": "live", "input": "привет", "expect": {"contains": ["нет модели"]}}
    offline = {"id": "off", "input": "x", "expect": {"contains": ["ok"]}, "script": ["ok"]}
    rs = await run_cases([live, offline], factory(tmp_path), models=["fake:a", "fake:b"], repeat=2, parallel=4)
    # офлайн-кейс — только на первой модели: модель ему не нужна
    assert sorted((r.id, r.model) for r in rs) == sorted(
        [("live", "fake:a")] * 2 + [("live", "fake:b")] * 2 + [("off", "fake:a")] * 2)
    s = summarize(rs)
    assert s["fake:a"]["runs"] == 4 and s["fake:a"]["pass_rate"] == 1.0 and s["fake:b"]["cases"] == 1


def _rep(passes):
    return [{"suite": "s", "results": [{"id": i, "model": "m", "passed": p, "failures": [] if p else ["x"]}
                                       for i, p in passes.items()]}]


def test_compare_and_markdown():
    d = compare(_rep({"a": True, "b": False, "c": True}), _rep({"a": True, "b": True, "c": False, "z": True}))
    assert [x["id"] for x in d["regressions"]] == ["b"] and [x["id"] for x in d["fixed"]] == ["c"]
    md = markdown(_rep({"a": True, "b": False}), d)
    assert "| m | 1/2 (50%)" in md and "`s/b`" in md and "сломалось" in md and "починилось" in md


def test_harness_suite_is_valid():
    ids = []
    for f in suite_files("evals/harness"):
        _, cases = load_suite(f)
        for c in cases:
            assert c.get("id") and c.get("input") and c.get("expect"), c
            assert not c.get("script"), f"{c['id']}: набор harness — для живых моделей"
            for v in (c.get("files") or {}).values():
                make_text(v)
            ids.append(c["id"])
    assert len(ids) >= 30 and len(ids) == len(set(ids))


# ── replay ────────────────────────────────────────────────────────────

async def test_replay_timeline_and_roundtrip_to_eval(tmp_path):
    case = {"id": "orig", "mode": "default", "approve": "all", "input": "создай отчёт",
            "files": {"in.txt": "данные"},
            "expect": {"status": "done"},
            "script": [{"text": "Читаю.", "tool_calls": [["read_file", {"path": "in.txt"}]]},
                       {"tool_calls": [["write_file", {"path": "out.txt", "content": "отчёт"}]]},
                       "Готово: out.txt"]}
    r = await run_case(case, factory(tmp_path))
    assert r.passed, r.failures
    log = save_run(tmp_path / "runs", "suite", r)
    events, _ = await rp.load(str(log))
    rs = rp.runs(events)
    assert [x["trigger"] for x in rs] == ["user", "approval"]
    tl = rp.timeline(rs)
    for part in ("человек: создай отчёт", "→ read_file(path=in.txt)", "⏸ ждёт подтверждения: write_file",
                 "⏵ разрешено", "агент: Готово: out.txt", "■ done"):
        assert part in tl, tl
    turn = rp.turns(rs)[0]
    assert len(turn) == 2
    ws = tmp_path / r.events[0]["session_id"]
    cases = rp.to_cases(turn, case_id="rep", judge="Сообщает про out.txt", workspace=ws)
    wiring, live = cases
    assert wiring["id"] == "rep-wiring" and wiring["approve"] == "all" and wiring["files"] == {"in.txt": "данные"}
    assert wiring["expect"] == {"status": "done", "tools_called": ["read_file", "write_file"]}
    assert live["expect"]["judge"] == "Сообщает про out.txt" and "script" not in live
    # Проводка из журнала проходит офлайн так же, как исходный прогон.
    wiring["mode"] = "default"
    again = await run_case(wiring, factory(tmp_path))
    assert again.passed, again.failures

    suite = tmp_path / "regress.yaml"
    suite.write_text(textwrap.dedent("""\
        # мой комментарий
        name: regress
        cases:
          - id: old
            input: "x"
            expect: {}
    """), encoding="utf-8")
    rp.append_cases(suite, cases, str(log))
    text = suite.read_text(encoding="utf-8")
    assert "# мой комментарий" in text
    assert [c["id"] for c in yaml.safe_load(text)["cases"]] == ["old", "rep-wiring", "rep-live"]
    new = tmp_path / "new" / "fresh.yaml"
    rp.append_cases(new, cases, str(log))
    assert len(load_suite(new)[1]) == 2


async def test_replay_from_db_uses_messages_for_history(tmp_path):
    from agentkit import SQLStore, User

    db = tmp_path / "agent.db"
    store = await SQLStore(f"sqlite:///{db}").init()
    agent = factory(tmp_path)(store, FakeLLM(["Первый ответ", "Второй ответ"]))
    s = await store.create_session("u1")
    user = User(id="u1")
    async for _ in agent.run(s.id, user, "первый вопрос"):
        pass
    async for _ in agent.run(s.id, user, "второй вопрос"):
        pass
    await store.close()
    events, msgs = await rp.load(str(db), s.id)
    rs = rp.runs(events)
    assert len(rs) == 2 and "второй вопрос" in rp.run_list(rs)
    live = rp.to_cases(rp.turns(rs)[-1], messages=msgs)[1]
    assert live["input"] == "второй вопрос"
    assert live["history"] == [{"role": "user", "content": "первый вопрос"},
                               {"role": "assistant", "content": "Первый ответ"}]


def test_cli_eval_json_md_baseline(tmp_path, capsys, monkeypatch):
    from agentkit.cli import main

    (tmp_path / "s.yaml").write_text(textwrap.dedent("""
        name: s
        cases:
          - id: ok
            input: "x"
            expect: {contains: ["да"]}
            script: ["да"]
    """), encoding="utf-8")
    mod = tmp_path / "evfactory.py"
    mod.write_text(textwrap.dedent("""
        from agentkit import AgentSpec, Agent, ModelRouter, standard_tools
        from agentkit.llm.router import RetryPolicy

        def make_agent(store, fake):
            r = ModelRouter(providers={"fake": fake}, roles={"main": "fake:m", "small": "fake:s"},
                            retry=RetryPolicy(attempts=1))
            return Agent(router=r, store=store, tools=standard_tools(files=False, subagents=False), spec=AgentSpec())
    """), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out, md = tmp_path / "out.json", tmp_path / "sum.md"
    import pytest

    with pytest.raises(SystemExit) as e:
        main(["eval", "s.yaml", "--app", "evfactory:make_agent", "--offline", "--json", str(out), "--md", str(md)])
    assert e.value.code == 0
    rep = json.loads(out.read_text())
    assert rep[0]["summary"][""]["pass_rate"] == 1.0
    with pytest.raises(SystemExit) as e:
        main(["eval", "s.yaml", "--app", "evfactory:make_agent", "--offline", "--baseline", str(out), "--md", str(md)])
    assert e.value.code == 0
    assert "Против baseline" in md.read_text() and "против baseline: 100% → 100%" in capsys.readouterr().out


def test_case_result_to_dict_has_no_events():
    r = CaseResult(id="x", passed=True, events=[{"type": "run_started"}])
    assert "events" not in r.to_dict()
