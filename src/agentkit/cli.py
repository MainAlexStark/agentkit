"""agentkit — командная строка.

  agentkit new <имя> [--dest DIR] [--git]     новый проект-агент из шаблона
  agentkit vendor [--dest DIR]                 обновить vendor/agentkit в проекте
  agentkit eval <путь> --app mod:fn [--offline|--live] [--only ID] [--tag T] [--model M …] [--repeat N]
                [--parallel N] [--json FILE] [--md FILE] [--baseline FILE] [--save-runs DIR] [--min-pass-rate X]
  agentkit replay <база|jsonl|папка> [ID] [--list] [--full] [--to-eval FILE --id ИМЯ --judge "…" --workspace DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import re
import shutil
import sys
from pathlib import Path

GIT_URL = "git+https://github.com/MainAlexStark/agentkit"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "data", ".venv", "*.db", ".env",
                                "deploy/*.env", "backups", ".ruff_cache")


def _pkg_root() -> Path:
    return Path(__file__).resolve().parent


def _template_dir() -> Path:
    # В установленном пакете шаблон лежит внутри (agentkit/_template), в репозитории — рядом с src/.
    inside = _pkg_root() / "_template"
    if inside.is_dir():
        return inside
    repo = _pkg_root().parents[1] / "template"
    if repo.is_dir():
        return repo
    sys.exit("шаблон не найден: переустановите agentkit")


def _core_source() -> Path:
    """Корень исходников ядра (где pyproject.toml и src/agentkit) для vendor."""
    repo = _pkg_root().parents[1]
    if (repo / "pyproject.toml").is_file() and (repo / "src" / "agentkit").is_dir():
        return repo
    sys.exit("vendor работает из исходников agentkit (git clone + pip install -e .)")


def vendor(dest: Path) -> None:
    src = _core_source()
    target = dest / "vendor" / "agentkit"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    shutil.copy2(src / "pyproject.toml", target / "pyproject.toml")
    (target / "README.md").write_text("Копия ядра agentkit. Обновляется командой `agentkit vendor`, руками не править.\n",
                                      encoding="utf-8")
    shutil.copytree(src / "src", target / "src", ignore=IGNORE)
    # Шаблон внутри vendor не нужен: он только для `agentkit new`.
    pp = target / "pyproject.toml"
    pp.write_text(re.sub(r"\n\[tool\.hatch\.build\.targets\.wheel\.force-include\][^\[]*", "\n", pp.read_text()),
                  encoding="utf-8")
    from agentkit import __version__

    (dest / "requirements.txt").write_text(f"./vendor/agentkit[service]  # agentkit {__version__}\n", encoding="utf-8")
    print(f"vendor/agentkit обновлён до {__version__}")


def new(name: str, dest: Path | None, use_git: bool) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,40}", name):
        sys.exit("имя проекта: латиница в нижнем регистре, цифры и дефис (например ei-assistant)")
    dest = (dest or Path.cwd()) / name
    if dest.exists():
        sys.exit(f"{dest} уже существует")
    shutil.copytree(_template_dir(), dest, ignore=IGNORE)
    for p in dest.rglob("*"):
        if p.is_file() and p.suffix in {".py", ".md", ".toml", ".yaml", ".yml", ".sh", ".example", ".json", ".txt", ""}:
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            new_text = text.replace("agent-service", name)
            if new_text != text:
                p.write_text(new_text, encoding="utf-8")
    for p in sorted(dest.rglob("*agent-service*"), reverse=True):
        p.rename(p.with_name(p.name.replace("agent-service", name)))
    # В репозитории шаблон смотрит на ядро по относительному пути — в новом проекте это не так.
    pp = dest / "pyproject.toml"
    pp.write_text(re.sub(r"\n\[tool\.uv\.sources\][^\[]*", "\n", pp.read_text(encoding="utf-8")), encoding="utf-8")
    from agentkit import __version__

    if use_git:
        (dest / "requirements.txt").write_text(f"agentkit[service] @ {GIT_URL}@v{__version__}\n", encoding="utf-8")
    else:
        vendor(dest)
    (dest / "deploy.sh").chmod(0o755)
    print(f"""Готово: {dest}

Дальше:
  cd {dest}
  python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt pytest
  cp deploy/{name}.env.example .env      # впишите ключ модели и токен
  uvicorn app.main:app --reload --port 8080
  pytest && agentkit eval evals/ --app app.evals:make_agent --offline

Что менять в первую очередь: app/domain/prompt.md, app/domain/tools.py, mcp.json, evals/.""")


def eval_cmd(a: argparse.Namespace) -> int:
    from agentkit.evals import (  # noqa: PLC0415
        Report,
        compare,
        load_report,
        load_suite,
        markdown,
        run_cases,
        save_run,
        select_cases,
        summarize,
    )

    sys.path.insert(0, str(Path.cwd()))
    mod, _, fn = a.app.partition(":")
    make_agent = getattr(importlib.import_module(mod), fn or "make_agent")
    from agentkit.evals import suite_files  # noqa: PLC0415

    files = suite_files(a.path)
    models = a.model or [None]

    async def go() -> list[dict]:
        reports = []
        for f in files:
            name, cases = load_suite(f)
            cases = select_cases(cases, only=a.only, tags=a.tag, offline=a.offline, live=a.live)
            if not cases:
                continue

            def progress(r, name=name):
                if a.parallel > 1:  # при параллельном прогоне — строка по мере готовности, иначе тишина минутами
                    print(f"  {'✓' if r.passed else '✗'} {name}/{r.id} {r.model} #{r.attempt + 1} "
                          f"[{r.seconds:.0f} с, ${r.usage.get('cost', 0):.4f}]", file=sys.stderr, flush=True)

            results = await run_cases(cases, make_agent, models=models, repeat=a.repeat, parallel=a.parallel,
                                      timeout=a.timeout, judge_model=a.judge_model, on_result=progress)
            rep = Report(suite=name, results=results)
            print(rep.table())
            if a.save_runs:
                for r in results:
                    if a.save_all or not r.passed:
                        save_run(a.save_runs, name, r)
            reports.append(rep.to_dict())
        return reports

    reports = asyncio.run(go())
    rows = [r for rep in reports for r in rep["results"]]
    summary = summarize(rows)
    if len(rows):
        print()
        for m, s in summary.items():
            print(f"{m or 'модель по умолчанию'}: {s['passed']}/{s['runs']} ({s['pass_rate']:.0%}), pass^k {s['pass_all']:.0%}, "
                  f"${s['cost']:.4f}, {s['avg_steps']} шагов и {s['avg_seconds']} с на прогон")
    diff = compare(reports, load_report(a.baseline)) if a.baseline and Path(a.baseline).exists() else None
    if diff:
        print(f"против baseline: {diff['pass_rate_was']:.0%} → {diff['pass_rate_now']:.0%}, "
              f"сломалось {len(diff['regressions'])}, починилось {len(diff['fixed'])}")
    if a.json:
        Path(a.json).write_text(json.dumps(reports, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if a.md:
        text = markdown(reports, diff)
        if a.md == "-":
            print(text)
        else:
            with open(a.md, "a", encoding="utf-8") as fh:  # дописываем: так работает $GITHUB_STEP_SUMMARY
                fh.write(text)
    total = len(rows)
    passed = sum(r["passed"] for r in rows)
    if a.min_pass_rate is not None:
        return 0 if total and passed / total >= a.min_pass_rate else 1
    return 1 if total - passed else 0


def replay_cmd(a: argparse.Namespace) -> int:
    from agentkit import replay as rp  # noqa: PLC0415

    events, messages = asyncio.run(rp.load(a.source, a.id))
    rs = rp.runs(events)
    if not rs:
        sys.exit("в журнале нет прогонов" + (f" для {a.id}" if a.id else ""))
    if a.list:
        print(rp.run_list(rs, a.limit))
        return 0
    if a.id and a.id.startswith("r_"):
        tl = rp.turns(rs)
        turn = next((t for t in tl if any(r["id"] == a.id for r in t)), None)
        if turn is None:
            sys.exit(f"нет прогона {a.id}")
    else:
        # Без id — последний ход; по сессии — показываем все ходы, в eval идёт последний.
        if not a.id:
            rs = [r for r in rs if r["session_id"] == rs[-1]["session_id"]]
        turn = rp.turns(rs)[-1]
    if not a.to_eval:
        shown = turn if (a.id or "").startswith("r_") else rs
        print(rp.timeline(shown, full=a.full))
        return 0
    cases = rp.to_cases(turn, case_id=a.case_id, judge=a.judge, messages=messages,
                        workspace=Path(a.workspace) if a.workspace else None)
    if a.to_eval == "-":
        print(rp.dump_cases(cases, a.source))
    else:
        rp.append_cases(Path(a.to_eval), cases, a.source)
        print(f"в {a.to_eval} добавлены кейсы {', '.join(c['id'] for c in cases)}")
    return 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="agentkit", description="Ядро агента и шаблон сервиса")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_new = sub.add_parser("new", help="новый проект-агент из шаблона")
    p_new.add_argument("name")
    p_new.add_argument("--dest", type=Path)
    p_new.add_argument("--git", action="store_true", help="зависимость по git-тегу вместо копии в vendor/")
    p_v = sub.add_parser("vendor", help="обновить vendor/agentkit в текущем проекте")
    p_v.add_argument("--dest", type=Path, default=Path.cwd())
    p_e = sub.add_parser("eval", help="прогнать evals")
    p_e.add_argument("path", type=Path)
    p_e.add_argument("--app", required=True, help="модуль:функция, собирающая агента (store, fake) → Agent")
    p_e.add_argument("--offline", action="store_true", help="только кейсы со script (без реальной модели)")
    p_e.add_argument("--live", action="store_true", help="только кейсы без script (реальная модель)")
    p_e.add_argument("--only", help="id кейса или шаблон: files-*")
    p_e.add_argument("--tag", action="append", help="только кейсы с тегом (можно несколько)")
    p_e.add_argument("--model", action="append", help="модель вместо main, «провайдер:модель»; можно несколько")
    p_e.add_argument("--repeat", type=int, default=1, help="прогонов каждого кейса (модель недетерминирована)")
    p_e.add_argument("--parallel", type=int, default=1, help="кейсов одновременно")
    p_e.add_argument("--timeout", type=float, default=None, help="секунд на кейс")
    p_e.add_argument("--judge-model", default="small", help="модель судьи (по умолчанию роль small)")
    p_e.add_argument("--json", help="отчёт в JSON (годится как --baseline в следующий раз)")
    p_e.add_argument("--md", help="сводка в Markdown: файл (дописывается) или «-»")
    p_e.add_argument("--baseline", help="прошлый отчёт JSON: что сломалось и что починилось")
    p_e.add_argument("--save-runs", help="папка для журналов упавших прогонов (читаются agentkit replay)")
    p_e.add_argument("--save-all", action="store_true", help="сохранять журналы и прошедших прогонов")
    p_e.add_argument("--min-pass-rate", type=float, help="код выхода 1, только если доля успешных ниже (0..1)")
    p_r = sub.add_parser("replay", help="ход прогона по журналу; прогон → eval-кейс")
    p_r.add_argument("source", help="база (data/agent.db, sqlite:///…, postgresql://…), файл JSONL или папка трейсов")
    p_r.add_argument("id", nargs="?", help="r_… — прогон, s_… — сессия; без id — последний")
    p_r.add_argument("--list", action="store_true", help="список прогонов")
    p_r.add_argument("--limit", type=int, default=30)
    p_r.add_argument("--full", action="store_true", help="аргументы и вывод целиком")
    p_r.add_argument("--to-eval", help="дописать кейсы в YAML-набор (или «-» — вывести)")
    p_r.add_argument("--id", dest="case_id", help="имя кейса")
    p_r.add_argument("--judge", help="критерий правильного ответа для живого кейса")
    p_r.add_argument("--workspace", help="папка разговора: взять оттуда файлы, которые читал агент")
    # Мост и песочница разбирают свои аргументы сами — здесь только передаём остаток.
    sub.add_parser("bridge", help="мост: папки и команды этого компьютера для агента", add_help=False)
    sub.add_parser("sandbox", help="песочница для run_python / run_bash", add_help=False)
    sub.add_parser("egress", help="прокси с белым списком доменов для песочницы", add_help=False)
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("bridge", "sandbox", "egress"):
        from agentkit import bridge, egress, sandbox  # noqa: PLC0415

        return {"bridge": bridge, "sandbox": sandbox, "egress": egress}[argv[0]].main(argv[1:])
    a = ap.parse_args(argv)
    if a.cmd == "new":
        new(a.name, a.dest, a.git)
    elif a.cmd == "vendor":
        vendor(a.dest)
    elif a.cmd == "eval":
        sys.exit(eval_cmd(a))
    elif a.cmd == "replay":
        sys.exit(replay_cmd(a))


if __name__ == "__main__":
    main()
