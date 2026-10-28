# evals ядра

`harness/` — набор реальных задач для самого agentkit: файлы и код, shell, план и вопросы,
подтверждения и режимы, устойчивость к инструкциям из данных, история и сжатие, обрезанный
вывод, память, субагенты, экономность. Гоняется на живых моделях через `agentkit.bench:make_agent`.

```bash
export AGENT_OPENROUTER_API_KEY=…
agentkit eval evals/harness --app agentkit.bench:make_agent \
    --model openrouter:anthropic/claude-sonnet-4.5 --model openrouter:anthropic/claude-haiku-4.5 \
    --repeat 2 --parallel 6 --json evals/out.json --md - --save-runs evals/runs
agentkit replay evals/runs/<модель>/files__fix-bug-and-verify__1.jsonl     # почему упал
agentkit eval evals/harness --app agentkit.bench:make_agent --baseline evals/out.json   # после правки: что сломалось
```

Ночной прогон — `.github/workflows/evals-nightly.yml` (нужен секрет `OPENROUTER_API_KEY`).

Правила для новых кейсов:

* Сначала механическая проверка (`files`, `contains`, `tools_called`, `regex`), судья — только где без него никак.
  Судья вызывается, только если механика прошла.
* Кейс проверяет одно свойство харнесса; если упал — по id должно быть понятно, куда смотреть.
* Ответ должен однозначно проверяться: точные числа, уникальные маркеры (`E-4471`, `DONE-7731`).
* Провал из прода → `agentkit replay data/agent.db r_… --to-eval evals/harness/regress.yaml --judge "…"`.
