"""
ping_worker.py — Standalone ping worker, invoked inside an ephemeral container

Usage:
    python3 -m app.ping_worker

Reads tracker URLs from stdin (one per line).
Writes a JSON array of {url, up, latency_ms} to stdout on completion.

This module is the entry point for the ephemeral ping container spawned by
ping.py when VPN_CONTAINER is set. The container runs on the VPN network
namespace (--network=container:<vpn_container>) so all outbound traffic
exits through the VPN tunnel automatically.

Using stdin/stdout avoids any filesystem coordination between the host and
the container — no temp files, no volume mounts, no host path detection.
"""

from __future__ import annotations

import asyncio
import json
import sys


async def run() -> None:
    from app.core.ping import ping_all
    from app.core.latency import measure_all

    urls = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]

    if not urls:
        print("[]", flush=True)
        return

    ping_results = await ping_all(urls, no_udp=False, timeout=10.0)
    passed = [r.url for r in ping_results if r.up is True]
    latency_map = await measure_all(passed, timeout_ms=3000)

    results = [
        {
            "url": r.url,
            "up": r.up,
            "latency_ms": latency_map.get(r.url) if r.up else None,
        }
        for r in ping_results
    ]

    print(json.dumps(results), flush=True)
    print(f"[ping_worker] Done. {len(passed)}/{len(urls)} passed.", file=sys.stderr, flush=True)


if __name__ == "__main__":
    asyncio.run(run())
