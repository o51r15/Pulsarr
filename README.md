# Trackarr

Automated BitTorrent tracker management for qBittorrent. Collects trackers from
multiple sources, pings them for liveness, measures latency, and injects the
working set into qBittorrent — on a schedule or on demand.

Rewritten in Python (FastAPI). Container-only deployment. No PowerShell, no
Windows host dependency, no Docker socket, no DPAPI.

> **Status:** Core rewrite complete (Phases 1–8) and verified, including a live
> browser walkthrough of the full GUI. Packaging is done — see `docker-compose.yml`
> and `docker-compose.vpn.example.yml` in this repo for ready-to-use deployment
> examples.

---

## Features

- Multi-source tracker collection: raw `.txt` list URLs, GitHub repo crawling
  (cached by commit SHA), website scraping, manual entries
- Async ping engine: UDP BitTorrent protocol, HTTP/HTTPS announce, WebSocket
- TCP latency measurement for every tracker that passes
- Sleep/hibernate system with progressive backoff (watching → 48h sleep → 7-day
  hibernate) for repeatedly-failing trackers
- 7-day tracker history with uptime tracking
- Source discovery engine: well-known aggregators checked every run, GitHub
  search rate-limited to once per 7 days, with a preview/approve/dismiss flow
- Internal scheduler: daily, weekly, hourly, or interval-based runs — no cron,
  no external scheduler container
- Pushover and/or webhook notifications on run completion
- Auto-detected VPN routing: if the container is attached to a VPN Docker
  network, the GUI locks to VPN mode automatically; otherwise SOCKS5/HTTP
  proxy or direct connection options are available
- Single web GUI, six tabs: Execution, Config, Sources, Stats, Discovery,
  Scheduler — live log streaming via Server-Sent Events, no polling

---

## Architecture

One container does everything. The HTTP API/GUI server and the ping engine
both run in-process inside the same FastAPI app — there is no second image,
no Docker-in-Docker, no ephemeral ping container. Pinging is a native asyncio
task, not a subprocess.

VPN routing is handled entirely by the Docker network the container is
attached to (e.g. via Gluetun or another VPN provider container) — Trackarr
detects this at startup by inspecting its own network gateway and adjusts
the GUI accordingly. There is no manual VPN network name to configure.

All credentials are environment variables, set in your `docker-compose.yml`.
Nothing sensitive is stored in a config file or entered through the GUI.

---

## Installation

Two ready-to-use compose files are included in this repo:

- **`docker-compose.yml`** — direct connection or SOCKS5/HTTP proxy (no VPN
  container). Exposes port 7374 directly.
- **`docker-compose.vpn.example.yml`** — VPN-routed via a provider container
  like Gluetun. Trackarr shares the VPN container's network namespace; VPN
  attachment is auto-detected at startup, no manual configuration needed.

Copy whichever matches your setup, fill in `QBT_URL`/`QBT_USER`/`QBT_PASS`
(and any optional vars you want), then:

```
docker compose up -d
```

Open `http://<host>:7374`.

---

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `QBT_URL` | Yes | qBittorrent Web UI URL |
| `QBT_USER` | Yes | qBittorrent username |
| `QBT_PASS` | Yes | qBittorrent password (plain text — scope this container's network access accordingly) |
| `GITHUB_TOKEN` | No | Raises GitHub API rate limit for repo crawling/discovery |
| `PUSHOVER_USER` | No | Pushover user key, for completion notifications |
| `PUSHOVER_TOKEN` | No | Pushover API token |
| `WEBHOOK_URL` | No | Generic webhook URL, POSTed a JSON payload on run completion |

Non-sensitive settings (history retention, latency timeout, proxy URL,
connection mode, tracker URL list, notification toggles) live in
`/app/data/config.json` and are editable from the GUI's Config tab.

---

## Connection modes

| Mode | How it's selected | UDP trackers |
|---|---|---|
| VPN | Auto-detected from the container's network gateway | Supported |
| Direct | Default when no VPN network is detected | Supported |
| Proxy — SOCKS5 | Selected in the GUI when no VPN is detected; uses `aiohttp-socks` | **Skipped** — SOCKS5 cannot tunnel UDP |
| Proxy — HTTP | Selected in the GUI when no VPN is detected | **Skipped** — HTTP CONNECT proxies cannot tunnel UDP |

---

## File layout

```
trackarr/
├── app/
│   ├── main.py            FastAPI app, startup/shutdown lifecycle
│   ├── config.py          Env var credentials + AppConfig (non-sensitive settings)
│   ├── network.py         VPN auto-detection
│   ├── api/
│   │   ├── router.py      All REST endpoints
│   │   └── jobs.py        Async job manager, SSE streaming
│   └── core/
│       ├── collect.py     Source collection pipeline
│       ├── ping.py        Async ping engine (UDP, HTTP/HTTPS, WS, SOCKS5/HTTP proxy)
│       ├── latency.py     TCP latency measurement
│       ├── inject.py      qBittorrent API client
│       ├── sleep.py       Sleep/hibernate state
│       ├── history.py     7-day tracker run history
│       ├── sources.py     Tracker source CRUD + discovery state
│       ├── discovery.py   Source discovery engine
│       ├── scheduler.py   Internal async scheduler
│       ├── notify.py      Pushover + webhook
│       └── run.py         Full pipeline orchestration
├── static/
│   └── gui.html            Single-file web GUI
├── data/                   Mounted volume — all persistent state (gitignored)
├── Dockerfile
├── docker-compose.yml
├── docker-compose.vpn.example.yml
├── config.example.json
└── requirements.txt
```

---

## API

Interactive docs at `/api/docs` once running.

---

## Roadmap

- [ ] SQLite for tracker history, if JSON file size becomes a problem at scale
- [ ] Multi-client support (Transmission, rTorrent/ruTorrent, Deluge) — see
      local dev notes for research findings; requires a client abstraction
      layer since most other clients lack qBittorrent's global default
      tracker list feature
