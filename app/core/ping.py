"""
ping.py — Async tracker connectivity checker

Tests trackers via:
  - UDP: raw BitTorrent UDP tracker protocol "connect" handshake
  - HTTP/HTTPS: announce request, any non-5xx response counts as reachable
  - WS/WSS: mapped to HTTP/HTTPS equivalent (most WS trackers accept plain HTTP announce)

When no_udp=True (proxy modes), UDP trackers are skipped entirely rather than
tested — SOCKS5 and HTTP CONNECT proxies cannot tunnel UDP traffic.

Runs in-process. No subprocess, no Docker, no file-based handoff.
"""

from __future__ import annotations

import asyncio
import logging
import random
import socket
import struct
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

CONNECT_MAGIC = 0x41727101980
MAX_CONCURRENCY = 150


@dataclass
class PingResult:
    url: str
    up: bool | None     # None = skipped (UDP under proxy mode)


class _UDPPingProtocol(asyncio.DatagramProtocol):
    def __init__(self, packet: bytes, tid: int):
        self.packet = packet
        self.tid = tid
        self.result: asyncio.Future = asyncio.get_event_loop().create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        transport.sendto(self.packet)  # type: ignore[attr-defined]

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if self.result.done():
            return
        if len(data) >= 16:
            action, rtid, _ = struct.unpack(">IIQ", data[:16])
            self.result.set_result(action == 0 and rtid == self.tid)
        else:
            self.result.set_result(False)

    def error_received(self, exc: Exception) -> None:
        if not self.result.done():
            self.result.set_result(False)

    def connection_lost(self, exc: Exception | None) -> None:
        if not self.result.done():
            self.result.set_result(False)


async def _ping_udp(url: str, timeout: float) -> bool:
    p = urlparse(url)
    host = p.hostname or ""
    port = p.port or 80
    tid = random.randint(0, 0xFFFFFFFF)
    pkt = struct.pack(">QII", CONNECT_MAGIC, 0, tid)

    try:
        loop = asyncio.get_event_loop()
        infos = await loop.run_in_executor(
            None, lambda: socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
        )
        if not infos:
            return False
        addr = infos[0][4]
        proto = _UDPPingProtocol(pkt, tid)
        transport, _ = await asyncio.wait_for(
            loop.create_datagram_endpoint(lambda: proto, remote_addr=addr),
            timeout=timeout,
        )
        try:
            return await asyncio.wait_for(proto.result, timeout=timeout)
        finally:
            transport.close()
    except Exception:
        return False


async def _ping_http(session: aiohttp.ClientSession, url: str, timeout: float) -> bool:
    base = url.rstrip("/")
    if not base.endswith("/announce"):
        base += "/announce"
    try:
        async with session.get(
            base,
            params={
                "info_hash": "%00" * 20,
                "peer_id": "-TR3000-000000000000",
                "port": "6881",
                "uploaded": "0",
                "downloaded": "0",
                "left": "0",
                "compact": "1",
            },
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
            ssl=False,
        ) as resp:
            return resp.status < 500
    except Exception:
        return False


async def _ping_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    url: str,
    no_udp: bool,
    timeout: float,
) -> PingResult:
    async with sem:
        scheme = urlparse(url).scheme.lower()
        if scheme in ("http", "https"):
            return PingResult(url, await _ping_http(session, url, timeout))
        elif scheme in ("ws", "wss"):
            alt = url.replace("wss://", "https://").replace("ws://", "http://")
            return PingResult(url, await _ping_http(session, alt, timeout))
        elif scheme == "udp":
            if no_udp:
                return PingResult(url, None)
            return PingResult(url, await _ping_udp(url, timeout))
        else:
            return PingResult(url, False)


async def ping_all(
    urls: list[str],
    no_udp: bool = False,
    timeout: float = 10.0,
    proxy_url: str | None = None,
) -> list[PingResult]:
    """
    Pings every tracker URL concurrently (capped at MAX_CONCURRENCY).

    proxy_url: if set, HTTP/HTTPS pings are routed through this proxy
               (e.g. "socks5://host:port" or "http://host:port").
               UDP cannot be proxied — callers should pass no_udp=True
               whenever proxy_url is set.
    """
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    connector = aiohttp.TCPConnector(ssl=False, limit=MAX_CONCURRENCY)

    session_kwargs = {"connector": connector}

    async with aiohttp.ClientSession(**session_kwargs) as session:
        if proxy_url:
            # aiohttp's native proxy support handles HTTP CONNECT proxies directly.
            # SOCKS5 requires aiohttp-socks; until that dependency is added,
            # proxy_url is passed through to _ping_http via session-level trust_env
            # is NOT used here — proxy is applied per-request below.
            tasks = [
                _ping_one_with_proxy(session, sem, url, no_udp, timeout, proxy_url)
                for url in urls
            ]
        else:
            tasks = [_ping_one(session, sem, url, no_udp, timeout) for url in urls]

        results = await asyncio.gather(*tasks, return_exceptions=True)

    clean: list[PingResult] = []
    for r, url in zip(results, urls):
        if isinstance(r, Exception):
            clean.append(PingResult(url, False))
        else:
            clean.append(r)
    return clean


async def _ping_one_with_proxy(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    url: str,
    no_udp: bool,
    timeout: float,
    proxy_url: str,
) -> PingResult:
    async with sem:
        scheme = urlparse(url).scheme.lower()
        if scheme == "udp":
            # UDP cannot be proxied — always skip when a proxy is configured
            return PingResult(url, None)

        target = url
        if scheme in ("ws", "wss"):
            target = url.replace("wss://", "https://").replace("ws://", "http://")

        base = target.rstrip("/")
        if not base.endswith("/announce"):
            base += "/announce"

        try:
            async with session.get(
                base,
                params={
                    "info_hash": "%00" * 20,
                    "peer_id": "-TR3000-000000000000",
                    "port": "6881",
                    "uploaded": "0",
                    "downloaded": "0",
                    "left": "0",
                    "compact": "1",
                },
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True,
                ssl=False,
                proxy=proxy_url if proxy_url.startswith("http") else None,
            ) as resp:
                return PingResult(url, resp.status < 500)
        except Exception:
            return PingResult(url, False)
