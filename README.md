# Trackarr

Automatically downloads, pings, and injects BitTorrent trackers into qBittorrent.
Supports VPN Docker networks, SOCKS5 proxies, HTTPS proxies, or direct connections.

## Features

- Multi-source tracker collection (raw URL lists, GitHub repos, website scrapes, manual entries)
- Selectable ping mode: Docker VPN network, SOCKS5 proxy, HTTPS proxy, or direct
- Latency measurement for all surviving trackers
- Automatic sleep/hibernate system with progressive backoff for repeated failures
- Per-tracker history with uptime percentages and trend indicators
- Source discovery engine (well-known aggregators + rate-limited GitHub search)
- Web GUI on port 7374 — all 5 tabs in one HTML file

---

## Installation

### Option 1 — Windows (run directly on host)

**Requirements**
- PowerShell 5.1+
- Docker Desktop
- qBittorrent with Web API enabled

**Steps**

1. Clone the repo
2. Build the ping image from source:
   ```
   docker build -t local-trackerping ./ping
   ```
3. Copy `homelab-config.example.json` to `homelab-config.json` and fill in your settings
4. Encrypt your qBittorrent password:
   ```powershell
   ConvertFrom-SecureString (ConvertTo-SecureString "your_password" -AsPlainText -Force)
   ```
   Paste the output as `tp.pass` in `homelab-config.json`
5. Set `tp.dir` to the full path of the repo directory
6. Set `tp.script` to the full path of `trackerping.ps1`
7. Run the bridge:
   ```powershell
   powershell -ExecutionPolicy Bypass -File trackarr-bridge.ps1
   ```
8. Open http://localhost:7374

---

### Option 2 — Docker (pre-built images from CI)

Both images are published to GitHub Container Registry on every push to master.
No local builds required.

**Requirements**
- Docker
- qBittorrent with Web API enabled

**Steps**

1. Pull both images:
   ```
   docker pull ghcr.io/o51r15/trackarr:latest
   docker pull ghcr.io/o51r15/trackarr-ping:latest
   docker tag ghcr.io/o51r15/trackarr-ping:latest local-trackerping
   ```
2. Create your config:
   ```
   curl -o homelab-config.json https://raw.githubusercontent.com/o51r15/trackarr/master/homelab-config.example.json
   ```
   Edit `homelab-config.json` with your qBittorrent URL, credentials, and ping mode settings.
3. Run the bridge:
   ```
   docker run -d \
     --name trackarr \
     -p 7374:7374 \
     -v ./homelab-config.json:/app/homelab-config.json \
     -v ./tracker-data:/app/tracker-data \
     -v /var/run/docker.sock:/var/run/docker.sock \
     ghcr.io/o51r15/trackarr:latest
   ```
4. Open http://localhost:7374

> **Note:** The Docker socket mount (`/var/run/docker.sock`) is required so the bridge
> can call `docker run local-trackerping` for each ping run.

---

## Ping modes

Configured in the GUI under **Config → Ping Mode**, or as `tp.pingMode` in `homelab-config.json`.

| Mode | Description | UDP trackers |
|---|---|---|
| `docker-vpn` | Ping container joins a VPN Docker network (e.g. Gluetun). IP check confirms traffic exits through VPN. | Supported |
| `socks5` | Ping container uses a SOCKS5 proxy via `ALL_PROXY`. | **Skipped** — SOCKS5 cannot tunnel UDP |
| `https-proxy` | Ping container uses an HTTP CONNECT proxy via `HTTPS_PROXY`. | **Skipped** — HTTP proxy cannot tunnel UDP |
| `direct` | No VPN or proxy. Pings go out on the host network. | Supported |

Set `tp.proxyUrl` for SOCKS5/HTTPS proxy modes, e.g. `socks5://192.168.1.x:1080` or `http://192.168.1.x:3128`.

---

## The ping image

`local-trackerping` is built from `./ping` in this repo — it is not an external dependency.
It is a small Alpine + Python image that handles UDP announce, HTTP/HTTPS, and WebSocket trackers.
It is created and destroyed on every TrackerPing run. It is never a running service.

- **Windows install:** `docker build -t local-trackerping ./ping`
- **Docker install:** `docker pull ghcr.io/o51r15/trackarr-ping:latest && docker tag ghcr.io/o51r15/trackarr-ping:latest local-trackerping`

Rebuild after any changes to `./ping`.

---

## File layout

```
trackarr/
├── ping/
│   ├── Dockerfile           Builds the local-trackerping image (source is here)
│   ├── trackerping.py       Async ping binary — UDP, HTTP/HTTPS, WebSocket
│   └── requirements.txt
├── trackerping.ps1          Core script — collect, ping, inject
├── tracker-discovery.ps1    Finds new tracker list sources
├── trackarr-bridge.ps1      HTTP bridge (serves GUI + API on port 7374)
├── trackarr-gui.html        Single-file web GUI
├── tracker_urls.txt         Raw .txt list URLs (one per line)
├── homelab-config.json      Your config (gitignored)
├── homelab-config.example.json  Config template
├── bridge-config.json       Bridge port config
└── tracker-data/            Runtime data (gitignored)
    ├── tracker-sources.json
    ├── tracker-source-cache.json
    ├── tracker-sleep.json
    └── tracker-history.json
```

---

## Roadmap

- [ ] Replace Windows DPAPI credential storage for Linux/Docker compatibility
- [ ] Scheduler built into the bridge
