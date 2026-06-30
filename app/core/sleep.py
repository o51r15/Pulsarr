"""
sleep.py — Sleep/hibernate state for repeatedly-failing trackers

Progressive backoff:
  failures < 2   -> "watching"  (no sleep, still pinged every run)
  failures 2-5   -> "sleep"     (skip for 48 hours)
  failures > 5   -> "hibernate" (skip for 7 days / 168 hours)

A successful ping clears the tracker's failure count entirely.

State is stored in /app/data/tracker-sleep.json.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SLEEP_FILE = Path("/app/data/tracker-sleep.json")


@dataclass
class SleepEntry:
    state: str              # "watching" | "sleep" | "hibernate"
    failures: int
    sleep_until: str | None   # ISO8601, or None for "watching"
    last_failure: str


def load_sleep_state() -> dict[str, SleepEntry]:
    if not SLEEP_FILE.exists():
        return {}
    try:
        raw = json.loads(SLEEP_FILE.read_text(encoding="utf-8"))
        return {
            url: SleepEntry(
                state=v.get("state", "watching"),
                failures=int(v.get("failures", 0)),
                sleep_until=v.get("sleep_until"),
                last_failure=v.get("last_failure", ""),
            )
            for url, v in raw.items()
        }
    except Exception as exc:
        logger.warning("Could not read tracker-sleep.json: %s", exc)
        return {}


def save_sleep_state(state: dict[str, SleepEntry]) -> None:
    try:
        SLEEP_FILE.parent.mkdir(parents=True, exist_ok=True)
        out = {url: asdict(entry) for url, entry in state.items()}
        SLEEP_FILE.write_text(json.dumps(out, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not save tracker-sleep.json: %s", exc)


def prune_unknown(state: dict[str, SleepEntry], known_trackers: set[str]) -> dict[str, SleepEntry]:
    """Remove sleep state entries for trackers no longer in the current pool."""
    return {url: entry for url, entry in state.items() if url in known_trackers}


def get_dormant_set(state: dict[str, SleepEntry], now: datetime | None = None) -> set[str]:
    """Returns the set of tracker URLs currently sleeping or hibernating (not yet expired)."""
    now = now or datetime.now(timezone.utc)
    dormant = set()
    for url, entry in state.items():
        if entry.state in ("sleep", "hibernate") and entry.sleep_until:
            try:
                until = datetime.fromisoformat(entry.sleep_until)
                if until > now:
                    dormant.add(url)
            except ValueError:
                continue
    return dormant


def update_after_run(
    state: dict[str, SleepEntry],
    tested_trackers: set[str],
    passed_trackers: set[str],
    now: datetime | None = None,
) -> dict[str, SleepEntry]:
    """
    Updates sleep state based on the results of a ping run.
    tested_trackers: every tracker that was actually pinged this run (active, non-dormant)
    passed_trackers: subset that came back UP
    """
    now = now or datetime.now(timezone.utc)

    for url in tested_trackers:
        if url in passed_trackers:
            state.pop(url, None)
        else:
            prev = state.get(url)
            prev_failures = prev.failures if prev else 0
            new_failures = prev_failures + 1

            if new_failures < 2:
                new_state, sleep_hours = "watching", None
            elif new_failures <= 5:
                new_state, sleep_hours = "sleep", 48
            else:
                new_state, sleep_hours = "hibernate", 168

            sleep_until = (
                (now + timedelta(hours=sleep_hours)).isoformat() if sleep_hours else None
            )

            state[url] = SleepEntry(
                state=new_state,
                failures=new_failures,
                sleep_until=sleep_until,
                last_failure=now.isoformat(),
            )

    return state


def counts(state: dict[str, SleepEntry], now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    dormant = get_dormant_set(state, now)
    sleeping = sum(1 for u in dormant if state[u].state == "sleep")
    hibernating = sum(1 for u in dormant if state[u].state == "hibernate")
    return {"sleeping": sleeping, "hibernating": hibernating, "total_dormant": len(dormant)}
