"""
network.py — VPN auto-detection

Runs once at container startup. Detects whether a VPN tunnel interface
(tun*/wg*/tap*) is present in this container's network namespace.

This is the correct signal — NOT the default gateway IP. An earlier version
of this module tried to infer VPN presence from the gateway address (treating
anything other than 172.17.0.1 as "must be a custom VPN network"), which is
wrong: docker-compose always creates its own per-project bridge network with
a non-172.17.0.1 gateway regardless of whether a VPN is involved at all. That
produced false positives for completely ordinary `docker compose up` deployments
with no VPN anywhere in the picture.

A real VPN container (Gluetun, OpenVPN, WireGuard) creates an actual tunnel
network interface — tun0, wg0, etc. — in the network namespace it shares with
Trackarr via `network_mode: service:<vpn-container>`. Checking for that
interface's existence is the only reliable signal, independent of whatever
subnet Docker happens to assign.

Result is stored in app.state and exposed via GET /api/network-mode.
It is set once at startup and never changes for the lifetime of the container.
If VPN is detected, the GUI hides proxy/direct options entirely.
If VPN is not detected, the GUI shows proxy/direct options and hides VPN references.
"""

from __future__ import annotations

import logging
import socket
import struct
from pathlib import Path
from typing import Literal

import aiohttp

logger = logging.getLogger(__name__)

ConnectionMode = Literal["vpn", "direct", "proxy"]

NET_CLASS_DIR = Path("/sys/class/net")
# Interface name prefixes created by VPN tunnel software.
# tun/tap: OpenVPN and generic TUN/TAP devices. wg: WireGuard (including Gluetun's wireguard mode).
VPN_INTERFACE_PREFIXES = ("tun", "tap", "wg")


def _read_default_gateway() -> str | None:
    """
    Parse /proc/net/route for the default route (destination 0.0.0.0).
    Returns the gateway IP string, or None if unreadable.
    Informational only — no longer used to determine VPN status.
    """
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            for line in f.readlines()[1:]:          # skip header
                fields = line.strip().split()
                if len(fields) < 3:
                    continue
                destination = fields[1]
                gateway_hex = fields[2]
                if destination == "00000000":        # 0.0.0.0 = default route
                    gw_bytes = struct.pack("<L", int(gateway_hex, 16))
                    return socket.inet_ntoa(gw_bytes)
    except Exception as exc:
        logger.debug("Could not read /proc/net/route: %s", exc)
    return None


def _detect_vpn_interface() -> str | None:
    """
    Returns the name of the first VPN tunnel interface found in this
    network namespace (e.g. "tun0", "wg0"), or None if none exist.
    """
    try:
        if not NET_CLASS_DIR.is_dir():
            return None
        for entry in sorted(NET_CLASS_DIR.iterdir()):
            name = entry.name
            if name.startswith(VPN_INTERFACE_PREFIXES):
                return name
    except Exception as exc:
        logger.debug("Could not enumerate /sys/class/net: %s", exc)
    return None


async def _fetch_external_ip() -> str | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.ipify.org",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return (await resp.text()).strip()
    except Exception as exc:
        logger.warning("Could not fetch external IP: %s", exc)
        return None


async def detect() -> dict:
    """
    Run VPN detection. Returns a dict suitable for the /api/network-mode response.

    Fields:
        mode           "vpn" | "direct" | "proxy"  — resolved mode
        vpn_detected   bool                          — True if a VPN tunnel interface was found
        vpn_interface  str | None                    — the interface name, if found (e.g. "tun0")
        gateway        str | None                    — default gateway IP (informational only)
        external_ip    str | None                    — container's external IP
    """
    gateway = _read_default_gateway()
    vpn_interface = _detect_vpn_interface()
    external_ip = await _fetch_external_ip()

    vpn_detected = vpn_interface is not None
    mode: ConnectionMode = "vpn" if vpn_detected else "direct"

    result = {
        "mode":          mode,
        "vpn_detected":  vpn_detected,
        "vpn_interface": vpn_interface,
        "gateway":       gateway,
        "external_ip":   external_ip,
    }

    if vpn_detected:
        logger.info(
            "VPN tunnel interface detected (%s) — external_ip=%s — proxy/direct options disabled.",
            vpn_interface, external_ip,
        )
    else:
        logger.info(
            "No VPN tunnel interface found — gateway=%s external_ip=%s — proxy/direct options available.",
            gateway, external_ip,
        )

    return result
