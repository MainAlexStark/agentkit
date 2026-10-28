# agentkit

Ядро агента (harness) и шаблон сервиса-агента. Чтобы каждый новый агент — для EI_doc, OpenSeller, PrintPipe, чего угодно — начинался с предметной части, а не с пятой реализации цикла, прав и чата.

```
agentkit new ei-assistant        # готовый проект: сервис, веб-чат, deploy.sh, тесты, evals
```

В новом проекте пишутся только **промпт**, **доменные инструменты**, **mcp.json** и **evals**. Цикл, модели, MCP, права и подтверждения, память, skills, субагенты, сжатие контекста, расписание, проекты, мост к компьютеру пользователя, песочница для кода, трейсы, HTTP API и веб-чат приходят из пакета.

## Что внутри

| Модуль | Что делает |
|---|---|
| `agent.py` | Цикл: модель → инструменты → права/хуки → результат. Параллельные read-only вызовы, подтверждение без висящего процесса, отмена, лимит шагов с обязательным итогом, защита от зацикливания |
| `llm/` | OpenAI-совместимый клиент (OpenRouter, Yandex AI Studio, OpenAI, Ollama), «провайдер:модель», роли main/small, повторы, запасные модели, prompt caching для Claude, стоимость |
| `tools/` | `@tool` из сигнатуры функции; базовые: задачи, вопрос человеку, файлы, shell, web, память; реестр с отложенной загрузкой (`tool_search`) |
| `mcp.py` | Клиент MCP без SDK: Streamable HTTP и stdio, Bearer или OAuth; инструменты сервера → `mcp__<сервер>__<имя>` с read-only/destructive из аннотаций |
| `permissions.py` | Режимы (в интерфейсе: вручную / автоматически / без вопросов), песочница как зона агента, правила `allow/ask/deny` вида `shell(git status*)` и выбор пользователя по инструменту |
| `hooks.py` | session_start, user_prompt_submit, pre/post_tool_use, stop, pre_compact |
| `context.py` | Системный промпт слоями под кэш, сжатие истории, обрезка вывода |
| `skills.py` | SKILL.md-пакеты: в промпте оглавление, текст — по запросу; свои навыки пользователя — в базе |
| `subagents.py` | `task`: explore / plan / general в своих сессиях, параллельно |
| `store/` | SQLite / PostgreSQL: сессии, сообщения, прогоны, события, подтверждения, память, расписание |
| `runs.py` | Прогоны в фоне, SSE с курсором, один прогон на сессию, сообщения на ходу |
| `bus.py` | Шина между процессами (Redis): блокировки, живые события, отмена, общие команды |
| `oauth.py` | Вход в MCP-серверы по OAuth 2.1: DCR, PKCE, обновление и отзыв токенов |
| `attachments.py` | Файлы и картинки в сообщениях: загрузка, текст из PDF/DOCX/XLSX, картинки для моделей со зрением |
| `scheduler.py` | Задачи по cron из базы |
| `evals.py` | YAML-кейсы: рабочая папка и проверка файлов после, несколько ходов, ответы на вопросы, история, память; офлайн-сценарии для CI и живые модели с судьёй; несколько моделей, повторы, pass^k, сравнение с baseline, сводка в Markdown |
| `bench.py`, `evals/harness/` | Набор реальных задач для самого ядра (32 кейса: файлы, shell, план, подтверждения, инъекции, сжатие, память, субагенты) и агент для него на дешёвых моделях; прогон в CI по кнопке |
| `replay.py` | `agentkit replay`: ход прогона по журналу (база или JSONL) шаг за шагом; прогон → eval-кейс |
| `tracing.py` | Лог, JSONL, OpenTelemetry |
| `server/` | FastAPI: API, авторизация (личные токены и сервисный токен хоста), веб-чат |
| `template/` | Шаблон проекта: `app/domain` (промпт, инструменты, хуки), skills, evals, Dockerfile, compose, `deploy.sh` стандарта |

Подробно: [docs/architecture.md](docs/architecture.md) · что сделано и чего нет: [docs/checklist.md](docs/checklist.md) · как сделать нового агента: [docs/new-agent.md](docs/new-agent.md)

## Быстрый старт (разработка)

```bash
git clone … agentkit && cd agentkit
python -m venv .venv && . .venv/bin/activate
pip install -e ".[service,dev]"
pytest                                   # ядро
cd template && pytest                    # сервис-шаблон

cp deploy/agent-service.env.example .env # впишите AGENT_OPENROUTER_API_KEY и AGENT_TOKENS=me:секрет:admin
uvicorn app.main:app --reload --port 8080
```

Открыть http://localhost:8080, войти токеном.

## Качество: evals и replay

```bash
agentkit eval evals/harness --app agentkit.bench:make_agent --budget 0.5 --parallel 8 \
    --baseline evals/baseline.json --md - --save-runs evals/runs   # ядро на DeepSeek Flash: что сломалось против baseline
agentkit replay template/data/agent.db --list         # последние прогоны сервиса
agentkit replay template/data/agent.db r_…            # ход прогона: шаги, вызовы, результаты, стоимость
agentkit replay template/data/agent.db r_… --to-eval evals/harness/regress.yaml --judge "…"   # провал → кейс
```

Подробно — [evals/README.md](evals/README.md).

## Ядро как библиотека

```python
from agentkit import Agent, AgentSpec, SQLStore, User, make_router, standard_tools, tool

@tool(read_only=True)
async def stock(ctx, sku: str) -> str:
    """Остаток товара на складе."""
    return "12 шт."

store = await SQLStore("sqlite:///agent.db").init()
agent = Agent(
    router=make_router(openrouter_key=KEY, main="openrouter:anthropic/claude-sonnet-4.5"),
    store=store,
    tools=standard_tools(files=False, extra=[stock]),
    spec=AgentSpec(prompt="Ты — помощник склада."),
)
s = await store.create_session("u1")
async for ev in agent.run(s.id, User(id="u1"), "Сколько осталось A-15?"):
    print(ev.type, ev.data)
```

## Встраивание в существующее приложение

* **По HTTP** (любой язык, в т. ч. Go — OpenSeller): агент — отдельный контейнер в той же `mcp-network`; хост ходит с `AGENT_SERVICE_TOKEN` и заголовком `X-Agent-User-Id`. `POST /api/run` — задача и итог одним запросом; `POST /api/sessions/{id}/messages` + SSE — для живого чата.
* **Библиотекой** (Python — EI_doc): `Agent` + своя реализация `Store` поверх ORM хоста, свои инструменты, свой UI на событиях.
* **Инструменты хоста** — лучше как MCP-сервер по стандарту `mcp-server-standard`: тогда ими пользуется и этот агент, и Claude.
