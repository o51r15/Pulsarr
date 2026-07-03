"""
scoring.py — Composite tracker scoring engine

Scores each tracker 0-100 from four components:

  Protocol     (10 pts) — UDP preferred over HTTP/HTTPS/WS
  Latency      (30 pts) — normalized across current field; fastest = 30 pts
  Uptime       (30 pts) — % of UP results across the full history window
  AI Stability (30 pts) — Ollama rates run-pattern stability/reliability

If Ollama is not configured the AI component is 0 and the 70 deterministic
points are scaled to 100, so the score is always comparable regardless of
whether AI is enabled. Any Ollama failure (timeout, bad JSON, HTTP error)
is caught and treated as AI disabled for this run — it never blocks or
crashes the main pipeline.

Scores are saved to /app/data/tracker-scores.json after each run and
served via GET /api/tracker-scores.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

SCORES_FILE = Path("/app/data/tracker-scores.json")
OLLAMA_TIMEOUT = aiohttp.ClientTimeout(total=120)
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
MAX_PATTERN_RUNS = 30   # last N runs shown to AI as U/D pattern


# ---------------------------------------------------------------------------
# Data type
# ---------------------------------------------------------------------------

@dataclass
class TrackerScore:
    url:          str
    protocol:     str         # udp | http | https | ws | wss
    protocol_pts: float       # 0-10
    latency_pts:  float       # 0-30
    uptime_pts:   float       # 0-30
    ai_pts:       float       # 0-30  (0 if AI disabled)
    ai_enabled:   bool
    total:        float       # 0-100 (normalized)
    latency_ms:   int | None
    uptime_pct:   float       # 0-100


# ---------------------------------------------------------------------------
# Deterministic components
# ---------------------------------------------------------------------------

def _protocol(url: str) -> tuple[str, float]:
    scheme = urlparse(url).scheme.lower()
    pts = 10.0 if scheme == "udp" else 0.0
    return scheme, pts


def _latency_pts(url: str, latency_map: dict, valid_latencies: list[int]) -> float:
    ms = latency_map.get(url)
    if ms is None or not valid_latencies:
        return 0.0
    lo, hi = min(valid_latencies), max(valid_latencies)
    if hi == lo:
        return 30.0
    return 30.0 * (hi - ms) / (hi - lo)


def _uptime(url: str, history: dict) -> tuple[float, float]:
    runs = history.get("trackers", {}).get(url, {}).get("runs", [])
    if not runs:
        return 0.0, 0.0
    up = sum(1 for r in runs if r.get("status") == "UP")
    pct = up / len(runs)
    return round(30.0 * pct, 2), round(pct * 100, 1)


def _pattern(url: str, history: dict) -> str:
    runs = history.get("trackers", {}).get(url, {}).get("runs", [])[:MAX_PATTERN_RUNS]
    return "".join("U" if r.get("status") == "UP" else "D" for r in runs)


# ---------------------------------------------------------------------------
# AI component (optional, never raises)
# ---------------------------------------------------------------------------

async def _ai_score_batch(
    tracker_data: list[dict],
    ollama_url: str,
    model: str,
) -> dict[str, float]:
    """
    Sends all tracker summaries in a single prompt and returns url -> ai_pts (0-30).
    Returns an empty dict on any failure so callers treat AI as disabled.
    """
    if not ollama_url or not model or not tracker_data:
        return {}

    prompt = (
        "You are evaluating the stability and reliability of BitTorrent tracker servers.\n\n"
        "For each tracker assign a stability_score from 0-30 based on:\n"
        "- Consistency: a steady all-UP pattern scores highest\n"
        "- Flapping: frequent UP/DOWN alternation scores low\n"
        "- Chronic failure: long runs of D scores 0\n"
        "- Sparse data (few runs): score conservatively around 15\n\n"
        f"Pattern is most-recent-first, up to {MAX_PATTERN_RUNS} runs (U=UP D=DOWN).\n\n"
        "Tracker data:\n"
        + json.dumps(tracker_data, indent=2)
        + "\n\nReturn ONLY a JSON array, no markdown, no other text:\n"
        '[{"url": "<url>", "stability_score": <int 0-30>}]'
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ollama_url.rstrip('/')}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
                timeout=OLLAMA_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    logger.warning("Ollama scoring returned HTTP %d", resp.status)
                    return {}
                data = await resp.json()

        raw = THINK_BLOCK.sub("", data.get("response", "")).strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()

        parsed = json.loads(raw)
        return {
            item["url"]: max(0.0, min(30.0, float(item.get("stability_score", 0))))
            for item in parsed
            if "url" in item
        }

    except Exception as exc:
        logger.warning("AI scoring failed, proceeding without AI component: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def compute_scores(
    passed_urls: set[str],
    latency_map: dict[str, int | None],
    history: dict,
    ollama_url: str = "",
    ollama_model: str = "",
) -> list[TrackerScore]:
    """
    Computes composite scores for every tracker that passed ping.
    Always returns a result — AI failure produces scores without AI component.
    Results are sorted highest score first.
    """
    ai_enabled = bool(ollama_url and ollama_model)

    valid_latencies = [
        latency_map[u] for u in passed_urls
        if latency_map.get(u) is not None
    ]

    # Build per-tracker data for deterministic components + AI input
    tracker_data = []
    for url in passed_urls:
        scheme, proto_pts      = _protocol(url)
        lat_pts                = _latency_pts(url, latency_map, valid_latencies)
        up_pts, uptime_pct     = _uptime(url, history)
        pattern                = _pattern(url, history)
        runs                   = history.get("trackers", {}).get(url, {}).get("runs", [])

        tracker_data.append({
            "url":          url,
            "protocol":     scheme,
            "protocol_pts": proto_pts,
            "latency_pts":  lat_pts,
            "uptime_pts":   up_pts,
            "uptime_pct":   uptime_pct,
            "latency_ms":   latency_map.get(url),
            "total_runs":   len(runs),
            "pattern":      pattern,
        })

    # AI scoring — one batch call, empty dict on any failure
    ai_scores: dict[str, float] = {}
    if ai_enabled:
        ai_input = [
            {
                "url":        d["url"],
                "protocol":   d["protocol"],
                "latency_ms": d["latency_ms"],
                "uptime_pct": d["uptime_pct"],
                "total_runs": d["total_runs"],
                "pattern":    d["pattern"],
            }
            for d in tracker_data
        ]
        ai_scores = await _ai_score_batch(ai_input, ollama_url, ollama_model)
        if not ai_scores:
            ai_enabled = False   # fallback: treat as no AI for normalization

    max_possible = 100.0 if ai_enabled else 70.0

    results: list[TrackerScore] = []
    for d in tracker_data:
        url      = d["url"]
        proto_pts = d["protocol_pts"]
        lat_pts   = d["latency_pts"]
        up_pts    = d["uptime_pts"]
        ai_pts    = float(ai_scores.get(url, 0.0)) if ai_enabled else 0.0

        raw   = proto_pts + lat_pts + up_pts + ai_pts
        total = round(min(raw / max_possible * 100, 100.0), 1)

        results.append(TrackerScore(
            url=url,
            protocol=d["protocol"],
            protocol_pts=round(proto_pts, 1),
            latency_pts=round(lat_pts, 1),
            uptime_pts=round(up_pts, 1),
            ai_pts=round(ai_pts, 1),
            ai_enabled=ai_enabled,
            total=total,
            latency_ms=d["latency_ms"],
            uptime_pct=d["uptime_pct"],
        ))

    results.sort(key=lambda s: s.total, reverse=True)
    return results


def save_scores(scores: list[TrackerScore]) -> None:
    try:
        SCORES_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCORES_FILE.write_text(
            json.dumps([asdict(s) for s in scores], indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("Could not save tracker-scores.json: %s", exc)


def load_scores() -> list[dict]:
    if not SCORES_FILE.exists():
        return []
    try:
        return json.loads(SCORES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
