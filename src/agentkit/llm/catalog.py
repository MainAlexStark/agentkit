"""Каталог моделей: что есть у провайдеров, сколько стоит, что умеет.

OpenRouter отдаёт в /models сотни моделей с ценами, контекстом и списком
поддерживаемых параметров — из этого и строится выбор модели в чате. Провайдеры
без каталога (Яндекс, FakeLLM) представлены моделями из настроек.

Каталог живёт в памяти процесса и обновляется раз в `ttl`. Если провайдер не
ответил, остаётся прошлый снимок: падение OpenRouter на минуту не должно
превращать выбор модели в пустой список.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from typing import Any

from agentkit.llm.base import ModelInfo
from agentkit.llm.router import ModelRouter, split_model

log = logging.getLogger("agentkit.models")


def _price(v: Any) -> float | None:
    """Цена OpenRouter — строка «$ за токен»; в UI нужна «$ за 1M». «-1» — цена плавает (роутеры)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f < 0 else round(f * 1_000_000, 4)


def parse_model(provider: str, raw: dict[str, Any]) -> ModelInfo:
    """Запись /models (OpenAI-совместимая, с расширениями OpenRouter) → ModelInfo."""
    mid = str(raw.get("id", ""))
    arch = raw.get("architecture") or {}
    params = raw.get("supported_parameters")
    inputs = arch.get("input_modalities") or []
    pricing = raw.get("pricing") or {}
    name = str(raw.get("name") or mid)
    vendor = mid.split("/", 1)[0] if "/" in mid else provider
    # «Anthropic: Claude Sonnet 4.5» — префикс дублирует vendor, в списке он лишний.
    if ": " in name:
        name = name.split(": ", 1)[1]
    return ModelInfo(
        id=f"{provider}:{mid}",
        name=name,
        context_window=int(raw.get("context_length") or (raw.get("top_provider") or {}).get("context_length") or 0),
        vision="image" in inputs or "image" in str(arch.get("modality", "")).split("->")[0],
        # Без списка параметров (OpenAI, Ollama) считаем, что инструменты есть: иначе каталог
        # этих провайдеров оказался бы пустым — агенту без инструментов модель не нужна.
        tools=True if params is None else ("tools" in params),
        reasoning=bool(params and ("reasoning" in params or "include_reasoning" in params)),
        price_in=_price(pricing.get("prompt")),
        price_out=_price(pricing.get("completion")),
        provider=provider,
        vendor=vendor,
        description=str(raw.get("description") or "")[:400],
        created=float(raw.get("created") or 0),
    )


class ModelCatalog:
    def __init__(self, router: ModelRouter, *, configured: Iterable[str] = (), ttl: float = 6 * 3600,
                 timeout: float = 20.0):
        self.router = router
        self.configured = [m for m in configured if m]
        self.ttl = ttl
        self.timeout = timeout
        self._by_provider: dict[str, list[ModelInfo]] = {}
        self._errors: dict[str, str] = {}
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def fetched_at(self) -> float:
        return self._fetched_at

    @property
    def errors(self) -> dict[str, str]:
        return dict(self._errors)

    async def refresh(self, *, force: bool = False) -> None:
        if not force and self._fetched_at and time.time() - self._fetched_at < self.ttl:
            return
        async with self._lock:
            if not force and self._fetched_at and time.time() - self._fetched_at < self.ttl:
                return  # обновил сосед, пока ждали блокировку

            async def one(name: str, p: Any) -> None:
                lister = getattr(p, "list_models", None)
                if lister is None:
                    return
                try:
                    raw = await asyncio.wait_for(lister(), self.timeout)
                    # «:batch» — пакетные варианты (ответ через часы): для чата бесполезны и удваивают список.
                    self._by_provider[name] = [parse_model(name, r) for r in raw
                                               if r.get("id") and not str(r["id"]).endswith(":batch")]
                    self._errors.pop(name, None)
                except Exception as e:  # noqa: BLE001 — любой сбой провайдера не должен ронять список
                    log.warning("каталог %s не получен: %s", name, e)
                    self._errors[name] = str(e) or type(e).__name__

            await asyncio.gather(*(one(n, p) for n, p in self.router.providers.items()))
            self._fetched_at = time.time()

    def models(self, *, tools_only: bool = True) -> list[ModelInfo]:
        """Все модели: из каталогов провайдеров плюс заданные в настройках (их каталог мог не вернуть)."""
        out: dict[str, ModelInfo] = {}
        for items in self._by_provider.values():
            for m in items:
                if m.tools or not tools_only:
                    out[m.id] = m
        for mid in self.configured:
            mid = self.router.resolve(mid)
            if mid not in out:
                out[mid] = self._stub(mid)
        return list(out.values())

    def get(self, model_id: str) -> ModelInfo | None:
        model_id = self.router.resolve(model_id)
        for items in self._by_provider.values():
            for m in items:
                if m.id == model_id:
                    return m
        return None

    def vision(self, model_id: str) -> bool | None:
        """Умеет ли модель картинки; None — каталог не знает (решают шаблоны vision_models)."""
        m = self.get(model_id)
        return m.vision if m else None

    def _stub(self, mid: str) -> ModelInfo:
        provider, name = split_model(mid, self.router.default_provider)
        short = name.split("/", 1)[-1]
        return ModelInfo(id=mid, name=short, provider=provider,
                         vendor=name.split("/", 1)[0] if "/" in name else provider, context_window=0)
