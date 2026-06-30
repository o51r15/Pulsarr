#!/usr/bin/env python3
"""
trackerping - BitTorrent tracker connectivity checker
Part of the Trackarr project: https://github.com/o51r15/trackarr

Usage:
    trackerping -o /data/working_trackers.txt /data/active_raw.txt
    trackerping -l -o /data/working_trackers.txt /data/active_raw.txt
    trackerping -l --no-udp -o /data/working_trackers.txt /data/active_raw.txt

NOTE: --no-udp must be passed when using a SOCKS5 or HTTP proxy.
UDP traffic cannot be tunnelled through SOCKS5 (requires UDP ASSOCIATE support,
which most proxies do not implement) or HTTP CONNECT proxies. Passing --no-udp
causes all udp:// trackers to be skipped rather than tested and silently failed.
"""

import asyncio
import argparse
import socket
import struct
import random
import sys
from urllib.parse import urlparse

import aiohttp

CONNECT_MAGIC = 0x41727101980
MAX_CONCURRENCY = 150


class UDPPingProtocol(asyncio.DatagramProtocol):
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
            action, rtid, _ = struct.unpack('>IIQ', data[:16])
            self.result.set_result(action == 0 and rtid == self.tid)
        else:
            self.result.set_result(False)

    def error_received(self, exc: Exception) -> None:
        if not self.result.done():
            self.result.set_result(False)

    def connection_lost(self, exc: Exception | None) -> None:
        if not self.result.done():
            self.result.set_result(False)


async def ping_udp(url: str, timeout: float) -> bool:
    p = urlparse(url)
    host = p.hostname or ''
    port = p.port or 80
    tid = random.randint(0, 0xFFFFFFFF)
    pkt = struct.pack('>QII', CONNECT_MAGIC, 0, tid)
    try:
        loop = asyncio.get_event_loop()
        infos = await loop.run_in_executor(
            None,
            lambda: socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
        )
        if not infos:
            return False
        addr = infos[0][4]
        proto = UDPPingProtocol(pkt, tid)
        transport, _ = await asyncio.wait_for(
            loop.create_datagram_endpoint(lambda: proto, remote_addr=addr),
            timeout=timeout
        )
        try:
            return await asyncio.wait_for(proto.result, timeout=timeout)
        finally:
            transport.close()
    except Exception:
        return False


async def ping_http(session: aiohttp.ClientSession, url: str, timeout: float) -> bool:
    base = url.rstrip('/')
    if not base.endswith('/announce'):
        base += '/announce'
    try:
        async with session.get(
            base,
            params={
                'info_hash': '%00' * 20,
                'peer_id': '-TR3000-000000000000',
                'port': '6881',
                'uploaded': '0',
                'downloaded': '0',
                'left': '0',
                'compact': '1',
            },
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
            ssl=False,
        ) as resp:
            # Any non-5xx response means the tracker is reachable
            return resp.status < 500
    except Exception:
        return False


async def ping_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    url: str,
    no_udp: bool,
    timeout: float,
) -> tuple[str, bool | None]:
    """Returns (url, True/False/None).  None means skipped."""
    async with sem:
        scheme = urlparse(url).scheme.lower()
        if scheme in ('http', 'https'):
            return url, await ping_http(session, url, timeout)
        elif scheme in ('ws', 'wss'):
            # Treat as HTTP - most WS trackers respond to plain HTTP announce
            alt = url.replace('wss://', 'https://').replace('ws://', 'http://')
            return url, await ping_http(session, alt, timeout)
        elif scheme == 'udp':
            if no_udp:
                return url, None  # skipped
            return url, await ping_udp(url, timeout)
        else:
            return url, False


async def run(args: argparse.Namespace) -> None:
    with open(args.input) as f:
        urls = [
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith('#')
        ]

    if args.log:
        print(f'[trackerping] {len(urls)} trackers to test', flush=True)
        if args.no_udp:
            print(
                '[trackerping] --no-udp active: UDP trackers will be skipped '
                '(SOCKS5 and HTTP proxies cannot tunnel UDP traffic)',
                flush=True,
            )

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    connector = aiohttp.TCPConnector(ssl=False, limit=MAX_CONCURRENCY)

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [ping_one(session, sem, url, args.no_udp, args.timeout) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    working: list[str] = []
    skipped = 0
    for entry in results:
        if isinstance(entry, Exception):
            continue
        url, ok = entry
        if ok is None:
            skipped += 1
        elif ok:
            working.append(url)
        if args.log and ok is not None:
            print(f'[trackerping] {"UP  " if ok else "DOWN"}: {url}', flush=True)

    with open(args.output, 'w') as f:
        if working:
            f.write('\n'.join(working) + '\n')

    if args.log:
        down = len(urls) - len(working) - skipped
        print(
            f'[trackerping] Done: {len(working)} up  |  {down} down  |  {skipped} skipped (UDP/no-udp)',
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Trackarr tracker connectivity checker',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('input', help='Input file — one tracker URL per line')
    parser.add_argument('-o', '--output', required=True, help='Output file for working trackers')
    parser.add_argument('-l', '--log', action='store_true', help='Verbose logging to stdout')
    parser.add_argument(
        '--no-udp',
        dest='no_udp',
        action='store_true',
        help='Skip UDP trackers (required for SOCKS5/HTTP proxy modes)',
    )
    parser.add_argument('--timeout', type=float, default=10.0, help='Per-tracker timeout in seconds')
    args = parser.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == '__main__':
    main()
