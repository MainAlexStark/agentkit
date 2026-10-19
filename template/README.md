# agent-service

Агент на [agentkit](https://github.com/MainAlexStark/agentkit): веб-чат, API для других сервисов, MCP, подтверждения действий, память, расписание.

## Запуск локально

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest pytest-asyncio
cp deploy/agent-service.env.example .env   # AGENT_OPENROUTER_API_KEY=…, AGENT_TOKENS=me:секрет:admin
uvicorn app.main:app --reload --port 8080
```

http://localhost:8080 → войти токеном `секрет`.

## Проверка

```bash
pytest -q
agentkit eval evals/ --app app.evals:make_agent --offline   # бесплатно, для CI
agentkit eval evals/ --app app.evals:make_agent             # на настоящей модели
```

## Развёртывание на VPS

```bash
./deploy.sh agent.example.com    # первый раз: спросит ключ модели, напечатает токены
./deploy.sh update               # обновить (с откатом при неудаче)
./deploy.sh token ivan worker    # токен сотруднику
./deploy.sh secrets | logs | status | backup | restart | down
```

## API для других сервисов

```
POST /api/run
Authorization: Bearer <AGENT_SERVICE_TOKEN>
X-Agent-User-Id: 42
X-Agent-User-Name: Иван%20Петров
X-Agent-Roles: worker

{"text": "Найди заметки про воду"}
→ {"session_id": "...", "status": "done", "text": "...", "usage": {...}, "approvals": [...]}
```

Живой чат: `POST /api/sessions` → `POST /api/sessions/{id}/messages` → `GET /api/sessions/{id}/events` (SSE) или `/poll?after=<seq>`; подтверждения — `POST /api/approvals/{id}` `{"approve": true}`.

## Структура

| Путь | Что |
|---|---|
| `app/domain/prompt.md` | поведение агента |
| `app/domain/tools.py` | доменные инструменты |
| `app/domain/hooks.py` | доменные правила и аудит |
| `app/skills/` | skills |
| `mcp.json` | MCP-серверы |
| `evals/` | проверки поведения |
| `vendor/agentkit` | ядро (обновление: `agentkit vendor`) |
