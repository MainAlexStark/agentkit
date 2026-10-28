# evals ядра

`harness/` — набор реальных задач для самого agentkit: файлы и код, shell, план и вопросы,
подтверждения и режимы, устойчивость к инструкциям из данных, история и сжатие, обрезанный
вывод, память, субагенты, экономность. Гоняется на живых моделях через `agentkit.bench:make_agent`.

**Модели и деньги.** По умолчанию main — DeepSeek V4.1 Flash, small (судья, сжатие, explore) —
бесплатная Gemma 4 31B с запасной DeepSeek Flash: весь набор ≈ $0.1–0.2. Свои — через
`AGENTKIT_EVAL_MAIN` / `AGENTKIT_EVAL_SMALL` или `--model`. Сильная модель (Sonnet 4.5) стоит
≈ $0.7 за набор, с `--repeat 2` и второй моделью — в разы больше; поэтому всегда `--budget`.

```bash
export AGENT_OPENROUTER_API_KEY=…
agentkit eval evals/harness --app agentkit.bench:make_agent --budget 0.5 --parallel 8 \
    --json evals/out.json --md - --save-runs evals/runs
agentkit replay evals/runs/default/files__fix-bug-and-verify__1.jsonl      # почему упал
agentkit eval evals/harness --app agentkit.bench:make_agent --budget 0.5 --baseline evals/baseline.json   # после правки
```

В CI — `.github/workflows/evals-live.yml`, **только по кнопке** (Actions → evals-live → Run workflow):
модели, повторы и бюджет задаются в форме запуска. Нужен секрет `OPENROUTER_API_KEY`; без него
прогон пропускается. Расписания нет: каждый прогон стоит денег.

Правила для новых кейсов:

* Сначала механическая проверка (`files`, `contains`, `tools_called`, `regex`), судья — только где без него никак.
  Судья вызывается, только если механика прошла.
* Кейс проверяет одно свойство харнесса; если упал — по id должно быть понятно, куда смотреть.
* Ответ должен однозначно проверяться: точные числа, уникальные маркеры (`E-4471`, `DONE-7731`).
* Провал из прода → `agentkit replay data/agent.db r_… --to-eval evals/harness/regress.yaml --judge "…"`.
