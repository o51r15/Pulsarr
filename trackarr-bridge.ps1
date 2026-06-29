# =============================================================================
# trackarr-bridge.ps1 - Trackarr Bridge v1.0
# Standalone HTTP bridge for TrackerPing.
# Default port: 7374. Override in bridge-config.json.
# =============================================================================

$ScriptDir          = Split-Path -Parent $MyInvocation.MyCommand.Path
$BridgeCfgFile      = Join-Path $ScriptDir "bridge-config.json"
$ConfigFile         = Join-Path $ScriptDir "homelab-config.json"
$TrackerDataDir     = Join-Path $ScriptDir "tracker-data"
$TrackerHistoryFile = Join-Path $TrackerDataDir "tracker-history.json"
$TrackerSourcesFile = Join-Path $TrackerDataDir "tracker-sources.json"
$TrackerCacheFile   = Join-Path $TrackerDataDir "tracker-source-cache.json"
$TrackerSleepFile   = Join-Path $TrackerDataDir "tracker-sleep.json"

if (Test-Path $BridgeCfgFile) {
    $BridgeCfg = Get-Content $BridgeCfgFile -Raw | ConvertFrom-Json
    $Port = if ($BridgeCfg.port) { $BridgeCfg.port } else { 7374 }
} else {
    $Port = 7374
    @{ port = 7374 } | ConvertTo-Json | Set-Content $BridgeCfgFile -Encoding UTF8
}

$listener = [System.Net.HttpListener]::new()
$listener.Prefixes.Add("http://+:$Port/")
$listener.Start()
Write-Host "[Trackarr Bridge v1.0] Listening on port $Port" -ForegroundColor Cyan

if (-not (Get-NetFirewallRule -DisplayName 'Trackarr Bridge' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName 'Trackarr Bridge' -Direction Inbound `
        -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
    Write-Host "[Trackarr Bridge] Firewall rule added for port $Port" -ForegroundColor Green
}

$jobs = @{}
$asyncContext = $null

# =============================================================================
# HELPERS
# =============================================================================

function Set-Cors($ctx) {
    $origin = $ctx.Request.Headers["Origin"]
    if ([string]::IsNullOrWhiteSpace($origin) -or $origin -eq "null") {
        $ctx.Response.Headers.Add("Access-Control-Allow-Origin", "*")
    } else {
        $ctx.Response.Headers.Add("Access-Control-Allow-Origin", $origin)
    }
}

function Send-Json($ctx, $obj, $status = 200) {
    $body  = $obj | ConvertTo-Json -Depth 8 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $ctx.Response.StatusCode      = $status
    $ctx.Response.ContentType     = "application/json"
    $ctx.Response.Headers.Add("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
    $ctx.Response.Headers.Add("Pragma", "no-cache")
    $ctx.Response.Headers.Add("Expires", "0")
    Set-Cors $ctx
    $ctx.Response.ContentLength64 = $bytes.Length
    $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
    $ctx.Response.OutputStream.Close()
}

function Read-Body($ctx) {
    $reader = [System.IO.StreamReader]::new($ctx.Request.InputStream)
    return $reader.ReadToEnd() | ConvertFrom-Json
}

function Get-Config {
    if (Test-Path $ConfigFile) { return Get-Content $ConfigFile -Raw | ConvertFrom-Json }
    return $null
}

function Save-Config($cfg) {
    $json    = $cfg | ConvertTo-Json -Depth 10
    $retries = 3
    while ($retries -gt 0) {
        try {
            [System.IO.File]::WriteAllText($ConfigFile, $json, [System.Text.Encoding]::UTF8)
            return
        } catch [System.IO.IOException] {
            $retries--
            if ($retries -eq 0) { Write-Host "[ERROR] Could not save config: $($_.Exception.Message)" -ForegroundColor Red; throw }
            Start-Sleep -Milliseconds 200
        }
    }
}

function Send-Pushover($title, $message, $priority = 0) {
    $cfg = Get-Config
    if (-not $cfg -or -not $cfg.PSObject.Properties["notifications"]) { return }
    $n = $cfg.notifications
    if (-not $n.pushoverUser -or -not $n.pushoverToken -or $n.pushoverUser -eq "" -or $n.pushoverToken -eq "") { return }
    try {
        $userKey = [System.Net.NetworkCredential]::new("", (ConvertTo-SecureString $n.pushoverUser)).Password
        $token   = [System.Net.NetworkCredential]::new("", (ConvertTo-SecureString $n.pushoverToken)).Password
        Invoke-RestMethod -Uri "https://api.pushover.net/1/messages.json" -Method Post -UseBasicParsing `
            -Body @{ token=$token; user=$userKey; title=$title; message=$message; priority=$priority } | Out-Null
        Write-Host "[Pushover] Sent: $title - $message" -ForegroundColor DarkCyan
    } catch {
        Write-Host "[Pushover] Failed: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

function Invoke-CompletionNotification($job) {
    try {
        $cfg = Get-Config
        if (-not $cfg -or -not $cfg.PSObject.Properties["notifications"]) { return }
        $notify = $cfg.notifications.notify
        if (-not $notify.tp) { return }
        $log      = if (Test-Path $job.LogPath) { Get-Content $job.LogPath -Raw -ErrorAction SilentlyContinue } else { '' }
        # FIX BUG-02: Use actual log patterns from trackerping.ps1
        $fetched   = if ($log -match 'Collection complete: (\d+)') { $Matches[1] } else { '?' }
        $surviving = if ($log -match '(\d+) trackers passed') { $Matches[1] } `
                     elseif ($log -match 'Done\. (\d+) trackers') { $Matches[1] } else { '?' }
        $count = "$surviving/$fetched active"
        $msg   = if ($job.Success) { "TrackerPing: $count. qBittorrent updated." } else { "TrackerPing FAILED." }
        Send-Pushover "TrackerPing" $msg
    } catch {
        Write-Host "[Pushover] Notification error: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

function Update-TrackerHistory($logPath) {
    try {
        if (-not (Test-Path $TrackerDataDir)) { New-Item -ItemType Directory -Path $TrackerDataDir | Out-Null }
        $logLines = if (Test-Path $logPath) { Get-Content $logPath -ErrorAction SilentlyContinue } else { @() }
        $results  = @()
        foreach ($line in $logLines) {
            if ($line -match '^\[TRACKER_RESULT\] url=(.+?) status=(UP|DOWN) latency=(\d+)') {
                $results += [PSCustomObject]@{
                    url     = $Matches[1].Trim()
                    status  = $Matches[2]
                    latency = if ($Matches[2] -eq 'UP') { [int]$Matches[3] } else { $null }
                }
            }
        }
        if ($results.Count -eq 0) { return }
        $cfg     = Get-Config
        $maxRuns = if ($cfg -and $cfg.tp -and $cfg.tp.trackerHistoryRuns) { [int]$cfg.tp.trackerHistoryRuns } else { 200 }
        $history = $null
        if (Test-Path $TrackerHistoryFile) { try { $history = Get-Content $TrackerHistoryFile -Raw | ConvertFrom-Json } catch {} }
        if (-not $history) {
            $history = [PSCustomObject]@{ meta = [PSCustomObject]@{ lastRun = $null; totalRuns = 0 }; trackers = [PSCustomObject]@{} }
        }
        $ts = (Get-Date).ToString("o")
        $history.meta.lastRun   = $ts
        $history.meta.totalRuns = [int]$history.meta.totalRuns + 1
        $trackerHash = @{}
        if ($history.trackers) { $history.trackers.PSObject.Properties | ForEach-Object { $trackerHash[$_.Name] = $_.Value } }
        foreach ($r in $results) {
            $entry = [PSCustomObject]@{ ts = $ts; status = $r.status; latencyMs = $r.latency }
            if ($trackerHash.ContainsKey($r.url)) {
                $existing = @($trackerHash[$r.url].runs)
                $trackerHash[$r.url].runs = @($entry) + @($existing | Select-Object -First ($maxRuns - 1))
            } else {
                $trackerHash[$r.url] = [PSCustomObject]@{ runs = @($entry) }
            }
        }
        $tObj = [PSCustomObject]@{}
        foreach ($k in $trackerHash.Keys) { $tObj | Add-Member -NotePropertyName $k -NotePropertyValue $trackerHash[$k] -Force }
        $history.trackers = $tObj
        # Prune stale keys using tp.dir as canonical path
        $cfg2 = Get-Config
        $combinedRaw = if ($cfg2 -and $cfg2.tp -and $cfg2.tp.dir) { Join-Path $cfg2.tp.dir "combined_raw.txt" } else { Join-Path $ScriptDir "combined_raw.txt" }
        if (Test-Path $combinedRaw) {
            try {
                $currentPool = [System.Collections.Generic.HashSet[string]]::new(
                    [string[]]@(Get-Content $combinedRaw -ErrorAction Stop | Where-Object { $_.Trim() -ne "" } | ForEach-Object { $_.Trim() }),
                    [System.StringComparer]::OrdinalIgnoreCase
                )
                $staleKeys = @($trackerHash.Keys | Where-Object { -not $currentPool.Contains($_) })
                if ($staleKeys.Count -gt 0) {
                    foreach ($sk in $staleKeys) { $trackerHash.Remove($sk) }
                    Write-Host "[TrackerHistory] Pruned $($staleKeys.Count) stale key(s) not in current pool." -ForegroundColor DarkCyan
                    $tObj = [PSCustomObject]@{}
                    foreach ($k in $trackerHash.Keys) { $tObj | Add-Member -NotePropertyName $k -NotePropertyValue $trackerHash[$k] -Force }
                    $history.trackers = $tObj
                }
            } catch { Write-Host "[TrackerHistory] Prune skipped: $($_.Exception.Message)" -ForegroundColor Yellow }
        }
        $json = $history | ConvertTo-Json -Depth 10 -Compress
        [System.IO.File]::WriteAllText($TrackerHistoryFile, $json, [System.Text.Encoding]::UTF8)
        Write-Host "[TrackerHistory] $($results.Count) trackers recorded. Total runs: $($history.meta.totalRuns)" -ForegroundColor DarkCyan
    } catch {
        Write-Host "[TrackerHistory] ERROR: $($_.Exception.Message)" -ForegroundColor Red
    }
}

function Start-JobProcess($scriptPath, $toolName = "tp", $extraArgs = "") {
    $runId   = [System.Guid]::NewGuid().ToString("N").Substring(0, 8)
    $logPath = Join-Path $env:TEMP "trackarr_$runId.log"
    $errPath = "$logPath.err"
    $psArgs  = "-NonInteractive -ExecutionPolicy Bypass -File `"$scriptPath`""
    if ($extraArgs) { $psArgs += " $extraArgs" }
    $proc = Start-Process powershell.exe -ArgumentList $psArgs `
        -RedirectStandardOutput $logPath -RedirectStandardError $errPath -PassThru -WindowStyle Hidden
    $jobs[$runId] = @{
        Proc    = $proc; LogPath = $logPath; ErrPath = $errPath
        Pos     = 0; ErrPos = 0; Done = $false; Success = $false
        Tool    = $toolName; EndTime = $null; TempScript = $null
    }
    return $runId
}

function Check-BackgroundJobs {
    foreach ($runId in @($jobs.Keys)) {
        $job = $jobs[$runId]
        if ($job.Done) { continue }
        if (-not $job.Proc.HasExited) { continue }
        $job.Proc.WaitForExit(); Start-Sleep -Milliseconds 200
        try {
            $job.Done    = $true
            $job.EndTime = Get-Date
            $logContent  = if (Test-Path $job.LogPath) { Get-Content $job.LogPath -Raw -ErrorAction SilentlyContinue } else { '' }
            $job.Success = $logContent -match "SCRIPT_FINISHED_SUCCESSFULLY"
            if ($job.Tool -eq "tp") { Update-TrackerHistory $job.LogPath }
            Invoke-CompletionNotification $job
        } catch {
            Write-Host "[Bridge] ERROR completing job $runId : $($_.Exception.Message)" -ForegroundColor Red
        }
        Write-Host "[Bridge] Job complete: $runId ($($job.Tool)) Success=$($job.Success)" -ForegroundColor DarkGray
    }
}

# =============================================================================
# MAIN ASYNC LOOP
# =============================================================================
while ($listener.IsListening) {
    $cutoff = (Get-Date).AddMinutes(-30)
    ($jobs.Keys | Where-Object { $jobs[$_].Done -and $jobs[$_].EndTime -lt $cutoff }) | ForEach-Object { $jobs.Remove($_) }
    Check-BackgroundJobs
    if ($null -eq $asyncContext) { $asyncContext = $listener.BeginGetContext($null, $null) }
    $gotRequest = $asyncContext.AsyncWaitHandle.WaitOne(500)
    if (-not $gotRequest) { continue }
    try { $ctx = $listener.EndGetContext($asyncContext); $asyncContext = $null }
    catch { $asyncContext = $null; continue }

    $path   = $ctx.Request.Url.AbsolutePath
    $method = $ctx.Request.HttpMethod

    if ($method -eq "OPTIONS") {
        Set-Cors $ctx
        $ctx.Response.Headers.Add("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        $ctx.Response.Headers.Add("Access-Control-Allow-Headers", "Content-Type")
        $ctx.Response.StatusCode = 204
        $ctx.Response.OutputStream.Close()
        continue
    }

    switch -Wildcard ($path) {

        "/ping" { Send-Json $ctx @{ ok = $true; version = "1.0" } }

        "/get-config" {
            $cfg = Get-Config
            if ($cfg) {
                if ($cfg.tp) { $cfg.tp.pass = "********" }
                if ($cfg.notifications) {
                    if ($cfg.notifications.pushoverUser)  { $cfg.notifications.pushoverUser  = "********" }
                    if ($cfg.notifications.pushoverToken) { $cfg.notifications.pushoverToken = "********" }
                }
                Send-Json $ctx @{ ok = $true; config = $cfg }
            } else { Send-Json $ctx @{ ok = $false; error = "No config found. Copy homelab-config.example.json to homelab-config.json and fill in your settings." } }
        }

        "/config" {
            $body = Read-Body $ctx
            $cfg  = if (Test-Path $ConfigFile) { Get-Content $ConfigFile -Raw | ConvertFrom-Json } else { [PSCustomObject]@{} }
            if ($body.tp) {
                if ($body.tp.pass -eq "********" -and $cfg.PSObject.Properties["tp"]) { $body.tp.pass = $cfg.tp.pass }
                elseif (![string]::IsNullOrWhiteSpace($body.tp.pass)) {
                    $body.tp.pass = ConvertFrom-SecureString (ConvertTo-SecureString $body.tp.pass -AsPlainText -Force)
                }
                if (-not $cfg.PSObject.Properties["tp"]) { $cfg | Add-Member -NotePropertyName tp -NotePropertyValue $body.tp }
                else { $cfg.tp = $body.tp }
            }
            if ($body.notifications) {
                foreach ($k in @("pushoverUser","pushoverToken")) {
                    if ($body.notifications.$k -eq "********" -and $cfg.PSObject.Properties["notifications"]) {
                        $body.notifications.$k = $cfg.notifications.$k
                    } elseif (![string]::IsNullOrWhiteSpace($body.notifications.$k)) {
                        $body.notifications.$k = ConvertFrom-SecureString (ConvertTo-SecureString $body.notifications.$k -AsPlainText -Force)
                    }
                }
                if (-not $cfg.PSObject.Properties["notifications"]) {
                    $cfg | Add-Member -NotePropertyName notifications -NotePropertyValue $body.notifications
                } else { $cfg.notifications = $body.notifications }
            }
            Save-Config $cfg
            Send-Json $ctx @{ ok = $true }
        }

        "/logs/tp" {
            try {
                $cfg     = Get-Config
                $logPath = if ($cfg -and $cfg.tp -and $cfg.tp.dir) { Join-Path $cfg.tp.dir "trackerping.log" } else { "" }
                if ($logPath -and (Test-Path $logPath)) {
                    $tmp = Join-Path $env:TEMP "trackarr_read_tp.log"
                    Copy-Item $logPath $tmp -Force -ErrorAction SilentlyContinue
                    $raw = Get-Content $tmp -Tail 500 -ErrorAction SilentlyContinue
                    [string[]]$lines = if ($raw) { @($raw) | ForEach-Object { $_ -replace "[\x00-\x1F]","" } } else { @() }
                    Send-Json $ctx @{ ok = $true; lines = $lines }
                } else { Send-Json $ctx @{ ok = $true; lines = @() } }
            } catch { Send-Json $ctx @{ ok = $false; error = $_.Exception.Message } }
        }

        "/tracker-urls" {
            $UrlFile = Join-Path $ScriptDir "tracker_urls.txt"
            if ($method -eq "GET") {
                $urls = if (Test-Path $UrlFile) {
                    @(Get-Content $UrlFile | Where-Object { $_.Trim() -ne "" } | ForEach-Object { "$_" })
                } else { @("https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_all.txt") }
                Send-Json $ctx @{ ok = $true; urls = $urls }
            } else {
                $body = Read-Body $ctx
                $body.urls | Out-File $UrlFile -Encoding UTF8
                Send-Json $ctx @{ ok = $true }
            }
        }

        "/tracker-sources" {
            if ($method -eq "GET") {
                $emptyState = [PSCustomObject]@{
                    githubToken   = ""
                    githubRepos   = @()
                    websiteScrape = @()
                    manual        = @()
                    discovery     = [PSCustomObject]@{ lastGithubRun = $null; minimumIntervalDays = 7; candidates = @(); dismissed = @() }
                }
                if (Test-Path $TrackerSourcesFile) {
                    try {
                        $src = Get-Content $TrackerSourcesFile -Raw | ConvertFrom-Json
                        if ($src.PSObject.Properties["githubToken"] -and $src.githubToken -ne "") { $src.githubToken = "********" }
                        Send-Json $ctx @{ ok = $true; sources = $src }
                    } catch { Send-Json $ctx @{ ok = $true; sources = $emptyState } }
                } else { Send-Json $ctx @{ ok = $true; sources = $emptyState } }
            } else {
                $body = Read-Body $ctx
                if (-not (Test-Path $TrackerDataDir)) { New-Item -ItemType Directory -Path $TrackerDataDir | Out-Null }
                if ($body.PSObject.Properties["githubToken"]) {
                    if ($body.githubToken -eq "********") {
                        if (Test-Path $TrackerSourcesFile) {
                            try { $ex = Get-Content $TrackerSourcesFile -Raw | ConvertFrom-Json; $body.githubToken = $ex.githubToken } catch {}
                        }
                    } elseif (![string]::IsNullOrWhiteSpace($body.githubToken)) {
                        $body.githubToken = ConvertFrom-SecureString (ConvertTo-SecureString $body.githubToken -AsPlainText -Force)
                    }
                }
                $json = $body | ConvertTo-Json -Depth 10 -Compress
                [System.IO.File]::WriteAllText($TrackerSourcesFile, $json, [System.Text.Encoding]::UTF8)
                Send-Json $ctx @{ ok = $true }
            }
        }

        "/tracker-sources/*" {
            $action = $path -replace "/tracker-sources/",""
            $body   = if ($method -eq "POST") { Read-Body $ctx } else { $null }
            switch ($action) {
                "discover" {
                    $discoverScript = Join-Path $ScriptDir "tracker-discovery.ps1"
                    if (-not (Test-Path $discoverScript)) { Send-Json $ctx @{ ok = $false; error = "tracker-discovery.ps1 not found." } 404 }
                    else { $runId = Start-JobProcess $discoverScript "discover"; Send-Json $ctx @{ ok = $true; jobId = $runId } }
                }
                "preview" {
                    $previewUrl  = $body.url
                    $previewType = $body.sourceType
                    $existing = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
                    $cfg2 = Get-Config
                    if ($cfg2 -and $cfg2.tp -and $cfg2.tp.dir) {
                        $combinedRaw = Join-Path $cfg2.tp.dir "combined_raw.txt"
                        if (Test-Path $combinedRaw) {
                            Get-Content $combinedRaw -ErrorAction SilentlyContinue | Where-Object { $_.Trim() -ne "" } | ForEach-Object { [void]$existing.Add($_.Trim()) }
                        }
                    }
                    $previewTrackers = @()
                    try {
                        if ($previewType -eq "websiteScrape") {
                            $resp2    = Invoke-WebRequest -Uri $previewUrl -UseBasicParsing -TimeoutSec 15 -ErrorAction Stop
                            $pcontent = if ($resp2.Content -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($resp2.Content) } else { "$($resp2.Content)" }
                            $ppattern = '(?:udp|https?|wss?)://[a-zA-Z0-9._\-\[\]]+:\d+(?:/[^\s"' + "'" + '<>]*)?'
                            $pmatch   = [regex]::Matches($pcontent, $ppattern)
                            $previewTrackers = @($pmatch | ForEach-Object { $_.Value.TrimEnd('/') } | Sort-Object -Unique)
                        } elseif ($previewType -eq "githubRepo") {
                            Send-Json $ctx @{ ok = $true; total = 0; newCount = 0; existingCount = 0; sample = @(); note = "GitHub repo sources are scanned fully during TrackerPing. Use Add Source to include this repo." }
                            continue
                        } else {
                            $resp2 = Invoke-RestMethod -Uri $previewUrl -UseBasicParsing -TimeoutSec 15 -ErrorAction Stop
                            $previewTrackers = @($resp2 -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" -and $_ -match '^(https?|udp|wss?)://' })
                        }
                    } catch { Send-Json $ctx @{ ok = $false; error = "Failed to fetch URL: $($_.Exception.Message)" }; continue }
                    $newTrackers = @($previewTrackers | Where-Object { -not $existing.Contains($_) })
                    Send-Json $ctx @{ ok = $true; total = $previewTrackers.Count; newCount = $newTrackers.Count; existingCount = ($previewTrackers.Count - $newTrackers.Count); sample = @($newTrackers | Select-Object -First 20) }
                }
                "approve" {
                    $candidate = $body.candidate
                    if (Test-Path $TrackerSourcesFile) {
                        $sf    = Get-Content $TrackerSourcesFile -Raw | ConvertFrom-Json
                        $newId = [System.Guid]::NewGuid().ToString("N").Substring(0,8)
                        if ($candidate.sourceType -eq "githubRepo") {
                            if (-not $sf.PSObject.Properties["githubRepos"]) { $sf | Add-Member -NotePropertyName githubRepos -NotePropertyValue @() }
                            $sf.githubRepos = @($sf.githubRepos) + [PSCustomObject]@{ id=$newId; url=$candidate.url; label=$candidate.label; addedDate=(Get-Date).ToString("o"); lastCount=0 }
                        } elseif ($candidate.sourceType -eq "websiteScrape") {
                            if (-not $sf.PSObject.Properties["websiteScrape"]) { $sf | Add-Member -NotePropertyName websiteScrape -NotePropertyValue @() }
                            $sf.websiteScrape = @($sf.websiteScrape) + [PSCustomObject]@{ id=$newId; url=$candidate.url; label=$candidate.label; addedDate=(Get-Date).ToString("o"); lastCount=0 }
                        } else {
                            $urlFile = Join-Path $ScriptDir "tracker_urls.txt"
                            $rawUrl  = if ($candidate.rawUrl -and $candidate.rawUrl -ne "") { $candidate.rawUrl } else { $candidate.url }
                            Add-Content -Path $urlFile -Value $rawUrl -Encoding UTF8
                        }
                        $sf.discovery.candidates = @($sf.discovery.candidates | Where-Object { $_.url -ne $candidate.url })
                        [System.IO.File]::WriteAllText($TrackerSourcesFile, ($sf | ConvertTo-Json -Depth 10 -Compress), [System.Text.Encoding]::UTF8)
                        Send-Json $ctx @{ ok = $true }
                    } else { Send-Json $ctx @{ ok = $false; error = "tracker-sources.json not found." } }
                }
                "dismiss" {
                    $dismissUrl = $body.url
                    if (Test-Path $TrackerSourcesFile) {
                        $sf   = Get-Content $TrackerSourcesFile -Raw | ConvertFrom-Json
                        $diss = @(if ($sf.discovery.dismissed) { $sf.discovery.dismissed } else { @() })
                        if ($dismissUrl -notin $diss) { $diss = $diss + $dismissUrl }
                        $sf.discovery.dismissed  = $diss
                        $sf.discovery.candidates = @($sf.discovery.candidates | Where-Object { $_.url -ne $dismissUrl })
                        [System.IO.File]::WriteAllText($TrackerSourcesFile, ($sf | ConvertTo-Json -Depth 10 -Compress), [System.Text.Encoding]::UTF8)
                        Send-Json $ctx @{ ok = $true }
                    } else { Send-Json $ctx @{ ok = $false; error = "tracker-sources.json not found." } }
                }
                "clear-dismissed" {
                    if (Test-Path $TrackerSourcesFile) {
                        $sf = Get-Content $TrackerSourcesFile -Raw | ConvertFrom-Json
                        $sf.discovery.dismissed = @()
                        [System.IO.File]::WriteAllText($TrackerSourcesFile, ($sf | ConvertTo-Json -Depth 10 -Compress), [System.Text.Encoding]::UTF8)
                        Send-Json $ctx @{ ok = $true }
                    } else { Send-Json $ctx @{ ok = $false; error = "tracker-sources.json not found." } }
                }
                default { Send-Json $ctx @{ error = "Unknown action: $action" } 404 }
            }
        }

        "/run" {
            $body    = Read-Body $ctx
            $cfg     = Get-Config
            $toolCfg = if ($cfg) { $cfg.tp } else { $null }
            $mainScript = if ($toolCfg -and $toolCfg.script) { $toolCfg.script } else { "" }
            if (-not $mainScript -or -not (Test-Path $mainScript)) {
                Send-Json $ctx @{ ok = $false; error = "Script not found. Check Config > Script Path." } 400
                continue
            }
            $runId = Start-JobProcess $mainScript "tp"
            Send-Json $ctx @{ ok = $true; jobId = $runId }
        }

        "/job/*" {
            $runId = $path -replace "/job/",""
            $job   = $jobs[$runId]
            if (-not $job) { Send-Json $ctx @{ error = "unknown job" } 404; continue }
            if (-not $job.Done -and $job.Proc.HasExited) {
                $job.Proc.WaitForExit(); Start-Sleep -Milliseconds 200
                $job.Done    = $true
                $job.EndTime = Get-Date
                $logContent  = if (Test-Path $job.LogPath) { Get-Content $job.LogPath -Raw -ErrorAction SilentlyContinue } else { '' }
                $job.Success = $logContent -match "SCRIPT_FINISHED_SUCCESSFULLY"
                if ($job.Tool -eq "tp") { Update-TrackerHistory $job.LogPath }
                Invoke-CompletionNotification $job
            }
            $newLines = @()
            if (Test-Path $job.LogPath) {
                $raw = Get-Content $job.LogPath -ErrorAction SilentlyContinue
                if ($raw) {
                    $slice = @($raw) | Select-Object -Skip $job.Pos
                    $job.Pos += $slice.Count
                    foreach ($line in $slice) {
                        if ($line -match '^\[TRACKER_RESULT\]') { continue }
                        if ([string]::IsNullOrWhiteSpace($line)) { continue }
                        $level = if     ($line -match "\[ERROR\]|ERROR:")  { "error" }
                                 elseif ($line -match "\[WARN\]|WARNING")  { "warn"  }
                                 elseif ($line -match "\[OK\]|Success")    { "ok"    }
                                 elseif ($line -match "\[STEP\]|---")      { "step"  }
                                 else                                       { "info"  }
                        $newLines += @{ level = $level; msg = ($line -replace "[\x00-\x1F]","") }
                    }
                }
            }
            if ($job.Done -and (Test-Path $job.ErrPath)) {
                $errRaw = Get-Content $job.ErrPath -ErrorAction SilentlyContinue
                if ($errRaw) {
                    $errSlice = @($errRaw) | Select-Object -Skip $job.ErrPos
                    $job.ErrPos += $errSlice.Count
                    foreach ($line in $errSlice) {
                        if ([string]::IsNullOrWhiteSpace($line)) { continue }
                        $newLines += @{ level = "error"; msg = "[STDERR] " + ($line -replace "[\x00-\x1F]","") }
                    }
                }
            }
            Send-Json $ctx @{ done = $job.Done; success = $job.Success; newLines = @($newLines) }
        }

        "/abort/tp" {
            foreach ($job in $jobs.Values) {
                if ($job.Tool -eq "tp" -and -not $job.Done) {
                    Stop-Process -Id $job.Proc.Id -Force -ErrorAction SilentlyContinue
                    $job.Done = $true; $job.EndTime = Get-Date
                }
            }
            Send-Json $ctx @{ ok = $true }
        }

        "/tracker-sleep" {
            if (Test-Path $TrackerSleepFile) {
                try {
                    $raw   = Get-Content $TrackerSleepFile -Raw -ErrorAction SilentlyContinue
                    $bytes = [System.Text.Encoding]::UTF8.GetBytes($raw)
                    $ctx.Response.StatusCode      = 200
                    $ctx.Response.ContentType     = "application/json; charset=utf-8"
                    $ctx.Response.Headers.Add("Cache-Control","no-store")
                    Set-Cors $ctx
                    $ctx.Response.ContentLength64 = $bytes.LongLength
                    $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
                    $ctx.Response.Close()
                } catch { Send-Json $ctx @{ ok = $false; error = $_.Exception.Message } }
            } else { Send-Json $ctx @{ } }
        }

        "/tracker-sleep/wake-all" {
            try {
                [System.IO.File]::WriteAllText($TrackerSleepFile, "{}", [System.Text.Encoding]::UTF8)
                Write-Host "[TrackerSleep] Wake-all: sleep file cleared." -ForegroundColor DarkCyan
            } catch { Send-Json $ctx @{ ok = $false; error = $_.Exception.Message }; continue }
            Send-Json $ctx @{ ok = $true }
        }

        "/tracker-sleep/wake" {
            $body    = Read-Body $ctx
            $wakeUrl = $body.url
            if (Test-Path $TrackerSleepFile) {
                try {
                    $sf     = Get-Content $TrackerSleepFile -Raw | ConvertFrom-Json
                    $newObj = [PSCustomObject]@{}
                    $sf.PSObject.Properties | Where-Object { $_.Name -ne $wakeUrl } |
                        ForEach-Object { $newObj | Add-Member -NotePropertyName $_.Name -NotePropertyValue $_.Value }
                    [System.IO.File]::WriteAllText($TrackerSleepFile, ($newObj | ConvertTo-Json -Depth 5 -Compress), [System.Text.Encoding]::UTF8)
                } catch { Send-Json $ctx @{ ok = $false; error = $_.Exception.Message }; continue }
            }
            Send-Json $ctx @{ ok = $true }
        }

        "/tracker-history" {
            if (Test-Path $TrackerHistoryFile) {
                $raw   = Get-Content $TrackerHistoryFile -Raw -ErrorAction SilentlyContinue
                $bytes = [System.Text.Encoding]::UTF8.GetBytes($raw)
                $ctx.Response.StatusCode      = 200
                $ctx.Response.ContentType     = "application/json; charset=utf-8"
                $ctx.Response.Headers.Add("Cache-Control","no-store")
                Set-Cors $ctx
                $ctx.Response.ContentLength64 = $bytes.LongLength
                $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
                $ctx.Response.Close()
            } else {
                Send-Json $ctx @{ ok = $false; error = "No tracker history yet. Run TrackerPing first." }
            }
        }

        "/" {
            $guiPath = Join-Path $ScriptDir "trackarr-gui.html"
            if (Test-Path $guiPath) {
                $bytes = [System.IO.File]::ReadAllBytes($guiPath)
                $ctx.Response.StatusCode      = 200
                $ctx.Response.ContentType     = "text/html; charset=utf-8"
                $ctx.Response.Headers.Add("Cache-Control", "no-store")
                $ctx.Response.ContentLength64 = $bytes.LongLength
                $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
                $ctx.Response.Close()
            } else { Send-Json $ctx @{ error = "trackarr-gui.html not found" } 404 }
        }

        "/favicon.ico" {
            $ctx.Response.StatusCode = 204
            $ctx.Response.OutputStream.Close()
        }

        default { Send-Json $ctx @{ error = "not found" } 404 }
    }
}
