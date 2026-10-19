"""Веб: чтение страниц (web_fetch) и поиск (web_search с подключаемым поставщиком).

web_fetch закрыт от SSRF: адрес, который резолвится во внутреннюю сеть
(localhost, 10.*, 192.168.*, metadata облака), не открывается — иначе
модель, прочитав «зайди на http://169.254.169.254/…» в чужом документе,
достала бы секреты сервера.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
from typing import Annotated, Any, Protocol
from urllib.parse import urlparse

import httpx

from agentkit.tools.base import Tool, ToolContext, ToolResult, tool

UA = "Mozilla/5.0 (compatible; agentkit/0.1; +https://github.com/MainAlexStark/agentkit)"


async def _is_public(host: str) -> bool:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


_DROP = re.compile(r"<(script|style|noscript|svg|head|nav|footer)[^>]*>.*?</\1>", re.S | re.I)
_BLOCK = re.compile(r"</?(p|div|br|li|tr|h[1-6]|section|article|table|ul|ol)[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def html_to_text(src: str) -> str:
    title = re.search(r"<title[^>]*>(.*?)</title>", src, re.S | re.I)
    s = _DROP.sub(" ", src)
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s).strip()
    return (f"# {html.unescape(title.group(1)).strip()}\n\n" if title else "") + s


def web_fetch_tool(*, allow_private: bool = False, max_chars: int = 60_000) -> Tool:
    @tool(read_only=True, group="web", timeout=60, source="builtin")
    async def web_fetch(
        ctx: ToolContext,
        url: Annotated[str, "Адрес http(s)"],
    ) -> ToolResult:
        """Открывает страницу и возвращает её текст (HTML очищается). Для JSON и простого текста — как есть."""
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return ToolResult.error("нужен адрес http(s)://…")
        if not allow_private and not await _is_public(u.hostname):
            return ToolResult.error("адрес во внутренней сети — открывать запрещено")
        async with httpx.AsyncClient(follow_redirects=True, timeout=30, headers={"User-Agent": UA}) as c:
            r = await c.get(url)
            # Редирект мог увести во внутреннюю сеть — проверяем конечный адрес тоже.
            if not allow_private and r.url.host and not await _is_public(r.url.host):
                return ToolResult.error("редирект во внутреннюю сеть — открывать запрещено")
        ctype = r.headers.get("content-type", "")
        if r.status_code >= 400:
            return ToolResult.error(f"HTTP {r.status_code}")
        if "html" in ctype:
            text = html_to_text(r.text)
        elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
            text = r.text
        else:
            return ToolResult(content=f"Файл {ctype}, {len(r.content)} байт — текст из него этим инструментом не извлечь.")
        return ToolResult(content=text[:max_chars] + ("\n… [обрезано]" if len(text) > max_chars else ""))

    return web_fetch


class SearchProvider(Protocol):
    async def search(self, query: str, limit: int) -> list[dict[str, Any]]: ...


class TavilySearch:
    """https://tavily.com — простой поисковый API для агентов (есть бесплатная квота)."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.tavily.com/search",
                             json={"api_key": self.api_key, "query": query, "max_results": limit})
            r.raise_for_status()
            return [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("content")}
                    for x in r.json().get("results", [])]


def web_search_tool(provider: SearchProvider) -> Tool:
    @tool(read_only=True, group="web", timeout=60, source="builtin")
    async def web_search(
        ctx: ToolContext,
        query: Annotated[str, "Поисковый запрос"],
        limit: Annotated[int, "Сколько результатов"] = 8,
    ) -> ToolResult:
        """Ищет в интернете. Возвращает заголовки, адреса и фрагменты; подробности — web_fetch по адресу."""
        res = await provider.search(query, limit)
        if not res:
            return ToolResult(content="Ничего не найдено.")
        lines = [f"{i}. {r['title']}\n   {r['url']}\n   {(r.get('snippet') or '')[:400]}" for i, r in enumerate(res, 1)]
        return ToolResult(content="\n".join(lines), data=res)

    return web_search
