"""HTTP-клиент только к публичным адресам — для серверов, адрес которых ввёл не админ.

Личный MCP-сервер добавляет любой пользователь, а запросы к нему идёт делать
сервис агента — изнутри своей сети. Без проверки адрес http://redis:6379,
http://10.0.0.5/admin или http://169.254.169.254/ (metadata облака) превратил бы
кнопку «Проверить» в сканер внутренней сети и вытащил бы секреты сервера (SSRF).
Те же запросы делает и OAuth: адреса authorize/token берутся из метаданных,
которые отдаёт сам сервер — их тоже нельзя пускать внутрь.

Адрес проверяется ПОСЛЕ резолва, и соединение идёт на тот же проверенный IP:
повторный резолв дал бы DNS-rebinding (проверили публичный, подключились к
10.0.0.5). TLS при этом проверяется по исходному имени хоста — httpcore
передаёт его в SNI сам, адрес подключения на это не влияет.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpcore
import httpx

from agentkit.egress import public


class PrivateAddress(httpcore.ConnectError):
    """Адрес ведёт во внутреннюю сеть. Наследник ConnectError: httpx покажет его как обычный сбой связи."""


async def resolve_public(host: str, port: int) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise httpcore.ConnectError(f"{host}: адрес не найден ({e})") from e
    ips = list(dict.fromkeys(i[4][0] for i in infos))
    if not ips:
        raise httpcore.ConnectError(f"{host}: адрес не найден")
    if not all(public(ip) for ip in ips):
        # Все, а не хотя бы один: иначе хост с двумя A-записями (публичной и 10.x) прошёл бы.
        raise PrivateAddress(f"{host} ведёт во внутреннюю сеть — личному серверу туда нельзя "
                             "(нужен публичный адрес; для разработки — AGENT_USER_MCP_PRIVATE=true)")
    return ips


class _PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, inner: httpcore.AsyncNetworkBackend):
        self.inner = inner

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None, socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        last: Exception | None = None
        # По очереди ко всем проверенным адресам: первым часто приходит IPv6, а он бывает недоступен
        # (тот же разбор, что в egress._connect).
        for ip in await resolve_public(host, port):
            try:
                return await self.inner.connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                                    socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                last = e
        assert last is not None
        raise last

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        raise PrivateAddress("unix-сокет — это эта машина, туда нельзя")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


class PublicOnlyTransport(httpx.AsyncHTTPTransport):
    def __init__(self, **kw: Any):
        super().__init__(**kw)
        pool = getattr(self, "_pool", None)
        if pool is None or not hasattr(pool, "_network_backend"):
            # Подмена сети — через внутренности httpcore. Если их переименуют, лучше упасть на старте,
            # чем молча ходить во внутреннюю сеть без проверки.
            raise RuntimeError("httpx/httpcore сменили устройство пула — обновите agentkit.netguard")
        pool._network_backend = _PublicOnlyBackend(pool._network_backend)


def public_client(*, timeout: Any = 30, follow_redirects: bool = False) -> httpx.AsyncClient:
    """httpx-клиент, который не подключается к внутренним адресам (и после редиректа тоже)."""
    # trust_env=False: через HTTP(S)_PROXY соединение шло бы к прокси, и проверка видела бы только его адрес.
    return httpx.AsyncClient(transport=PublicOnlyTransport(), timeout=timeout,
                             follow_redirects=follow_redirects, trust_env=False)
