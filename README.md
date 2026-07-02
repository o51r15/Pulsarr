# Trackarr

Automatically collects BitTorrent trackers from multiple sources, tests them for liveness, and injects the working ones into qBittorrent — on a schedule or on demand.

---

## Quick Start

**1. Create a `docker-compose.yml`:**

```yaml
services:
  trackarr:
    image: ghcr.io/o51r15/trackarr:latest
    container_name: trackarr
    restart: unless-stopped
    ports:
      - "7374:7374"
    volumes:
      - ./data:/app/data
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - QBT_URL=http://192.168.1.x:8080
      - QBT_USER=admin
      - QBT_PASS=yourpassword
```

**2. Start it:**

```bash
docker compose up -d
```

**3. Open the GUI:**

```
http://<your-host-ip>:7374
```

Hit **Run Now** on the Execution tab. That is it.

---

## VPN Setup (optional)

If you run qBittorrent behind a VPN container like Gluetun, Trackarr can route all tracker pings through it while still reaching qBittorrent directly on your local network. Add one environment variable:

```yaml
environment:
  - QBT_URL=http://192.168.1.x:56923   # your qBittorrent LAN address
  - QBT_USER=admin
  - QBT_PASS=yourpassword
  - VPN_CONTAINER=gluetun               # name of your running VPN container
```

Trackarr verifies at startup that the VPN is actually routing traffic by comparing the external IP seen inside the VPN container against its own. If they match, runs are aborted until the VPN is confirmed working. Both IPs are shown on the Config tab.

> The Docker socket mount (`/var/run/docker.sock`) is required — it is how Trackarr spawns the ephemeral ping container on the VPN network.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `QBT_URL` | Yes | qBittorrent Web UI URL, e.g. `http://192.168.1.x:8080` |
| `QBT_USER` | Yes | qBittorrent username |
| `QBT_PASS` | Yes | qBittorrent password |
| `VPN_CONTAINER` | No | Name of your VPN container (e.g. `gluetun`). Routes all pings through it. |
| `GITHUB_TOKEN` | No | GitHub personal access token. Raises the API rate limit for tracker source discovery. |
| `PUSHOVER_USER` | No | Pushover user key — enables push notifications on run completion. |
| `PUSHOVER_TOKEN` | No | Pushover API token. Required alongside `PUSHOVER_USER`. |
| `WEBHOOK_URL` | No | A URL to POST a JSON payload to on run completion. |
| `OLLAMA_URL` | No | URL of an Ollama instance, e.g. `http://192.168.1.x:11434`. Enables AI-powered quality scoring in the Discovery tab. |
| `OLLAMA_MODEL` | No | Model to use with Ollama, e.g. `gemma4:latest`. Required alongside `OLLAMA_URL`. |

All other settings (history retention, latency timeout, tracker source lists, notification toggles, proxy settings) are managed through the GUI Config tab and stored in your mounted data volume.

---

## How It Works

Each run goes through these steps:

1. **Collect** — fetches tracker URLs from all configured sources: raw `.txt` lists, GitHub repos (cached by commit SHA so unchanged repos are not re-fetched), website scrapes, and any manually added entries
2. **Filter** — skips trackers currently in sleep or hibernate state (progressive backoff: trackers that repeatedly fail get parked for 48h, then 7 days)
3. **Ping** — tests every active tracker for liveness using the correct protocol per URL scheme (UDP BitTorrent connect, HTTP/HTTPS announce, WebSocket)
4. **Latency** — measures TCP latency for every tracker that passed
5. **Inject** — logs into qBittorrent and sets the working tracker list as the global default (applied to all torrents automatically)

Runs can be triggered manually or scheduled from the Scheduler tab (daily, weekly, hourly, or a fixed interval — no cron required).

---

## Tabs

| Tab | What it does |
|---|---|
| **Execution** | Run Now / Abort, live log, run history |
| **Config** | Connection mode, credentials status, settings |
| **Sources** | Manage tracker source lists (URLs, GitHub repos, website scrapes, manual entries) |
| **Stats** | Per-tracker uptime, latency, and sleep state |
| **Discovery** | Find new tracker sources — well-known aggregators plus GitHub search |
| **Scheduler** | Set up automatic runs on a schedule |

---

## Data and Persistence

Everything is stored in your mounted `./data` directory:

```
data/
├── config.json               Settings (editable from the GUI)
├── sources.json              Configured tracker sources
├── tracker-source-cache.json GitHub SHA cache (avoids redundant API calls)
├── tracker-history.json      7-day per-tracker run history
├── tracker-sleep.json        Sleep/hibernate state
└── schedules.json            Scheduled run definitions
```

Back this directory up to preserve your sources and history.

---

## Connection Modes

| Mode | How to use | UDP trackers |
|---|---|---|
| **Direct** | Default — no extra config needed | Supported |
| **VPN container** | Set `VPN_CONTAINER` env var | Supported |
| **SOCKS5 / HTTP proxy** | Set in the GUI Config tab | Skipped (proxies cannot tunnel UDP) |
