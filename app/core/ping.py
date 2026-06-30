"""
ping.py — Async tracker connectivity checker

Tests trackers via:
  - UDP: raw BitTorrent UDP tracker protocol "connect" handshake
  - HTTP/HTTPS: announce request, any non-5xx response counts as reachable
  - WS/WSS: mapped to HTTP/HTTPS equivalent (most WS trackers accept plain HTTP announce)

When no_udp=True (proxy modes), UDP trackers are skipped entirely rather than
tested — SOCKS5 and HTTP CONNECT proxies cannot tunnel UDP traffic.

Proxy support:
  - HTTP/HTTPS proxy URLs (http://host:port) use aiohttp's native per-request
    `proxy=` kwarg.
  - SOCKS5 proxy URLs (socks5://host:port) use aiohttp-socks's ProxyConnector
    as the session's connector — aiohttp has no native SOCKS5 support.

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
ANNOUNCE_PARAMS = {
    "info_hash": "%00" * 20,
    "peer_id": "-TR3000-000000000000",
    "port": "6881",
    "uploaded": "0",
    "downloaded": "0",
    "left": "0",
    "compact": "1",
}


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


def _announce_url(url: str) -> str:
    """Maps ws/wss to http/https and ensures the URL ends with /announce."""
    target = url
    scheme = urlparse(url).scheme.lower()
    if scheme in ("ws", "wss"):
        target = url.replace("wss://", "https://").replace("ws://", "http://")
    base = target.rstrip("/")
    if not base.endswith("/announce"):
        base += "/announce"
    return base


async def _ping_http(
    session: aiohttp.ClientSession,
    url: str,
    timeout: float,
    http_proxy: str | None = None,
) -> bool:
    """
    http_proxy: an http:// proxy URL applied per-request via aiohttp's native
    proxy kwarg. None for direct connections or when the session's connector
    already routes everything through a SOCKS5 proxy.
    """
    base = _announce_url(url)
    try:
        async with session.get(
            base,
            params=ANNOUNCE_PARAMS,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
            ssl=False,
            proxy=http_proxy,
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
    http_proxy: str | None = None,
) -> PingResult:
    async with sem:
        scheme = urlparse(url).scheme.lower()
        if scheme in ("http", "https", "ws", "wss"):
            return PingResult(url, await _ping_http(session, url, timeout, http_proxy))
        elif scheme == "udp":
            if no_udp:
                return PingResult(url, None)   # proxy modes can't tunnel UDP
            return PingResult(url, await _ping_udp(url, timeout))
        else:
            return PingResult(url, False)


def _build_connector(proxy_url: str | None) -> aiohttp.BaseConnector:
    """
    Returns a SOCKS5 ProxyConnector if proxy_url is a socks5:// URL, otherwise
    a plain TCPConnector (used directly, or with aiohttp's per-request `proxy=`
    kwarg for HTTP proxies).
    """
    if proxy_url and proxy_url.startswith("socks5"):
        from aiohttp_socks import ProxyConnector
        return ProxyConnector.from_url(proxy_url, limit=MAX_CONCURRENCY, ssl=False)
    return aiohttp.TCPConnector(ssl=False, limit=MAX_CONCURRENCY)


async def ping_all(
    urls: list[str],
    no_udp: bool = False,
    timeout: float = 10.0,
    proxy_url: str | None = None,
) -> list[PingResult]:
    """
    Pings every tracker URL concurrently (capped at MAX_CONCURRENCY).

    proxy_url:
      - "socks5://host:port" — routed via aiohttp-socks's ProxyConnector for the
        whole session. UDP trackers are always skipped (no_udp should be True).
      - "http://host:port"   — routed via aiohttp's native per-request proxy kwarg.
        UDP trackers are always skipped (no_udp should be True).
      - None — direct connection, or VPN-routed at the network layer (the
        container's own traffic already exits through the VPN network in that
        case, so no proxy_url is needed).
    """
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    connector = _build_connector(proxy_url)
    http_proxy = proxy_url if proxy_url and proxy_url.startswith("http") else None

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            _ping_one(session, sem, url, no_udp, timeout, http_proxy)
            for url in urls
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    clean: list[PingResult] = []
    for r, url in zip(results, urls):
        if isinstance(r, Exception):
            clean.append(PingResult(url, False))
        else:
            clean.append(r)
    return clean
