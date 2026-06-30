"""
notify.py — Pushover + webhook notifications

Both are optional and independent — either, both, or neither can be active.
Controlled by AppConfig.pushover_notify / AppConfig.webhook_notify (booleans)
plus the presence of the relevant credentials in environment variables.

Failures to send a notification are logged but never raise — a notification
failure must never fail the underlying trackerping/discovery run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def send_pushover(user_key: str, api_token: str, title: str, message: str, priority: int = 0) -> bool:
    if not user_key or not api_token:
        return False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                PUSHOVER_API_URL,
                data={
                    "token": api_token,
                    "user": user_key,
                    "title": title,
                    "message": message,
                    "priority": priority,
                },
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Pushover send failed (HTTP %d): %s", resp.status, body)
                    return False
        logger.info("Pushover sent: %s - %s", title, message)
        return True
    except Exception as exc:
        logger.warning("Pushover send error: %s", exc)
        return False


async def send_webhook(url: str, payload: dict) -> bool:
    if not url:
        return False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning("Webhook send failed (HTTP %d): %s", resp.status, body)
                    return False
        logger.info("Webhook sent: %s", payload.get("event", "?"))
        return True
    except Exception as exc:
        logger.warning("Webhook send error: %s", exc)
        return False


async def notify_run_complete(
    config, env,
    success: bool, fetched: int = 0, active: int = 0, passed: int = 0, error: str | None = None,
) -> None:
    """
    Fires both Pushover and webhook if configured/enabled. Never raises.
    Called from job completion handlers regardless of whether the run succeeded,
    failed cleanly, or crashed — not coupled to the RunSummary dataclass shape.
    """
    if config.pushover_notify and env.pushover_user and env.pushover_token:
        title = "TrackerPing"
        message = f"{passed}/{fetched} active. qBittorrent updated." if success else f"TrackerPing FAILED: {error}"
        priority = 0 if success else 1
        await send_pushover(env.pushover_user, env.pushover_token, title, message, priority)

    if config.webhook_notify and env.webhook_url:
        await send_webhook(env.webhook_url, {
            "event": "run_complete" if success else "run_failed",
            "success": success,
            "fetched": fetched,
            "active": active,
            "passed": passed,
            "error": error,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


async def notify_discovery_complete(config, env, candidate_count: int, success: bool, error: str | None) -> None:
    if config.pushover_notify and env.pushover_user and env.pushover_token:
        title = "Trackarr Discovery"
        message = (
            f"{candidate_count} new candidate(s) pending approval." if success
            else f"Discovery FAILED: {error}"
        )
        await send_pushover(env.pushover_user, env.pushover_token, title, message, 0 if success else 1)

    if config.webhook_notify and env.webhook_url:
        await send_webhook(env.webhook_url, {
            "event": "discovery_complete" if success else "discovery_failed",
            "success": success,
            "candidates": candidate_count,
            "error": error,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
