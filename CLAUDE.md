# agentkit — заметки для Claude

Ядро агента (`src/agentkit`) и шаблон сервиса (`template/`). Язык кода — Python 3.11+, комментарии и тексты — по-русски.

## Правила

- **Граница ядро/проект.** В ядро — только то, что одинаково у любого агента. Предметное — в `template/app/domain` (пример) или в проект.
- **Ядро не зависит от FastAPI** везде, кроме `agentkit/server`. `agent.py`, `tools/`, `llm/`, `store/` должны работать как библиотека.
- **Безопасность — кодом.** Всё, что важно (права, подтверждения, рабочая папка, SSRF), закрывается в коде; промпт — не граница.
- **Ошибка инструмента — текст для модели** (`ToolResult.error`), не исключение цикла. Тексты ошибок для человека называют, что и где править.
- **Комментарии объясняют «почему»** и называют поломку, из-за которой код написан именно так.
- **Новое событие** = значение в `EventType` + обработка в `server/static/app.js`.
- **Новая настройка сервиса** = поле в `server/settings.py` + строка в `template/deploy/agent-service.env.example`.

## Проверка перед коммитом

```bash
ruff check src tests template/app template/tests
pytest -q
cd template && pytest -q && agentkit eval evals/ --app app.evals:make_agent --offline
agentkit new smoke-agent --dest /tmp && (cd /tmp/smoke-agent && pip install -r requirements.txt -q && pytest -q)
```

UI проверяется Playwright-скриптом против сервиса с `FakeLLM` (см. `template/tests/test_service.py` — как подменить модели через `AgentService(router=…)`).

## Где что

`docs/architecture.md` — устройство и решения · `docs/checklist.md` — что сделано (✅/🟡/❌), обновлять при каждом изменении возможностей · `docs/new-agent.md` — как делать нового агента.
