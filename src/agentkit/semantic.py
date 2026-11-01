"""Смысловой поиск: эмбеддинги, векторный индекс и гибрид со словами.

Поиск по словам находит «поверка» по «поверки», но не находит «калибровку» по
«проверке точности прибора» и не понимает «что я говорил про машину» → «Камри
2019». Смысловой — понимает: текст и запрос превращаются в векторы, ближайшие
по косинусу — похожие по смыслу. Слова при этом не выбрасываются: точные коды,
номера и названия (МИ 1592-2015, артикул) векторы находят хуже слов. Итог —
слияние двух списков (RRF): наверху то, что высоко в обоих.

Где лежат векторы:
- PostgreSQL с расширением pgvector — колонка ``embedding vector`` и HNSW-индекс
  на каждую размерность; поиск — в базе (``<=>``).
- SQLite или PostgreSQL без pgvector — float32 в ``vec`` и перебор в Python.
  Для памяти и проектов на тысячи кусков этого хватает; расширение не
  обязательное, чтобы разработка и тесты не требовали своего Postgres.

Индекс догоняется лениво (``VectorIndex.sync``): перед поиском сравниваются
хэши — новое досчитывается, изменённое и удалённое убирается. Так не нужно
вставлять пересчёт в каждое место, где меняются память и файлы, а смена модели
эмбеддингов просто пересчитывает всё при следующем поиске. Удаление записи
памяти или файла стирает её векторы сразу, в той же транзакции (store/sql.py).

Модель эмбеддингов недоступна (нет ключа, сеть, 5xx) — поиск остаётся по словам,
а не падает: смысл — улучшение, а не условие работы.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import struct
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable, Hashable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy import delete, insert, select
from sqlalchemy import text as sql

from agentkit.llm.base import LLMError

log = logging.getLogger("agentkit.semantic")

# ── слова ────────────────────────────────────────────────────────────
_WORD = re.compile(r"\w{2,}", re.UNICODE)


def stems(text: str) -> list[str]:
    # Грубая «основа» для русского: первые 5 букв. «поверка/поверки/поверку» совпадут,
    # а точный морфоанализатор ради поиска по паре десятков файлов не нужен.
    return [w[:5] if len(w) > 5 else w for w in _WORD.findall(text.lower())]


def chunks(text: str, size: int = 1500, overlap: int = 200) -> list[tuple[int, str]]:
    """Куски по ~size символов с перекрытием; режем по абзацу или строке, а не посреди слова."""
    out, i = [], 0
    while i < len(text):
        end = min(len(text), i + size)
        if end < len(text):
            cut = max(text.rfind("\n\n", i + size // 2, end), text.rfind("\n", i + size // 2, end))
            end = cut if cut > i else end
        out.append((i, text[i:end]))
        if end >= len(text):
            break
        i = max(end - overlap, i + 1)
    return out


def keyword_rank(query: str, docs: Sequence[str]) -> list[tuple[int, float]]:
    """Номера документов по убыванию совпадения слов (tf-idf); без совпадений — не попадают."""
    q = set(stems(query))
    if not q or not docs:
        return []
    counts = [Counter(stems(d)) for d in docs]
    df = Counter(w for cnt in counts for w in q if w in cnt)
    n = len(docs)
    scored = []
    for i, cnt in enumerate(counts):
        score = sum((1 + math.log(cnt[w])) * math.log(1 + n / df[w]) for w in q if cnt.get(w))
        if score > 0:
            scored.append((i, score))
    scored.sort(key=lambda x: -x[1])
    return scored


def fuse(*rankings: Sequence[Hashable], k: int = 60) -> list[tuple[Hashable, float]]:
    """Reciprocal Rank Fusion: сумма 1/(k + место) по спискам. Шкалы оценок у слов и косинуса
    несравнимы (tf-idf ~ 0…20, косинус ~ 0.2…0.6), а места — сравнимы."""
    score: dict[Hashable, float] = {}
    for ranking in rankings:
        for pos, key in enumerate(ranking):
            score[key] = score.get(key, 0.0) + 1.0 / (k + pos + 1)
    return sorted(score.items(), key=lambda x: -x[1])


# ── эмбеддинги ───────────────────────────────────────────────────────

class Embedder(Protocol):
    model: str  # уникальное имя вместе с провайдером и размерностью: по нему видно, что индекс устарел

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


_RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}


class OpenAIEmbedder:
    """POST {base_url}/embeddings — OpenRouter, OpenAI, Ollama, vLLM, LM Studio."""

    def __init__(self, cfg: Any, model: str, *, dimensions: int = 0, batch: int = 64,
                 client: httpx.AsyncClient | None = None, max_chars: int = 8000):
        self.cfg = cfg  # OpenAICompatConfig: base_url, api_key, auth_scheme, headers
        self.name = model
        self.dimensions = dimensions
        self.model = f"{cfg.name}:{model}" + (f"@{dimensions}" if dimensions else "")
        self.batch = batch
        self.max_chars = max_chars  # ~3k токенов по-русски: у text-embedding-3 лимит 8191 на текст
        self._client = client
        self.tokens = 0
        self.cost = 0.0

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            out += await self._call(texts[i:i + self.batch])
        return out

    async def _call(self, texts: list[str]) -> list[list[float]]:
        from agentkit.llm.openai_compat import ascii_header  # noqa: PLC0415 — без цикла импорта

        cfg = self.cfg
        headers = {"Content-Type": "application/json", **{k: ascii_header(v) for k, v in cfg.headers.items()}}
        if cfg.api_key:
            headers["Authorization"] = f"{cfg.auth_scheme} {cfg.api_key}"
        # Пустая строка — 400 у OpenAI на весь пакет.
        body: dict[str, Any] = {"model": self.name, "input": [(t or " ")[:self.max_chars] for t in texts]}
        if self.dimensions:
            body["dimensions"] = self.dimensions
        url = cfg.base_url.rstrip("/") + "/embeddings"
        last: Exception | None = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(2 ** attempt)
            try:
                r = await self._http().post(url, json=body, headers=headers)
            except httpx.TransportError as e:
                last = LLMError(f"эмбеддинги {self.model}: {e!r}", retryable=True)
                continue
            if r.status_code in _RETRYABLE:
                last = LLMError(f"эмбеддинги {self.model}: HTTP {r.status_code}", status=r.status_code, retryable=True)
                continue
            if r.status_code >= 400:
                raise LLMError(f"эмбеддинги {self.model}: HTTP {r.status_code} {r.text[:300]}", status=r.status_code)
            data = r.json()
            # OpenRouter отвечает 200 с {"error": …}, если модель у провайдера недоступна.
            if "data" not in data:
                raise LLMError(f"эмбеддинги {self.model}: {str(data.get('error') or data)[:300]}")
            items = sorted(data["data"], key=lambda d: d.get("index", 0))
            if len(items) != len(texts):
                raise LLMError(f"эмбеддинги {self.model}: {len(items)} векторов на {len(texts)} текстов")
            u = data.get("usage") or {}
            self.tokens += int(u.get("total_tokens") or u.get("prompt_tokens") or 0)
            self.cost += float(u.get("cost") or 0)
            return [list(map(float, d["embedding"])) for d in items]
        assert last is not None
        raise last

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def make_embedder(spec: str, *, openrouter_key: str = "", openai_key: str = "", ollama_url: str = "",
                  dimensions: int = 0, app_name: str = "agentkit", app_url: str = "") -> Embedder | None:
    """«openrouter:openai/text-embedding-3-small», «openai:text-embedding-3-small», «ollama:bge-m3».

    None — смысловой поиск выключен (пустая строка или нет ключа провайдера); поиск остаётся по словам.
    """
    from agentkit.llm import openai_compat as oc  # noqa: PLC0415

    spec = spec.strip()
    if not spec or spec.lower() in ("off", "none", "false", "0"):
        return None
    provider, _, model = spec.partition(":")
    if provider == "openrouter" and openrouter_key:
        return OpenAIEmbedder(oc.openrouter(openrouter_key, app_name=app_name, app_url=app_url), model,
                              dimensions=dimensions)
    if provider == "openai" and openai_key:
        return OpenAIEmbedder(oc.openai(openai_key), model, dimensions=dimensions)
    if provider == "ollama" and ollama_url:
        return OpenAIEmbedder(oc.ollama(ollama_url), model, dimensions=dimensions)
    log.warning("смысловой поиск выключен: для %s нет ключа или адреса провайдера — поиск по словам", spec)
    return None


# ── индекс ───────────────────────────────────────────────────────────

@dataclass
class Hit:
    scope: str
    ref: str
    start: int
    text: str
    score: float  # косинус, 1 — то же самое


def _norm(v: Sequence[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _pack(v: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(v)}f", *v)


def _unpack(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))


def _digest(model: str, source: str) -> str:
    return hashlib.sha1(f"{model}\0{source}".encode()).hexdigest()


# HNSW у pgvector строится для vector до 2000 измерений; больше — точный перебор в базе.
HNSW_MAX_DIM = 2000

Loader = Callable[[str], "list[tuple[int, str]] | Awaitable[list[tuple[int, str]]]"]


class VectorIndex:
    """Векторы кусков текста по областям (scope) поверх SQLStore — та же база, та же транзакционность."""

    def __init__(self, store: Any, embedder: Embedder, *, cache_size: int = 256):
        from agentkit.store.sql import vectors  # noqa: PLC0415

        self.store = store
        self.embedder = embedder
        self.table = vectors
        self.pgvector = False
        self.iterative_scan = False  # pgvector ≥ 0.8: hnsw.iterative_scan
        self._hnsw: set[int] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        # Векторы запросов: тот же вопрос ищется на каждом шаге прогона (отбор памяти в промпт).
        self._qcache: OrderedDict[str, list[float]] = OrderedDict()
        self._qcache_size = cache_size

    @property
    def model(self) -> str:
        return self.embedder.model

    async def init(self) -> VectorIndex:
        if self.store.engine.dialect.name != "postgresql":
            return self
        try:
            async with self.store._begin() as c:
                await c.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
                # Колонка без размерности: модель (и размерность) можно сменить без миграции;
                # индекс — на каждую размерность отдельно, частичный (WHERE dim = N).
                await c.exec_driver_sql("ALTER TABLE ak_vectors ADD COLUMN IF NOT EXISTS embedding vector")
                ver = (await c.exec_driver_sql(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar() or "0"
            self.pgvector = True
            self.iterative_scan = tuple(int(x) for x in re.findall(r"\d+", ver)[:2]) >= (0, 8)
        except Exception as e:  # noqa: BLE001 — нет расширения или прав: работаем перебором
            log.warning("pgvector недоступен (%s) — векторы хранятся в ak_vectors.vec, поиск перебором. "
                        "Образ pgvector/pgvector:pg16 в docker-compose даёт поиск в базе.", str(e).splitlines()[0])
        return self

    async def _ensure_hnsw(self, dim: int) -> None:
        if not self.pgvector or dim in self._hnsw or dim > HNSW_MAX_DIM:
            return
        async with self.store._begin() as c:
            await c.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS ak_vectors_hnsw_{dim} ON ak_vectors "
                f"USING hnsw ((embedding::vector({dim})) vector_cosine_ops) WHERE dim = {dim}")
        self._hnsw.add(dim)

    async def query_vector(self, query: str) -> list[float]:
        if (v := self._qcache.get(query)) is not None:
            self._qcache.move_to_end(query)
            return v
        v = _norm((await self.embedder.embed([query]))[0])
        self._qcache[query] = v
        if len(self._qcache) > self._qcache_size:
            self._qcache.popitem(last=False)
        return v

    async def sync(self, scope: str, refs: dict[str, str], load: Loader) -> int:
        """Приводит векторы области к refs: {ref: что определяет его текст}. load(ref) → куски [(start, текст)].

        Возвращает, сколько ref посчитано заново. Ничего не поменялось — один SELECT, без запросов к модели.
        """
        t = self.table
        lock = self._locks.setdefault(scope, asyncio.Lock())
        async with lock:
            async with self.store._connect() as c:
                rows = (await c.execute(select(t.c.ref, t.c.digest).where(t.c.scope == scope).distinct())).all()
            have: dict[str, set[str]] = {}
            for ref, dg in rows:
                have.setdefault(ref, set()).add(dg)
            want = {ref: _digest(self.model, src) for ref, src in refs.items()}
            stale = [ref for ref, dgs in have.items() if dgs != {want.get(ref)}]
            todo = [ref for ref in want if have.get(ref) != {want[ref]}]
            if stale:
                async with self.store._begin() as c:
                    for i in range(0, len(stale), 500):
                        await c.execute(delete(t).where(t.c.scope == scope, t.c.ref.in_(stale[i:i + 500])))
            if not todo:
                return 0
            pieces: list[tuple[str, int, str]] = []
            for ref in todo:
                got = load(ref)
                if asyncio.iscoroutine(got) or isinstance(got, Awaitable):
                    got = await got
                pieces += [(ref, start, txt) for start, txt in got if txt.strip()]
            vecs = [_norm(v) for v in await self.embedder.embed([p[2] for p in pieces])] if pieces else []
            rows_out = []
            for (ref, start, txt), v in zip(pieces, vecs, strict=True):
                dg = want[ref]
                rows_out.append({"id": hashlib.sha1(f"{scope}|{ref}|{start}|{dg}".encode()).hexdigest(),
                                 "scope": scope, "ref": ref, "start": start, "text": txt, "digest": dg,
                                 "model": self.model, "dim": len(v), "v": v})
            await self._insert(rows_out)
            return len(todo)

    async def _insert(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        if self.pgvector:
            await self._ensure_hnsw(rows[0]["dim"])
            stmt = sql("INSERT INTO ak_vectors (id, scope, ref, start, text, digest, model, dim, embedding) "
                       "VALUES (:id, :scope, :ref, :start, :text, :digest, :model, :dim, CAST(:emb AS vector)) "
                       "ON CONFLICT (id) DO NOTHING")
            params = [{**{k: v for k, v in r.items() if k != "v"}, "emb": "[" + ",".join(map(repr, r["v"])) + "]"}
                      for r in rows]
            async with self.store._begin() as c:
                await c.execute(stmt, params)
            return
        # Другой процесс мог посчитать тот же кусок одновременно — id детерминированный, дубль пропускаем.
        if self.store.engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert  # noqa: PLC0415
        elif self.store.engine.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert  # noqa: PLC0415
        else:
            dialect_insert = None
        values = [{**{k: v for k, v in r.items() if k != "v"}, "vec": _pack(r["v"])} for r in rows]
        async with self.store._begin() as c:
            if dialect_insert is not None:
                await c.execute(dialect_insert(self.table).on_conflict_do_nothing(), values)
            else:
                await c.execute(insert(self.table), values)

    async def search(self, scopes: list[str], query: str, k: int = 8) -> list[Hit]:
        if not scopes or not query.strip():
            return []
        qv = await self.query_vector(query)
        dim = len(qv)
        if self.pgvector:
            # Фильтр по области после HNSW мог бы вернуть меньше k (индекс общий на всех):
            # iterative_scan (pgvector ≥ 0.8) досматривает граф, пока не наберётся k. На старых
            # версиях параметра нет, и SET падает («invalid configuration parameter») — там
            # только шире ef_search.
            # dim — числом в тексте запроса: только так планировщик видит частичный индекс WHERE dim = N.
            q = sql(f"SELECT scope, ref, start, text, 1 - (embedding::vector({dim}) <=> CAST(:q AS vector({dim}))) "
                    f"AS score FROM ak_vectors WHERE dim = {dim} AND model = :model AND scope = ANY(:scopes) "
                    f"ORDER BY embedding::vector({dim}) <=> CAST(:q AS vector({dim})) LIMIT :k")
            async with self.store._begin() as c:
                if self.iterative_scan:
                    await c.exec_driver_sql("SET LOCAL hnsw.iterative_scan = relaxed_order")
                await c.exec_driver_sql(f"SET LOCAL hnsw.ef_search = {max(40, min(1000, k * 10))}")
                rows = (await c.execute(q, {"q": "[" + ",".join(map(repr, qv)) + "]", "model": self.model,
                                            "scopes": list(scopes), "k": k})).all()
            return [Hit(r[0], r[1], r[2], r[3], float(r[4])) for r in rows]
        t = self.table
        async with self.store._connect() as c:
            rows = (await c.execute(select(t.c.scope, t.c.ref, t.c.start, t.c.text, t.c.vec)
                                    .where(t.c.scope.in_(scopes), t.c.model == self.model, t.c.dim == dim))).all()
        if not rows:
            return []
        scores = _dots(qv, [r[4] for r in rows])
        best = sorted(range(len(rows)), key=lambda i: -scores[i])[:k]
        return [Hit(rows[i][0], rows[i][1], rows[i][2], rows[i][3], scores[i]) for i in best]


def _dots(q: list[float], packed: list[bytes]) -> list[float]:
    try:
        import numpy as np  # noqa: PLC0415 — необязательный: в 20 раз быстрее на тысячах кусков

        m = np.frombuffer(b"".join(packed), dtype="<f4").reshape(len(packed), -1)
        return (m @ np.asarray(q, dtype="<f4")).tolist()
    except ImportError:
        return [sum(a * b for a, b in zip(q, _unpack(p), strict=False)) for p in packed]


# ── память ───────────────────────────────────────────────────────────

async def rank_memory(store: Any, index: VectorIndex | None, scopes: list[str], query: str,
                      items: list[Any] | None = None, *, k: int = 20, margin: float | None = None) -> list[Any]:
    """Записи памяти по близости к запросу: по смыслу (если есть индекс) и по словам, слитно.

    Векторный поиск всегда возвращает k ближайших, даже если ничего похожего нет. margin —
    отсечка «не дальше лучшего на столько по косинусу»: для отбора в промпт, где место дорого.
    Порог относительный, а не абсолютный: у разных моделей «похоже» начинается с разного
    косинуса (у text-embedding-3 близкие ~0.5, чужие ~0.1, у bge-m3 всё выше).

    Индекс недоступен или упал — только по словам: память не должна ломаться из-за сети.
    """
    items = items if items is not None else await store.memory_list(scopes)
    if not items or not query.strip():
        return []
    by_id = {m.id: m for m in items}
    words = [items[i].id for i, _ in keyword_rank(query, [m.text for m in items])]
    near: list[str] = []
    if index is not None:
        try:
            for sc in {m.scope for m in items}:
                texts = {m.id: m.text for m in items if m.scope == sc}
                await index.sync(sc, texts, lambda ref, texts=texts: [(0, texts[ref])])
            hits = [h for h in await index.search(sorted({m.scope for m in items}), query, k=k) if h.ref in by_id]
            if margin is not None and hits:
                hits = [h for h in hits if h.score >= hits[0].score - margin]
            near = [h.ref for h in hits]
        except Exception as e:  # noqa: BLE001
            log.warning("смысловой поиск по памяти не удался (%s) — ищу по словам", e)
    return [by_id[key] for key, _ in fuse(near, words)[:k]]  # type: ignore[index]
