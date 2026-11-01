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
    # Лимиты расходов на пользователя, $ (0 — без лимита). Месяц — календарный, день — с 00:00
    # по AGENT_TZ. Исчерпан — новые сообщения не принимаются (402), идущий прогон останавливается
    # перед следующим запросом к модели. Domain.plan может задать свои limit_usd / daily_limit_usd.
    monthly_budget_usd: float = 0
    daily_budget_usd: float = 0
    budget_exempt_roles: str = "admin"  # кого лимиты не касаются, через запятую; пусто — всех касаются
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
    # Избранные модели по умолчанию (пока пользователь не выбрал свои), через запятую. Пусто — только main.
    models: str = ""
    # Какие модели каталога доступны пользователям: шаблоны через запятую
    # («openrouter:anthropic/*,openrouter:openai/gpt-5*»); пусто — все с поддержкой инструментов.
    # Админ может сузить список в настройках чата — тогда действует его выбор.
    models_allow: str = ""
    # Запасные: "openrouter:a=openrouter:b|yandex:c;…"
    fallbacks: str = ""
    context_window: int = 200_000
    max_steps: int = 150  # 30 не хватало на сборку приложения: прогон обрывался на середине
    reasoning_effort: str = ""

    # ── инструменты ──
    tavily_api_key: str = ""
    enable_files: bool = True
    enable_shell: bool = False  # только в изолированном контейнере!
    enable_web: bool = True
    mcp_config: Path = Path("mcp.json")
    # Личные MCP-серверы: каждый пользователь подключает свои (видны и работают только у него).
    user_mcp: bool = True
    user_mcp_max: int = 10  # сколько личных серверов у одного пользователя
    # Личным серверам — во внутреннюю сеть и по http. По умолчанию нельзя: адрес вводит не админ,
    # а запрос делает сервис изнутри своей сети (SSRF: Redis, база, metadata облака). Только для разработки.
    user_mcp_private: bool = False
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
    # Компьютер пользователя через мост (bridge.py) — инструменты pc_*.
    enable_devices: bool = True
    # Проекты: инструкции, файлы и память для группы разговоров.
    enable_projects: bool = True
    project_inline_chars: int = 40_000  # столько текста файлов проекта кладётся прямо в промпт
    max_project_file_mb: int = 50
    # Компьютер агента — run_python / run_bash. Пусто — выключен; http://sandbox:8090 — сервис sandbox
    # (контейнер на разговор); "local" — процессы на этой машине, только для разработки: код видит
    # всё, что видит сервис (файлы, .env), поэтому по умолчанию не включается.
    sandbox_url: str = ""
    sandbox_token: str = ""
    # Куда песочнице можно в интернет (через прокси egress): домены через запятую, «*» — всюду, пусто — никуда.
    # Сервис только сообщает это модели; пропускает трафик сам прокси (deploy.sh передаёт ему тот же список).
    sandbox_egress: str = ""
    # Превью серверов песочницы (sandbox_preview): куда сервису ходить к контейнерам разговоров —
    # обратный порт egress (deploy.sh: http://egress:3129). Для "local" не нужен; пусто — превью выключено.
    sandbox_preview_url: str = ""
    # Правки файлов в папке разговора (write_file / edit_file) — без подтверждения: это рабочая папка
    # агента, run_python и так пишет туда без вопросов, а откат к сообщению возвращает файлы.
    workspace_edits: bool = True
    max_output_chars: int = 30_000
    # Секреты сервиса (ключи, токены, пароли из этих настроек и окружения) скрываются всегда. Это — ещё и
    # типовые форматы в данных (sk-…, ghp_…, PASSWORD=…, JWT, PEM): чужой ключ из .env пользователя тоже
    # не уходит провайдеру модели и в журнал. false — если агент по делу работает с такими данными.
    redact_patterns: bool = True
    # Фоновая память (как у Claude): после каждого хода модель small выбирает из слов человека
    # устойчивые факты в его личную память. Ещё один дешёвый запрос на ход; человек выключает у себя
    # в «Настройки → Память», false — выключить для всех.
    auto_memory: bool = True
    # Сколько символов памяти человека идёт в промпт целиком; больше — только ближайшее к его
    # сообщению и самое новое, остальное агент ищет через memory_search.
    memory_inline_chars: int = 6000
    # Смысловой поиск по памяти и файлам проектов: модель эмбеддингов «провайдер:модель»
    # (openrouter / openai / ollama). Пусто или нет ключа провайдера — поиск только по словам.
    # Векторы — в той же базе: PostgreSQL с pgvector ищет в базе, без него (и SQLite) — перебором.
    # bge-m3: многоязычная, 1024 измерения (HNSW pgvector берёт до 2000), ≈ $0.01 за 1M токенов; та же
    # модель есть в Ollama. text-embedding-3-small по-русски заметно слабее («погода в моём городе» не
    # находила «живёт в Кирове»).
    embed_model: str = "openrouter:baai/bge-m3"
    # Укоротить векторы (text-embedding-3, qwen3-embedding умеют); 0 — родная размерность. Больше 2000 —
    # pgvector ищет без индекса, перебором в базе.
    embed_dimensions: int = 0

    def secret_env(self) -> dict[str, str]:
        """Настройки в виде переменных окружения — для redact.Redactor.add_env (берёт KEY/TOKEN/SECRET)."""
        return {f"AGENT_{k.upper()}": v for k, v in self.model_dump().items() if isinstance(v, str) and v}

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
