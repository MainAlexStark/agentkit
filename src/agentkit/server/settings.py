"""Настройки сервиса из окружения. Префикс у всех переменных — AGENT_.

Полный список с комментариями — deploy/agent.env.example.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    name: str = "Агент"
    description: str = ""  # одна строка под приветствием на пустом экране
    suggestions: str = ""  # примеры запросов на пустом экране, через «|»
    # Подписка по умолчанию (меню пользователя → «Подписка»). Настоящий биллинг —
    # Domain.plan в проекте (ЮKassa и т. п., как в OpenSeller).
    plan_name: str = ""
    monthly_budget_usd: float = 0  # 0 — без лимита; иначе полоса расхода в «Подписке»
    billing_url: str = ""  # «Управлять подпиской» — страница оплаты хост-приложения
    help_url: str = ""  # пункт «Помощь» в меню пользователя
    public_url: str = "http://localhost:8080"
    host: str = "0.0.0.0"
    port: int = 8080
    database_url: str = "sqlite:///data/agent.db"
    data_dir: Path = Path("data")
    tz: str = "Europe/Moscow"

    # ── модели ──
    openrouter_api_key: str = ""
    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    openai_api_key: str = ""
    ollama_url: str = ""
    model_main: str = "openrouter:anthropic/claude-sonnet-4.5"
    model_small: str = "openrouter:anthropic/claude-haiku-4.5"
    # Модели в выпадающем списке чата, через запятую. Пусто — только main.
    models: str = ""
    # Запасные: "openrouter:a=openrouter:b|yandex:c;…"
    fallbacks: str = ""
    context_window: int = 200_000
    max_steps: int = 30
    reasoning_effort: str = ""

    # ── инструменты ──
    tavily_api_key: str = ""
    enable_files: bool = True
    enable_shell: bool = False  # только в изолированном контейнере!
    enable_web: bool = True
    mcp_config: Path = Path("mcp.json")
    skills_dir: Path = Path("app/skills")
    workspace_dir: Path | None = None  # по умолчанию data_dir/workspaces

    # ── права ──
    default_mode: str = "default"
    allow_rules: str = ""  # через запятую: "shell(git status*),write_file(notes/*)"
    deny_rules: str = ""
    ask_rules: str = ""

    # ── доступ ──
    # Пользователи с токенами: "id:токен:роль1+роль2,id2:токен2:роль". Роль admin — всё.
    tokens: str = ""
    # Токен доверенного хост-приложения: с ним пользователь передаётся заголовками
    # X-Agent-User-Id / X-Agent-User-Name / X-Agent-Roles (EI_doc, OpenSeller и т. п.).
    service_token: str = ""
    allowed_origins: str = ""

    trace_dir: Path | None = None
    # Несколько процессов (uvicorn --workers N или несколько контейнеров) — только с Redis.
    redis_url: str = ""
    workers: int = 1
    # Модели, которым отдавать картинки (шаблоны через запятую); пусто — встроенный список.
    vision_models: str = ""
    max_upload_mb: int = 25
    scheduler: bool = True
    max_output_chars: int = 30_000

    def model_list(self) -> list[str]:
        items = [m.strip() for m in self.models.split(",") if m.strip()]
        return items or [self.model_main]

    def fallback_map(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for part in self.fallbacks.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = [x.strip() for x in v.split("|") if x.strip()]
        return out


def split_rules(s: str) -> list[str]:
    # Запятая внутри скобок правила — часть шаблона, а не разделитель.
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            if cur.strip():
                out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out
