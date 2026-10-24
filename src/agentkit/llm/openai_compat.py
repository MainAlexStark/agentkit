"""Провайдер для любого OpenAI-совместимого /chat/completions.

Покрывает OpenRouter (в т.ч. Claude, GPT, Gemini, Qwen), Yandex AI Studio,
OpenAI, vLLM, Ollama, LM Studio. Свой HTTP-клиент на httpx вместо SDK: у
провайдеров «совместимость» неполная (reasoning, cost, cache_control, формат
модели у Яндекса), и проще держать отличия явными полями конфига, чем
обходить SDK.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from agentkit.llm.base import LLMError, LLMRequest, LLMResponse, StreamChunk
from agentkit.messages import Message, ToolCall, Usage, new_id

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}


@dataclass
class OpenAICompatConfig:
    name: str
    base_url: str
    api_key: str = ""
    auth_scheme: str = "Bearer"  # Яндекс: "Api-Key"
    # Как имя модели превращается в поле model запроса. Яндекс: "gpt://{folder}/{model}"
    model_template: str = "{model}"
    template_vars: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    # "openrouter" → {"reasoning": {"effort": …}}, "openai" → {"reasoning_effort": …}, None — не слать
    reasoning_param: str | None = None
    # Расставлять cache_control для моделей Anthropic (через OpenRouter это работает).
    anthropic_cache: bool = False
    # OpenRouter отдаёт стоимость в usage, если попросить.
    request_usage_cost: bool = False
    timeout: float = 300.0


_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                      "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"], strict=True))


def ascii_header(value: str) -> str:
    """Значение HTTP-заголовка только в ASCII: httpx падает на «Метролог» ещё до отправки
    ('ascii' codec can't encode…) — и не работают ни чат, ни список моделей.
    Кириллицу транслитерируем, остальное не-ASCII выкидываем."""
    out = []
    for ch in value:
        low = ch.lower()
        if low in _TRANSLIT:
            t = _TRANSLIT[low]
            out.append(t.capitalize() if ch != low else t)
        elif ord(ch) < 128:
            out.append(ch)
    return "".join(out).strip() or "agent"


def openrouter(api_key: str, *, app_name: str = "agentkit", app_url: str = "") -> OpenAICompatConfig:
    headers = {"X-Title": ascii_header(app_name)}
    if app_url:
        headers["HTTP-Referer"] = app_url
    return OpenAICompatConfig(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        headers=headers,
        reasoning_param="openrouter",
        anthropic_cache=True,
        request_usage_cost=True,
    )


def yandex(api_key: str, folder_id: str) -> OpenAICompatConfig:
    return OpenAICompatConfig(
        name="yandex",
        base_url="https://llm.api.cloud.yandex.net/v1",
        api_key=api_key,
        auth_scheme="Api-Key",
        model_template="gpt://{folder}/{model}",
        template_vars={"folder": folder_id},
        headers={"x-folder-id": folder_id},
    )


def openai(api_key: str) -> OpenAICompatConfig:
    return OpenAICompatConfig(
        name="openai", base_url="https://api.openai.com/v1", api_key=api_key, reasoning_param="openai"
    )


def ollama(base_url: str = "http://localhost:11434/v1") -> OpenAICompatConfig:
    return OpenAICompatConfig(name="ollama", base_url=base_url, timeout=600.0)


def _is_anthropic(model: str) -> bool:
    m = model.lower()
    return m.startswith("anthropic/") or "claude" in m


def _with_cache_control(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ставит cache_control на системный промпт и на последнее сообщение.

    Две точки: стабильный префикс (системный промпт + описания инструментов)
    кэшируется на всю сессию, а метка на последнем сообщении позволяет
    следующему шагу цикла переиспользовать всю историю до него. У Anthropic
    до 4 точек, две — с запасом под инструменты.
    """
    out = [dict(m) for m in msgs]

    def mark(m: dict[str, Any]) -> None:
        c = m.get("content")
        if isinstance(c, str) and c:
            m["content"] = [{"type": "text", "text": c, "cache_control": {"type": "ephemeral"}}]
        elif isinstance(c, list) and c:
            c = [dict(p) for p in c]
            c[-1]["cache_control"] = {"type": "ephemeral"}
            m["content"] = c

    if out and out[0].get("role") == "system":
        mark(out[0])
    for m in reversed(out):
        if m.get("role") in ("user", "tool"):
            mark(m)
            break
    return out


class OpenAICompatProvider:
    def __init__(self, cfg: OpenAICompatConfig, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.name = cfg.name
        self._client = client

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.cfg.timeout, connect=15.0))
        return self._client

    def build_body(self, req: LLMRequest) -> dict[str, Any]:
        cfg = self.cfg
        model = cfg.model_template.format(model=req.model, **cfg.template_vars)
        messages = [m.to_openai() for m in req.messages]
        if cfg.anthropic_cache and _is_anthropic(req.model):
            messages = _with_cache_control(messages)
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        body["stream_options"] = {"include_usage": True}
        if req.tools:
            body["tools"] = req.tools
            if req.tool_choice is not None:
                body["tool_choice"] = req.tool_choice
            if not req.parallel_tool_calls:
                body["parallel_tool_calls"] = False
        if req.max_tokens:
            body["max_tokens"] = req.max_tokens
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.response_format:
            body["response_format"] = req.response_format
        if req.reasoning_effort and cfg.reasoning_param == "openrouter":
            body["reasoning"] = {"effort": req.reasoning_effort}
        elif req.reasoning_effort and cfg.reasoning_param == "openai":
            body["reasoning_effort"] = req.reasoning_effort
        if cfg.request_usage_cost:
            body["usage"] = {"include": True}
        body.update(cfg.extra_body)
        body.update(req.extra)
        return body

    async def stream(self, req: LLMRequest) -> AsyncIterator[StreamChunk]:
        cfg = self.cfg
        headers = {"Content-Type": "application/json", **{k: ascii_header(v) for k, v in cfg.headers.items()}}
        if cfg.api_key:
            headers["Authorization"] = f"{cfg.auth_scheme} {cfg.api_key}"
        body = self.build_body(req)

        text_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage = Usage()
        finish: str | None = None
        model_used: str | None = None

        try:
            async with self._http().stream(
                "POST", cfg.base_url.rstrip("/") + "/chat/completions", headers=headers, json=body
            ) as r:
                if r.status_code >= 400:
                    detail = (await r.aread()).decode("utf-8", "replace")[:2000]
                    raise LLMError(
                        f"{cfg.name}: HTTP {r.status_code}: {detail}",
                        status=r.status_code,
                        retryable=r.status_code in RETRYABLE_STATUS,
                    )
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue  # комментарии keep-alive (": OPENROUTER PROCESSING") и пустые строки
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if err := chunk.get("error"):
                        code = err.get("code") if isinstance(err, dict) else None
                        msg = err.get("message") if isinstance(err, dict) else str(err)
                        raise LLMError(
                            f"{cfg.name}: {msg}",
                            status=code if isinstance(code, int) else None,
                            retryable=code in RETRYABLE_STATUS if isinstance(code, int) else True,
                        )
                    model_used = chunk.get("model") or model_used
                    if u := chunk.get("usage"):
                        usage = _parse_usage(u)
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if t := delta.get("content"):
                            text_parts.append(t)
                            yield StreamChunk("text", text=t)
                        if th := (delta.get("reasoning") or delta.get("reasoning_content")):
                            yield StreamChunk("thinking", text=th)
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", len(calls))
                            slot = calls.setdefault(idx, {"id": None, "name": "", "args": []})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["args"].append(fn["arguments"])
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
        except httpx.TimeoutException as e:
            raise LLMError(f"{cfg.name}: таймаут: {e}", retryable=True) from e
        except httpx.TransportError as e:
            raise LLMError(f"{cfg.name}: обрыв связи: {e}", retryable=True) from e

        tool_calls = []
        for idx in sorted(calls):
            s = calls[idx]
            tool_calls.append(
                ToolCall.from_openai(
                    {
                        "id": s["id"] or new_id("call_"),
                        "function": {"name": s["name"], "arguments": "".join(s["args"]) or "{}"},
                    }
                )
            )
        msg = Message(role="assistant", content="".join(text_parts) or None, tool_calls=tool_calls)
        yield StreamChunk(
            "done", response=LLMResponse(message=msg, usage=usage, finish_reason=finish, model=model_used)
        )

    async def list_models(self) -> list[dict[str, Any]]:
        """Сырой каталог /models (у OpenRouter — с ценами, контекстом и поддержкой tools)."""
        headers = {k: ascii_header(v) for k, v in self.cfg.headers.items()}
        if self.cfg.api_key:
            headers["Authorization"] = f"{self.cfg.auth_scheme} {self.cfg.api_key}"
        r = await self._http().get(self.cfg.base_url.rstrip("/") + "/models", headers=headers)
        r.raise_for_status()
        return r.json().get("data", [])

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def _parse_usage(u: dict[str, Any]) -> Usage:
    pd = u.get("prompt_tokens_details") or {}
    cd = u.get("completion_tokens_details") or {}
    return Usage(
        input_tokens=int(u.get("prompt_tokens") or 0),
        output_tokens=int(u.get("completion_tokens") or 0),
        cached_tokens=int(pd.get("cached_tokens") or 0),
        reasoning_tokens=int(cd.get("reasoning_tokens") or 0),
        cost=float(u.get("cost") or 0.0),
    )
