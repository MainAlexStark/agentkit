"""agentkit — командная строка.

  agentkit new <имя> [--dest DIR] [--git]     новый проект-агент из шаблона
  agentkit vendor [--dest DIR]                 обновить vendor/agentkit в проекте
  agentkit eval <путь> --app mod:fn [--offline] [--only ID] [--json FILE]
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


def eval_cmd(path: Path, app: str, offline: bool, only: str | None, out: str | None) -> int:
    from agentkit.evals import load_suite, run_case

    sys.path.insert(0, str(Path.cwd()))
    mod, _, fn = app.partition(":")
    make_agent = getattr(importlib.import_module(mod), fn or "make_agent")
    files = sorted(path.glob("*.yaml")) + sorted(path.glob("*.yml")) if path.is_dir() else [path]

    async def go() -> list[dict]:
        reports = []
        for f in files:
            name, cases = load_suite(f)
            results = []
            for case in cases:
                if only and case.get("id") != only:
                    continue
                if offline and not case.get("script"):
                    continue
                results.append(await run_case(case, make_agent))
            from agentkit.evals import Report

            rep = Report(suite=name, results=results)
            print(rep.table())
            reports.append(rep.to_dict())
        return reports

    reports = asyncio.run(go())
    if out:
        Path(out).write_text(json.dumps(reports, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    failed = sum(r["total"] - r["passed"] for r in reports)
    return 1 if failed else 0


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
    p_e.add_argument("--only")
    p_e.add_argument("--json")
    a = ap.parse_args(argv)
    if a.cmd == "new":
        new(a.name, a.dest, a.git)
    elif a.cmd == "vendor":
        vendor(a.dest)
    elif a.cmd == "eval":
        sys.exit(eval_cmd(a.path, a.app, a.offline, a.only, a.json))


if __name__ == "__main__":
    main()
