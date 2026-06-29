# Trackarr

Automatically downloads, pings, and injects BitTorrent trackers into qBittorrent.  
Runs behind a VPN-routed Docker network so tracker pings exit through your VPN.

## Features

- Multi-source tracker collection (raw URL lists, GitHub repos, website scrapes, manual entries)
- Docker-based ping mechanism to test trackers through your VPN network
- Latency measurement for all surviving trackers
- Automatic sleep/hibernate system with progressive backoff for repeated failures
- Per-tracker history with uptime percentages and trend indicators
- Source discovery engine (well-known aggregators + rate-limited GitHub search)
- Web GUI served on port 7374 — all 5 tabs in one HTML file

## Requirements

- Windows host with PowerShell 5.1+
- Docker Desktop running
- A VPN Docker network (e.g. gluetun, Mullvad, etc.)
- The `local-trackerping` Docker image (separate build — see your homelab setup)
- qBittorrent with Web API enabled

## Setup

1. Copy `homelab-config.example.json` to `homelab-config.json`
2. Fill in your qBittorrent URL, credentials, and Docker network name
3. Set `tp.dir` to the full path of this directory
4. Set `tp.script` to the full path of `trackerping.ps1`
5. Run the bridge: `powershell -ExecutionPolicy Bypass -File trackarr-bridge.ps1`
6. Open http://localhost:7374 in your browser

## Encrypt your qBittorrent password

Run this in PowerShell to generate the encrypted value for `homelab-config.json`:

```powershell
ConvertFrom-SecureString (ConvertTo-SecureString "your_password" -AsPlainText -Force)
```

Paste the output as the value of `tp.pass` in `homelab-config.json`.

## File layout

```
trackarr/
├── trackerping.ps1          Core script — collect, ping, inject
├── tracker-discovery.ps1    Finds new tracker list sources
├── trackarr-bridge.ps1      HTTP bridge (serves GUI + API on port 7374)
├── trackarr-gui.html        Single-file web GUI
├── tracker_urls.txt         Raw .txt list URLs (one per line)
├── homelab-config.json      Your config (gitignored)
├── homelab-config.example.json  Config template
├── bridge-config.json       Bridge port config
└── tracker-data/            Runtime data (gitignored)
    ├── tracker-sources.json     GitHub/scrape/manual sources
    ├── tracker-source-cache.json  GitHub tree cache
    ├── tracker-sleep.json       Sleep/hibernate state
    └── tracker-history.json     Per-tracker run history
```

## Roadmap

- [ ] Docker image packaging (PowerShell 7 + Linux)
- [ ] Replace Windows DPAPI credential storage for Linux compatibility
- [ ] Native ping mechanism (remove Docker-in-Docker dependency)
- [ ] Scheduler built into the bridge
