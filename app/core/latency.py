"""
latency.py — TCP connect latency measurement

For each tracker that passed the ping check, measures TCP connect time
to indicate relative responsiveness. Replaces the PowerShell runspace pool
implementation with asyncio.gather + asyncio.open_connection.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class LatencyResult:
    url: str
    latency_ms: int | None   # None if connection failed


async def _measure_one(url: str, timeout_s: float) -> LatencyResult:
    p = urlparse(url)
    host = p.hostname
    port = p.port or (443 if p.scheme in ("https", "wss") else 80)

    if not host:
        return LatencyResult(url, None)

    start = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout_s
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return LatencyResult(url, elapsed_ms)
    except Exception:
        return LatencyResult(url, None)


async def measure_all(urls: list[str], timeout_ms: int = 3000) -> dict[str, int | None]:
    """Returns a dict of url -> latency_ms (None if unreachable)."""
    timeout_s = timeout_ms / 1000.0
    tasks = [_measure_one(url, timeout_s) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out: dict[str, int | None] = {}
    for r, url in zip(results, urls):
        if isinstance(r, Exception):
            out[url] = None
        else:
            out[url] = r.latency_ms
    return out
