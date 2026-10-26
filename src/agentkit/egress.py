#!/usr/bin/env python3
"""Выход песочницы в интернет: HTTP(S)-прокси с белым списком доменов.

Контейнеры разговоров сидят в сети без выхода наружу; единственная дорога —
этот прокси (HTTP_PROXY/HTTPS_PROXY в их окружении). Он пропускает только
домены из списка и никогда — внутренние адреса: иначе код из чужого CSV мог бы
постучаться в базу, Redis, сервис агента или в metadata облака.

Адрес проверяется ПОСЛЕ резолва, и соединение идёт на тот же проверенный IP —
повторный резолв дал бы DNS-rebinding (сначала публичный адрес, потом 10.0.0.5).

    agentkit egress --allow pypi.org,files.pythonhosted.org --port 3128

Только стандартная библиотека — работает в образе песочницы.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import socket
import sys
from urllib.parse import urlsplit

MAX_HEAD = 64 * 1024


def parse_allow(s: str) -> list[str]:
    return [h.strip().lower().removeprefix("*.").rstrip(".") for h in s.split(",") if h.strip()]


def allowed(host: str, allow: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return "*" in allow or any(host == d or host.endswith("." + d) for d in allow)


def public(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


class Egress:
    def __init__(self, allow: list[str], *, ports: set[int] | None = None, block_private: bool = True,
                 log=None):
        self.allow, self.ports, self.block_private = allow, ports or {80, 443}, block_private
        self.log = log or (lambda m: print(f"egress: {m}", file=sys.stderr, flush=True))

    async def _resolve(self, host: str, port: int) -> str:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ips = [i[4][0] for i in infos]
        if not ips:
            raise OSError("адрес не найден")
        if self.block_private and not all(public(ip) for ip in ips):
            raise PermissionError(f"{host} ведёт во внутреннюю сеть")
        return ips[0]

    async def _deny(self, w: asyncio.StreamWriter, code: int, text: str) -> None:
        body = (text + "\n").encode()
        w.write(f"HTTP/1.1 {code} Forbidden\r\nContent-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        try:
            await w.drain()
        finally:
            w.close()

    async def handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            head = await r.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            w.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            return await self._deny(w, 400, "egress: не разобрать запрос")
        connect = method.upper() == "CONNECT"
        if connect:
            host, _, port_s = target.rpartition(":")
            host = host.strip("[]")
            port = int(port_s) if port_s.isdigit() else 443
        else:
            u = urlsplit(target)
            if u.scheme != "http" or not u.hostname:
                return await self._deny(w, 400, "egress: нужен абсолютный адрес http://…")
            host, port = u.hostname, u.port or 80
        if not allowed(host, self.allow):
            self.log(f"запрещено {host}:{port}")
            return await self._deny(w, 403, f"egress: домена {host} нет в списке разрешённых "
                                            "(AGENT_SANDBOX_EGRESS); попросите администратора добавить")
        if port not in self.ports:
            return await self._deny(w, 403, f"egress: порт {port} закрыт (разрешены {sorted(self.ports)})")
        try:
            ip = await self._resolve(host, port)
            ur, uw = await asyncio.wait_for(asyncio.open_connection(ip, port), 20)
        except PermissionError as e:
            self.log(f"запрещено {e}")
            return await self._deny(w, 403, f"egress: {e}")
        except (TimeoutError, OSError) as e:
            return await self._deny(w, 502, f"egress: не подключиться к {host}:{port}: {e}")
        self.log(f"{'CONNECT' if connect else method} {host}:{port}")
        if connect:
            w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            u = urlsplit(target)
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            # Proxy-* — заголовки прокси, наверх не идут; Connection: close — одна цель на соединение,
            # иначе keep-alive мог бы унести следующий запрос к другому хосту мимо проверки.
            hdrs = [ln for ln in lines[1:] if ln and not ln.lower().startswith(("proxy-", "connection:"))]
            uw.write((f"{method} {path} {version}\r\n" + "\r\n".join([*hdrs, "Connection: close"]) + "\r\n\r\n")
                     .encode("latin-1"))
        await asyncio.gather(_pipe(r, uw), _pipe(ur, w))

    async def serve(self, host: str, port: int) -> None:
        srv = await asyncio.start_server(self.handle, host, port, limit=MAX_HEAD)
        self.log(f"слушаю {host}:{port}, разрешено: {', '.join(self.allow) or 'ничего'}")
        async with srv:
            await srv.serve_forever()


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while data := await src.read(65536):
            dst.write(data)
            await dst.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            dst.close()
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Прокси с белым списком для песочницы agentkit")
    ap.add_argument("--allow", default=os.environ.get("EGRESS_ALLOW", ""), help="домены через запятую, * — все")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("EGRESS_PORT", "3128")))
    ap.add_argument("--ports", default=os.environ.get("EGRESS_PORTS", "80,443"), help="разрешённые порты")
    a = ap.parse_args(argv)
    eg = Egress(parse_allow(a.allow), ports={int(p) for p in a.ports.split(",") if p.strip()})
    asyncio.run(eg.serve(a.host, a.port))


if __name__ == "__main__":
    main()
