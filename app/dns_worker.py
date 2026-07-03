"""
dns_worker.py — Standalone DNS resolver, invoked inside an ephemeral container

Usage:
    python3 -m app.dns_worker

Reads tracker URLs from stdin (one per line).
Writes a JSON array of {url, ip} to stdout on completion, where ip is null
if resolution failed.

This module is the entry point for the ephemeral DNS container spawned by
collect.py when VPN_CONTAINER is set. The container runs on the VPN network
namespace (--network=container:<vpn_container>) so all DNS queries exit
through the VPN tunnel automatically — the same guarantee that ping_worker
provides for connectivity checks.

Synchronous resolution is intentional here: we're already inside an
ephemeral single-purpose container and the overhead of asyncio for a
batch of sequential getaddrinfo calls is not worth the complexity.
"""

from __future__ import annotations

import json
import socket
import sys
from urllib.parse import urlparse


def _resolve(host: str) -> str | None:
    """Resolve hostname to first IPv4 address. Returns None on any failure."""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        return infos[0][4][0] if infos else None
    except Exception:
        return None


def main() -> None:
    urls = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]

    if not urls:
        print("[]", flush=True)
        return

    results = []
    for url in urls:
        p = urlparse(url)
        host = p.hostname or ""
        ip = _resolve(host) if host else None
        results.append({"url": url, "ip": ip})

    print(json.dumps(results), flush=True)

    resolved = sum(1 for r in results if r["ip"])
    print(
        f"[dns_worker] Done. {resolved}/{len(urls)} resolved.",
        file=sys.stderr,
        flush=True,
    )


if __name__ == "__main__":
    main()
