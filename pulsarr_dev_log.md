# Pulsarr — Development Log

---

## ✅ Completed

---

### 1. Project Rename → Pulsarr
*Completed: 2026-07-02 | Commit: `b312857`*

- Renamed project from **Trackarr** to **Pulsarr** across all files
- Updated: `static/gui.html` (page title, header logo `TR→PS`, `h1` text, sessionStorage keys)
- Updated: `README.md` (heading, service name, image name, container name, prose references)
- Updated: `Dockerfile` (image title label, GitHub source URL)
- Updated: `docker-compose.yml` (header comments, service name, image name, container name)
- Updated: `app/main.py` (startup/shutdown log messages, FastAPI app title)
- Updated: `app/core/notify.py` (Pushover notification title)
- Updated: `app/core/ping.py` (`IMAGE_NAME` constant, docstring)
- Updated: `app/core/discovery.py` (`DEFAULT_USER_AGENT`)
- Updated: `app/core/collect.py` (`DEFAULT_USER_AGENT`)
- Updated: `app/network.py` (docstring)
- GitHub repo renamed to `o51r15/Pulsarr` — Docker image now publishes as `ghcr.io/o51r15/pulsarr:latest`

---

### 2. Config — Max Trackers Import Limit
*Completed: 2026-07-02 | Commit: `b312857`*

- Added `max_trackers: int = 20` to `AppConfig` in `app/config.py`
  - Validated: clamped to range 1–500
- Added **Max Trackers to Import** field to the Settings panel in the GUI Config tab
  - Populated on config load, included in config save
  - Helper text: "Top N trackers by latency sent to qBittorrent. Default: 20."
- Updated `config.example.json` with `"max_trackers": 20`
- After latency measurement, trackers are sorted ascending by latency and sliced to `max_trackers` before injection
  - Log shows how many were dropped and what the cap was

> **Note:** Injection sort was later replaced by score-based sort (see item 3 below). `max_trackers` still applies — top N by score are injected.

---

### 3. Tracker Scoring System
*Completed: 2026-07-03 | Commit: `28af4fc`*

**New file: `app/core/scoring.py`**

Composite 0–100 score per tracker from four components:

| Component | Points | Logic |
|---|---|---|
| Protocol | 10 | UDP = 10, HTTP/HTTPS/WS/WSS = 0 |
| Latency | 30 | Normalized across current field — fastest = 30, slowest = 0 |
| Uptime | 30 | % of UP results across full history window × 30 |
| AI Stability | 30 | Ollama rates run-pattern stability (0 if not configured) |

- If Ollama is not configured or fails: AI pts = 0, score normalized from 70 → 100 so it remains comparable
- AI call: single batch prompt with all trackers (URL, protocol, latency, uptime%, run pattern as `UUUDUU...`). Returns JSON array. Falls back silently on any failure — timeout, bad JSON, HTTP error, not configured
- Scores saved to `/app/data/tracker-scores.json` after every run
- `load_scores()` / `save_scores()` public API

**`app/core/run.py` changes**

- Scoring runs after history is written (step 4), so it reads freshly updated data
- Inject sort changed from raw latency → score descending
- Top N by score selected (respects `max_trackers`)
- Logs: scoring mode (AI vs deterministic), top tracker with score/latency/uptime

**`app/api/router.py`**

- Added `GET /api/tracker-scores` endpoint

**`static/gui.html` — Stats page**

- Score badge on every active tracker row: green ≥80, yellow ≥60, red <60
- Sort toggle above the active section: **Score** (default) | **Latency**
- Toggle re-renders without refetching — last data cached in `lastStatsData`
- Green "AI" label shown when AI scoring was active for the last run
- `loadStats()` now fetches history, sleep, and scores in parallel

---

### 4. Stats Page — Active Trackers Sort by Latency (pre-scoring)
*Completed: 2026-07-02 | Commit: `2803f8a`*

- Active tracker list on the Stats page defaulted to sort by uptime %; changed to sort by latency ascending before the scoring system was added
- Superseded by the Score/Latency toggle added in item 3 above

---

### 5. URL Resolution & IP-level Deduplication Before Ping
*Completed: 2026-07-03 | Commits: `9c83a50`, `11d5ff8`*

**New file: `app/dns_worker.py`**

- Standalone resolver invoked inside an ephemeral Docker container on the VPN network namespace
- Reads tracker URLs from stdin (one per line)
- Resolves each hostname to IPv4 via `socket.getaddrinfo`
- Outputs `[{url, ip}]` JSON array to stdout
- Same stdin/stdout pattern as `ping_worker.py`

**`app/core/collect.py` — `resolve_and_deduplicate()`**

- New function added, runs as step 1.7 in the pipeline (after sleep filtering, before ping)
- Deduplication key: `(ip, port)` — two URLs pointing to the same IP and port = true duplicate
- Different port on same IP = different service, both kept
- When duplicates exist: UDP preferred over HTTP/HTTPS/WS/WSS; ties broken by shorter URL
- DNS failure handling: failed URLs are kept and only deduplicated against identical `hostname:port` strings — a resolution failure never silently drops a tracker
- **VPN routing**: when `VPN_CONTAINER` is set, DNS queries go through `_resolve_all_via_container()` which spawns an ephemeral container on `--network=container:{vpn_container}` so all lookups exit through the tunnel — same guarantee as ping
- Direct/proxy mode: in-process async resolution with 50 concurrent lookups via semaphore
- Logs clearly show which path was taken and how many duplicates were removed

**`app/core/run.py`**

- Step 1.7 inserted: `active_trackers = await collect.resolve_and_deduplicate(active_trackers, log, vpn_container=vpn_container)`

---

## 📋 Backlog / Planned

---

### 6. Rules Engine *(future)*

Configurable rules for tracker filtering/selection, evolving from the current `max_trackers` setting.

- Example rule logic:
  - Max import: `20` trackers
  - Prefer UDP over HTTP
  - Ping must be `< 199ms`
  - OR score above a user-defined threshold
- Rules should be user-configurable via UI

---

### 7. Stalled Torrent Rules *(future)*

Separate rule set for stalled torrents — if a torrent stalls mid-run, apply an expanded tracker set.

- Default import: Top **20** trackers (controlled by `max_trackers`)
- Stalled torrent fallback: Top **100** trackers (separate configurable limit)
- Rule triggers automatically on stall detection

---

*Last updated: 2026-07-03*
