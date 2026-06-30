# Trackarr — Developer Log
_Last updated: 2026-06-29_

---

## Architecture

### One image, two roles

There is a single Docker image: `ghcr.io/o51r15/trackarr:latest`.

It contains:
- `trackarr-bridge.ps1` — the HTTP bridge that serves the GUI and handles all API requests
- `trackerping.ps1` — the core collection/ping/inject script, invoked by the bridge per run
- `trackerping` binary — the Python ping tool installed at `/usr/local/bin/trackerping`
- `tracker-discovery.ps1` — the source discovery script, invoked by the bridge per discovery run
- `trackarr-gui.html` — the single-file web GUI, served by the bridge at `/`

The image's default entrypoint starts the bridge:
```
pwsh -NonInteractive -ExecutionPolicy Bypass -File /app/trackarr-bridge.ps1
```

### What happens during a ping run

1. User clicks Run Now in the GUI (or scheduler fires)
2. Bridge calls `Start-Process powershell.exe -File trackerping.ps1` — this is a detached child process, not a container
3. `trackerping.ps1` runs on the host (Windows) or inside the bridge container (Docker)
4. The script collects tracker URLs from all configured sources, writes them to `active_raw.txt` in `$Cfg.dir`
5. The script calls:
   ```
   docker run --rm -v "$($Cfg.dir):/data" $PingImage trackerping -l -o /data/working_trackers.txt /data/active_raw.txt
   ```
6. Docker creates a container from `$PingImage` (default: `ghcr.io/o51r15/trackarr:latest`) — the SAME image — but overrides the entrypoint with the `trackerping` command (Python binary)
7. The ping container reads `/data/active_raw.txt`, pings every tracker via UDP/HTTP/HTTPS, writes survivors to `/data/working_trackers.txt`, then exits and is destroyed (`--rm`)
8. Back in `trackerping.ps1`, the script reads `working_trackers.txt`, runs latency measurements natively in PS, emits `[TRACKER_RESULT]` lines to stdout (which the bridge streams to the GUI log), updates sleep state, and injects the tracker list into qBittorrent

### The ping container is not a service

The ping container is ephemeral. It does not run alongside the bridge. It is created at the start of step 6 and destroyed at the end of step 7. There is no `local-trackerping` image that needs to be separately maintained, pulled, built, or tagged. The ping container IS the main image invoked with `trackerping` as the command instead of the default bridge entrypoint.

### Docker socket requirement

When running as a Docker container, the bridge container needs access to the host Docker socket so it can call `docker run` to spin up the ephemeral ping container:
```
-v /var/run/docker.sock:/var/run/docker.sock
```
Without this mount, `trackerping.ps1` cannot call `docker run` from inside the container.

### Volume layout

| Volume | Host path | Container path | Purpose |
|---|---|---|---|
| tracker-data | `./tracker-data` | `/app/tracker-data` | Persistent state: sleep.json, history.json, sources.json, source-cache.json |
| data dir | `$Cfg.dir` (configurable) | `/data` | Runtime files: active_raw.txt, working_trackers.txt, combined_raw.txt, trackerping.log |

`$Cfg.dir` is whatever the user sets as `tp.dir` in `homelab-config.json`. In Docker deployment this should be set to `/data`. The bridge mounts it into the ephemeral ping container at the same `/data` path, so paths resolve correctly in both containers.

---

## Ping modes

Controlled by `tp.pingMode` in config and the GUI Config tab.

### `docker-vpn` (default)
```
docker run --rm --network=$docker_net -v "$($Cfg.dir):/data" $PingImage trackerping -l -o /data/working_trackers.txt /data/active_raw.txt
```
The ping container joins the specified Docker network (`tp.dockerNet`). Traffic exits through whatever VPN container owns that network (e.g. Gluetun). Before pinging, the script performs an IP verification: it checks the host's external IP and the container's external IP and aborts if they match (traffic not routed through VPN).

### `socks5`
```
docker run --rm -e ALL_PROXY=$ProxyUrl -e all_proxy=$ProxyUrl -v "$($Cfg.dir):/data" $PingImage trackerping -l --no-udp -o ...
```
Passes the proxy via env vars. `--no-udp` is always passed — SOCKS5 proxies cannot tunnel UDP (UDP ASSOCIATE is not implemented by most proxy servers). All `udp://` trackers are skipped, not tested.

### `https-proxy`
```
docker run --rm -e HTTPS_PROXY=$ProxyUrl -e HTTP_PROXY=$ProxyUrl -e https_proxy=... -e http_proxy=... -v "$($Cfg.dir):/data" $PingImage trackerping -l --no-udp -o ...
```
Same UDP limitation applies. HTTP CONNECT proxies do not tunnel UDP.

### `direct`
```
docker run --rm -v "$($Cfg.dir):/data" $PingImage trackerping -l -o /data/working_trackers.txt /data/active_raw.txt
```
No network argument. Traffic goes out on the default Docker bridge network / host network. No VPN, no proxy. No IP check.

### `tp.pingImage`
Which image to use for the ephemeral ping container. Defaults to `ghcr.io/o51r15/trackarr:latest`. Override in config if you want to use a locally built image or a pinned version. On Windows source installs where the bridge runs as a PS script, Docker still needs to be available to run the ping container — the image is pulled from the registry or built locally.

---

## File data flow

```
tracker_urls.txt          → trackerping.ps1 reads raw URL list sources
tracker-sources.json      → trackerping.ps1 reads GitHub repos, scrapes, manual entries
                          → tracker-discovery.ps1 reads/writes
tracker-source-cache.json → trackerping.ps1 reads/writes GitHub SHA cache
combined_raw.txt          → trackerping.ps1 writes full deduplicated tracker pool
active_raw.txt            → trackerping.ps1 writes trackers minus sleeping/hibernating ones
                          → ping container reads this as input
working_trackers.txt      → ping container writes surviving trackers
                          → trackerping.ps1 reads this for latency measurement + qBT injection
tracker-sleep.json        → trackerping.ps1 reads/writes sleep/hibernate state
tracker-history.json      → bridge writes after each run (from [TRACKER_RESULT] stdout lines)
trackerping.log           → trackerping.ps1 appends every log line (persistent across runs)
```

---

## Bridge job system

The bridge runs `trackerping.ps1` and `tracker-discovery.ps1` via `Start-Process powershell.exe` with stdout/stderr redirected to temp log files. Each invocation gets a random 8-char job ID. The bridge polls the process, streams new stdout lines to the GUI via `/job/{id}`, and on completion calls `Update-TrackerHistory` (parses `[TRACKER_RESULT]` lines from the log) and `Invoke-CompletionNotification` (Pushover).

`[TRACKER_RESULT]` lines are emitted by `trackerping.ps1` to stdout but filtered out of the GUI log stream. The bridge captures them separately to build tracker history. Format:
```
[TRACKER_RESULT] url=udp://tracker.example.com:6969 status=UP latency=42
[TRACKER_RESULT] url=udp://tracker.dead.com:80 status=DOWN latency=0
```

---

## Config reference (`homelab-config.json`)

```json
{
  "tp": {
    "url":                    "http://192.168.1.x:8080",   // qBittorrent Web UI URL
    "user":                   "admin",                     // qBittorrent username
    "pass":                   "...",                       // DPAPI-encrypted password (Windows only)
    "pingMode":               "docker-vpn",                // docker-vpn | socks5 | https-proxy | direct
    "pingImage":              "ghcr.io/o51r15/trackarr:latest", // image used for ephemeral ping container
    "dockerNet":              "your_vpn_network",          // Docker network name (docker-vpn mode only)
    "proxyUrl":               "",                          // Proxy URL (socks5/https-proxy modes only)
    "dir":                    "/data",                     // Runtime data dir - mounted into ping container
    "script":                 "/app/trackerping.ps1",      // Path to trackerping.ps1
    "trackerHistoryRuns":     200,                         // How many runs of history to keep per tracker
    "trackerLatencyTimeoutMs": 3000                        // Latency measurement TCP timeout
  },
  "notifications": {
    "pushoverUser":  "...",         // DPAPI-encrypted Pushover user key
    "pushoverToken": "...",         // DPAPI-encrypted Pushover API token
    "notify": {
      "tp": false                   // Send Pushover notification on TrackerPing completion
    }
  }
}
```

**Note on DPAPI**: `tp.pass`, `pushoverUser`, and `pushoverToken` are encrypted with Windows DPAPI via `ConvertFrom-SecureString`. This is a Windows-only encryption mechanism tied to the user account. It will not work in a Linux Docker container — credential storage strategy for Docker deployment is an open issue.

---

## Known bugs

### BUG-01 — `active_raw.txt` written with BOM (FIXED in trackarr)
`Out-File -Encoding UTF8` in PowerShell 5.1 prepends a BOM. The ping container reads `active_raw.txt` as its input. A BOM prefix corrupts the first URL.
Fixed: `[System.IO.File]::WriteAllLines($ActiveFile, @($ActiveTrackers), [System.Text.Encoding]::UTF8)`
Note: PowerShell 7 on Linux does not add BOM — this bug is self-healing in Docker.

### BUG-02 — Pushover notification regex never matches (FIXED in trackarr)
The regex in the notification function matched a log pattern that `trackerping.ps1` never actually emits. Every notification read "Run complete" regardless of actual results.
Fixed: notification function now matches actual log patterns: `Collection complete: (\d+)` and `(\d+) trackers passed` / `Done\. (\d+) trackers`.

### BUG-03 — `combined_raw.txt` path inconsistency (FIXED in trackarr bridge)
`Update-TrackerHistory` used `$ScriptDir` to locate `combined_raw.txt` for stale key pruning, while the preview endpoint used `$cfg.tp.dir`. Now canonicalized to `$cfg.tp.dir` in both places.

---

## Open issues

### DPAPI in Docker
`ConvertTo-SecureString` / `ConvertFrom-SecureString` uses Windows DPAPI. This is unavailable on Linux. The bridge and trackerping.ps1 currently decrypt credentials at runtime using DPAPI. For Docker deployment this means:
- Credentials cannot be stored encrypted in `homelab-config.json`
- Workaround options: plain text in config (acceptable for self-hosted), Docker secrets, env vars
- This needs to be resolved before Docker deployment is fully functional with qBittorrent auth

### `tp.script` path in Docker
`trackerping.ps1` sets `$ConfigDir = $Cfg.dir` and `$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path`. When running inside the bridge container, `$MyInvocation.MyCommand.Path` is `/app/trackerping.ps1`, so `$ScriptDir` = `/app`. This is correct. The `tp.script` config field should be set to `/app/trackerping.ps1` for Docker deployments.

---

## CI/CD

One workflow: `.github/workflows/docker.yml`
- Triggers on push to master
- Builds the single image from root `Dockerfile`
- Pushes to `ghcr.io/o51r15/trackarr:latest` and `ghcr.io/o51r15/trackarr:<short-sha>`

Also: `.github/workflows/gitea-sync.yml`
- Mirrors the repo to `git.sickbot.org/o51r15/trackarr` on push/delete/create

---

## PS 5.1 compatibility (Windows source installs)

- `Out-File -Encoding UTF8` adds BOM — use `[System.IO.File]::WriteAllLines/WriteAllText` with explicit UTF-8 encoding
- `ConvertFrom-Json` collapses single-item arrays — wrap reads/writes with `@()`
- Em dashes in `.ps1` files cause parse errors — use hyphens
- `$matches` is reserved — use `$regexMatches`
- `.Trim()` on null crashes — guard for null
- Inline `if` as parameter values is invalid — pre-compute to a variable
- `Write-Output` inside a function contaminates the function's return value — use `Write-Trace` (file-only) inside utility functions
