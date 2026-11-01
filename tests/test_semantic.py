"""Смысловой поиск: векторный индекс, гибрид со словами, память в промпте, файлы проекта."""

from __future__ import annotations

import os

import httpx
import pytest
from conftest import collect, make_agent

from agentkit import AgentConfig, FakeLLM, SQLStore, User
from agentkit.llm.openai_compat import OpenAICompatConfig
from agentkit.projects import ProjectLibrary, project_tools
from agentkit.semantic import OpenAIEmbedder, VectorIndex, fuse, keyword_rank, rank_memory, stems
from agentkit.tools.base import ToolContext

# «Смысл» для тестов: слова одного понятия — одно измерение. Синонимы без общих букв
# совпадают по вектору и не совпадают по словам — ровно то, чего не умел поиск по словам.
CONCEPTS = {
    "car": {"машин", "авто", "автом", "камри", "тачка", "toyot"},
    "food": {"еда", "ест", "аллер", "орехи", "орехо", "обед"},
    "pet": {"кот", "кошка", "собак", "питом", "барси"},
    "meter": {"поверк", "калиб", "счётч", "счетч", "прибо", "метро"},
}


class ConceptEmbedder:
    def __init__(self, model: str = "test:concepts"):
        self.model = model
        self.calls = 0
        self.texts = 0
        self.fail = False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("модель эмбеддингов недоступна")
        self.calls += 1
        self.texts += len(texts)
        out = []
        for t in texts:
            st = stems(t)
            v = [sum(1.0 for w in st if any(w.startswith(c) for c in cs)) for cs in CONCEPTS.values()]
            out.append([*v, 0.01])  # не нулевой — у пустого смысла косинус не определён
        return out


def test_fuse_prefers_items_high_in_both():
    order = [k for k, _ in fuse(["a", "b", "c"], ["c", "a"])]
    assert order[:2] == ["a", "c"]
    assert keyword_rank("поверка счётчиков", ["про поверки", "про котов"])[0][0] == 0


async def test_index_syncs_lazily_and_follows_deletes(store):
    emb = ConceptEmbedder()
    idx = await VectorIndex(store, emb).init()
    a = await store.memory_add("user:u1", "Ездит на Toyota Камри 2019")
    b = await store.memory_add("user:u1", "Аллергия на орехи")
    texts = {a.id: a.text, b.id: b.text}

    def load(ref):
        return [(0, texts[ref])]

    assert await idx.sync("user:u1", texts, load) == 2
    assert await idx.sync("user:u1", texts, load) == 0  # ничего не поменялось — модель не зовётся
    assert emb.calls == 1
    hits = await idx.search(["user:u1"], "какая у меня машина?", k=1)
    assert hits[0].ref == a.id and hits[0].score > 0.9
    assert await idx.search(["user:u2"], "машина") == []  # чужая область не видна

    # правка текста — старый вектор стирается сразу, новый считается при следующем sync
    await store.memory_update(b.id, "Кот Барсик")
    assert [h.ref for h in await idx.search(["user:u1"], "аллергия орехи", k=5)] == [a.id]
    texts[b.id] = "Кот Барсик"
    assert await idx.sync("user:u1", texts, load) == 1
    # удаление — вектора нет и без sync: «забудь» не оставляет текст в индексе
    await store.memory_delete(a.id)
    assert {h.ref for h in await idx.search(["user:u1"], "машина", k=5)} == {b.id}

    # другая модель — всё пересчитывается, старые векторы не смешиваются с новыми
    idx2 = await VectorIndex(store, ConceptEmbedder("test:other")).init()
    del texts[a.id]
    assert await idx2.sync("user:u1", texts, load) == 1
    assert await idx.search(["user:u1"], "кот") == []


async def test_rank_memory_finds_meaning_and_falls_back_to_words(store):
    emb = ConceptEmbedder()
    idx = await VectorIndex(store, emb).init()
    car = await store.memory_add("user:u1", "Ездит на Toyota Камри 2019")
    await store.memory_add("user:u1", "Аллергия на орехи")
    code = await store.memory_add("global", "Отчёты по МИ 1592-2015")
    found = await rank_memory(store, idx, ["global", "user:u1"], "что у меня за автомобиль", k=1)
    assert found[0].id == car.id
    # по словам «автомобиль» не находит «Камри» — именно это и чинит смысловой поиск
    assert await rank_memory(store, None, ["global", "user:u1"], "что у меня за автомобиль") == []
    # точный код — словами, даже когда модель эмбеддингов упала
    emb.fail = True
    assert (await rank_memory(store, idx, ["global", "user:u1"], "МИ 1592-2015"))[0].id == code.id


async def test_big_memory_goes_to_prompt_by_relevance(store):
    emb = ConceptEmbedder()
    idx = await VectorIndex(store, emb).init()
    for i in range(30):
        await store.memory_add("user:u1", f"Заметка про погоду номер {i}: " + "дождь " * 10)
    car = await store.memory_add("user:u1", "Ездит на Toyota Камри 2019")
    for i in range(30):
        await store.memory_add("user:u1", f"Ещё заметка {i}: " + "ветер " * 10)
    fake = FakeLLM(default="ок")
    agent = make_agent(store, fake, index=idx, config=AgentConfig(memory_inline_chars=1500))
    s = await store.create_session("u1")
    await collect(agent.run(s.id, User(id="u1"), "Когда менять масло в моей машине?"))
    req = fake.requests[-1]
    system = req.messages[0].text
    assert car.text in system  # старая, но нужная к вопросу
    assert "Ещё заметка 29" in system  # самые новые — тоже
    assert "Заметка про погоду номер 3:" not in system
    assert "memory_search" in system
    assert "memory_search" in {t["function"]["name"] for t in req.tools}

    search = next(t for t in agent.tools.all() if t.name == "memory_search")
    ctx = ToolContext(session_id=s.id, run_id="r", user=User(id="u1"), agent=agent, store=store, emit=None)
    res = await search.call({"query": "тачка"}, ctx)
    assert res.content.splitlines()[0].endswith(car.text)


async def test_small_memory_stays_whole_and_search_hidden(store):
    await store.memory_add("user:u1", "Зовут Алекс")
    fake = FakeLLM(default="ок")
    agent = make_agent(store, fake)
    s = await store.create_session("u1")
    await collect(agent.run(s.id, User(id="u1"), "привет"))
    req = fake.requests[-1]
    assert "Зовут Алекс" in req.messages[0].text and "memory_search" not in req.messages[0].text
    assert "memory_search" not in {t["function"]["name"] for t in req.tools}


async def test_project_search_hybrid(store, tmp_path):
    emb = ConceptEmbedder()
    idx = await VectorIndex(store, emb).init()
    lib = ProjectLibrary(store, tmp_path, inline_limit=10, index=idx)
    p = await store.project_create("u1", "Метрология")
    await lib.add_file(p.id, "регламент.txt", "Калибровка приборов проводится раз в год.\n".encode())
    await lib.add_file(p.id, "кадры.txt", "Отпуск согласуется с руководителем за две недели.\n".encode())
    await lib.add_file(p.id, "коды.txt", "Методика МИ 1592-2015 для водомеров.\n".encode())
    s = await store.create_session("u1", project_id=p.id)
    tool = next(t for t in project_tools(lib) if t.name == "project_search")
    ctx = ToolContext(session_id=s.id, run_id="r", user=User(id="u1"), agent=None, store=store, emit=None)
    res = await tool.call({"query": "как часто поверка счётчиков"}, ctx)
    assert res.data[0]["file"] == "регламент.txt"  # ни одного общего слова с запросом
    res = await tool.call({"query": "МИ 1592-2015"}, ctx)
    assert res.data[0]["file"] == "коды.txt"
    emb.fail = True  # модель упала — остаются слова, а не ошибка
    res = await tool.call({"query": "отпуск"}, ctx)
    assert res.data[0]["file"] == "кадры.txt"
    # файл удалён — его векторов больше нет
    emb.fail = False
    f = await lib.find_file(p.id, "регламент.txt")
    await lib.delete_file(p.id, f.id)
    assert all(h.ref != f.id for h in await idx.search([f"files:{p.id}"], "поверка", k=10))


async def test_openai_embedder_batches_and_retries():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(req.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(503)
        n = len(body["input"])
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [float(i), 1.0]} for i in reversed(range(n))],
                                         "usage": {"total_tokens": 3 * n, "cost": 0.0001}})

    cfg = OpenAICompatConfig(name="openrouter", base_url="https://x/api/v1", api_key="k")
    emb = OpenAIEmbedder(cfg, "openai/text-embedding-3-small", batch=2, dimensions=256,
                         client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    import agentkit.semantic as sem

    async def nosleep(_):
        return None

    orig, sem.asyncio.sleep = sem.asyncio.sleep, nosleep
    try:
        out = await emb.embed(["a", "", "c"])
    finally:
        sem.asyncio.sleep = orig
    assert out == [[0.0, 1.0], [1.0, 1.0], [0.0, 1.0]]  # порядок по index, а не по ответу
    assert seen[1]["input"] == ["a", " "] and seen[1]["dimensions"] == 256
    assert emb.model == "openrouter:openai/text-embedding-3-small@256" and emb.tokens == 9


@pytest.mark.skipif(not os.environ.get("AGENTKIT_TEST_PG"), reason="AGENTKIT_TEST_PG=postgresql://… с pgvector")
async def test_pgvector_live():
    store = await SQLStore(os.environ["AGENTKIT_TEST_PG"]).init()
    try:
        idx = await VectorIndex(store, ConceptEmbedder()).init()
        assert idx.pgvector
        scope = "user:pgtest"
        for m in await store.memory_list([scope]):
            await store.memory_delete(m.id)
        car = await store.memory_add(scope, "Ездит на Toyota Камри 2019")
        await store.memory_add(scope, "Кот Барсик")
        found = await rank_memory(store, idx, [scope], "моя машина", k=1)
        assert found[0].id == car.id
        hits = await idx.search([scope], "питомец", k=5)
        assert hits[0].text == "Кот Барсик" and hits[0].score > 0.9
        await store.memory_delete(car.id)
        assert all(h.ref != car.id for h in await idx.search([scope], "машина", k=5))
    finally:
        await store.close()
