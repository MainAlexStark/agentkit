"""Маскирование секретов: ни модели, ни в журнал, ни в blob, ни в логи (redact.py)."""

from __future__ import annotations

import json
import logging

from conftest import collect, make_agent, status

from agentkit import AgentConfig, FakeLLM, standard_tools
from agentkit.redact import MARKER, Redactor, contains_marker, install_log_redaction, remember

OR_KEY = "sk-or-v1-" + "3f9a" * 16
GH = "ghp_" + "Ab3" * 12


def test_patterns_and_false_positives():
    r = Redactor()
    masked = {
        f"key {OR_KEY}": "openrouter-key",
        f"token={GH}": "github-token",
        "PASSWORD=hunter2hunter": "PASSWORD",
        '"client_secret": "Zx81kdlLmmA"': "client_secret",
        "Authorization: Bearer abcdefghijklmnop1234": "authorization",
        "postgres://app:pa55word@db:5432/x": "пароль в URL",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----": "private-key",
    }
    for src, label in masked.items():
        out = r.text(src)
        assert f"{MARKER}: {label}]" in out, (src, out)
    assert "app:" in r.text("postgres://app:pa55word@db/x")  # имя пользователя остаётся
    # Код, числа, хэши и пути — не секреты: иначе edit_file не нашёл бы прочитанную строку.
    for keep in ["api_key=settings.openrouter_key", "max_tokens: 100000", "tokenizer: cl100k_base",
                 "commit 3f2a9c1d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c", "TOKEN_TTL=3600", "password: str = ''",
                 "def get_token(self) -> str:", "SECRET_FILE=/run/secrets/db1", "https://example.com/a:b@c"]:
        assert r.text(keep) == keep, keep
    once = r.text(f"PASSWORD=hunter2hunter {OR_KEY}")
    assert r.text(once) == once  # повторная маскировка ничего не портит


def test_known_values_from_env():
    r = Redactor(patterns=False, env={"OPENROUTER_API_KEY": "plainvalue-1234", "PWD": "/home/alex/project",
                                      "AGENT_TOKENS": "alex:alexs-long-token-77:admin,bob:bobs-long-token-88:user",
                                      "HOME": "/home/alex", "AGENT_PORT": "8080"})
    assert r.text("x plainvalue-1234 y") == f"x {MARKER}: OPENROUTER_API_KEY] y"
    assert r.text("cd /home/alex/project") == "cd /home/alex/project"  # PWD — не секрет
    assert "bobs-long-token-88" not in r.text("Authorization: bobs-long-token-88")  # части списка токенов
    assert r.text("alex admin") == "alex admin"  # короткие части — нет
    assert r.text(f"PASSWORD=hunter2hunter {OR_KEY}") == f"PASSWORD=hunter2hunter {OR_KEY}"  # шаблоны выключены


def test_base_and_deep():
    base = Redactor(patterns=False)
    r = Redactor(patterns=False, base=base)
    base.add("oauth-access-xyz123", "OAuth access")  # пришёл на ходу — виден и агентскому Redactor
    data = {"a": ["oauth-access-xyz123", 5, {"b": ("oauth-access-xyz123",)}], "n": None}
    out = r.deep(data)
    assert json.dumps(out, ensure_ascii=False).count("OAuth access") == 2 and out["n"] is None
    assert data["a"][0] == "oauth-access-xyz123"  # оригинал не тронут
    assert contains_marker(out) and not contains_marker(data)


def test_logging_redaction(caplog):
    remember("log-secret-value-42", "тест")
    install_log_redaction()
    log = logging.getLogger("agentkit.test.redact")
    with caplog.at_level(logging.INFO):
        log.info("ключ %s", "log-secret-value-42")
        try:
            raise RuntimeError("boom log-secret-value-42")
        except RuntimeError:
            log.exception("упало")
    assert "log-secret-value-42" not in caplog.text
    assert "log-secret-value-42" not in "".join(r.exc_text or "" for r in caplog.records)


async def test_tool_result_masked_for_model_journal_and_blob(store, user, tmp_path):
    (tmp_path / ".env").write_text(f"OPENROUTER_API_KEY={OR_KEY}\nSERVICE_KEY=svc-key-0000-1111\nPORT=8000\n")
    (tmp_path / "big.txt").write_text("x" * 5000 + f"\n{GH}\n")
    seen: list[str] = []

    def script(req):
        seen.append(json.dumps([m.text for m in req.messages], ensure_ascii=False))
        return "Готово"

    fake = FakeLLM([{"tool_calls": [("read_file", {"path": ".env"}), ("read_file", {"path": "big.txt"})]}, script])
    r = Redactor(env={"SERVICE_KEY": "svc-key-0000-1111"})
    agent = make_agent(store, fake, tools=standard_tools(subagents=False), workspace=tmp_path, redactor=r,
                       config=AgentConfig(max_output_chars=1000))
    s = await store.create_session(user.id)
    evs = await collect(agent.run(s.id, user, "что в .env и big.txt?"))
    assert status(evs) == "done"
    model_saw = seen[-1]
    assert "PORT=8000" in model_saw and f"{MARKER}: SERVICE_KEY]" in model_saw
    journal = json.dumps([e.data for e in await store.list_events(session_id=s.id)], ensure_ascii=False)
    history = json.dumps([m.text for _, m in await store.list_messages(s.id)], ensure_ascii=False)
    ids = [bid for bid, _ in await store.blob_list(s.id, "tool_output")]
    assert ids
    blobs = "".join([(await store.blob_get(bid))[0] for bid in ids])
    for secret in (OR_KEY, "svc-key-0000-1111", GH):
        for where in (model_saw, journal, history, blobs):
            assert secret not in where
    assert "read_output" in model_saw  # big.txt обрезан, а полный текст в blob — тоже без секрета


async def test_write_with_marker_refused_edit_around_works(store, user, tmp_path):
    cfg = tmp_path / "config.py"
    cfg.write_text(f'API_TOKEN = "{GH}"\nPORT = 8000\n')
    masked = f'API_TOKEN = "{MARKER}: github-token]"\nPORT = 9000\n'
    fake = FakeLLM([
        {"tool_calls": [("write_file", {"path": "config.py", "content": masked})]},
        {"tool_calls": [("edit_file", {"path": "config.py", "old_string": "PORT = 8000", "new_string": "PORT = 9000"})]},
        "Готово",
    ])
    agent = make_agent(store, fake, tools=standard_tools(subagents=False), workspace=tmp_path)
    s = await store.create_session(user.id, state={"mode": "bypass"})
    evs = await collect(agent.run(s.id, user, "порт 9000"))
    results = [e.data for e in evs if str(e.type) == "tool_result"]
    assert results[0]["is_error"] and "метка, а не значение" in results[0]["content"]
    assert not results[1]["is_error"]
    assert cfg.read_text() == f'API_TOKEN = "{GH}"\nPORT = 9000\n'  # секрет в файле цел


def test_settings_secret_env():
    from agentkit.server.settings import Settings

    s = Settings(_env_file=None, openrouter_api_key="or-key-123456789", tokens="alex:tok-abcdef123456:admin")
    r = Redactor(patterns=False, env=s.secret_env())
    assert MARKER in r.text("or-key-123456789") and MARKER in r.text("tok-abcdef123456")
    assert r.text("Агент alex admin") == "Агент alex admin"
