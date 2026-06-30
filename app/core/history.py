"""
history.py — Per-tracker run history with 7-day retention

Stores results from every run, keyed by tracker URL. Each entry holds a list
of timestamped (status, latency_ms) results. Entries older than
config.history_days are pruned on every write. Trackers no longer present in
the current pool are removed entirely (stale key pruning).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

HISTORY_FILE = Path("/app/data/tracker-history.json")


def load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {"meta": {"last_run": None, "total_runs": 0}, "trackers": {}}
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read tracker-history.json: %s", exc)
        return {"meta": {"last_run": None, "total_runs": 0}, "trackers": {}}


def save_history(history: dict) -> None:
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not save tracker-history.json: %s", exc)


def record_run(
    results: list[dict],          # [{url, status, latency_ms}, ...]
    known_trackers: set[str],     # current full tracker pool — for stale key pruning
    history_days: int = 7,
) -> dict:
    """
    Appends this run's results to history, prunes entries older than
    history_days, and removes trackers no longer in known_trackers.
    Returns the updated history dict (also written to disk).
    """
    history = load_history()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=history_days)

    history["meta"]["last_run"] = now.isoformat()
    history["meta"]["total_runs"] = int(history["meta"].get("total_runs", 0)) + 1

    trackers = history.get("trackers", {})

    for r in results:
        url = r["url"]
        entry = {"ts": now.isoformat(), "status": r["status"], "latency_ms": r.get("latency_ms")}
        runs = trackers.get(url, {}).get("runs", [])
        runs.insert(0, entry)
        trackers[url] = {"runs": runs}

    # Prune entries older than cutoff, and drop trackers with no remaining runs
    pruned_trackers = {}
    for url, data in trackers.items():
        if url not in known_trackers:
            continue   # stale key — tracker no longer in current pool
        fresh_runs = [
            run for run in data.get("runs", [])
            if _safe_parse(run.get("ts")) and _safe_parse(run["ts"]) > cutoff
        ]
        if fresh_runs:
            pruned_trackers[url] = {"runs": fresh_runs}

    history["trackers"] = pruned_trackers
    save_history(history)
    return history


def _safe_parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None
