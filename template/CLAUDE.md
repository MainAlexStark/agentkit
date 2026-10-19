# agent-service — заметки для Claude

Агент на ядре agentkit (`vendor/agentkit` — копия ядра, руками не править; обновление — `agentkit vendor`).

## Что здесь писать

- `app/domain/prompt.md` — поведение агента (только предметная часть; общие правила — в ядре).
- `app/domain/tools.py` — доменные инструменты (`@tool`). Читающие — `read_only=True`; пишущие — с `describe=` для карточки подтверждения; опасные — `destructive=True`. Права пользователя — внутри инструмента.
- `app/domain/hooks.py` — доменные запреты и аудит.
- `app/domain/__init__.py` — сборка `Domain`: промпт, инструменты, хуки, субагенты, `rules(user)`, `deps`.
- `app/skills/<имя>/SKILL.md` — длинные процедуры.
- `mcp.json` — внешние MCP-серверы.
- `evals/*.yaml` — кейсы поведения. Каждый новый пишущий инструмент — офлайн-кейс со `script`.

## Проверка

```bash
ruff check app tests && pytest -q && agentkit eval evals/ --app app.evals:make_agent --offline
```

## Развёртывание

`./deploy.sh <домен>` на VPS, `./deploy.sh update` — обновление с откатом. Стандарт тот же, что у MCP-серверов (общий Caddy, `mcp-network`).
