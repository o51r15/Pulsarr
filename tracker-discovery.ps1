# =============================================================================
# tracker-discovery.ps1
# Discovers new tracker list sources and adds them as pending candidates.
# Rate-limited: GitHub API search only runs if minimumIntervalDays has elapsed.
# Well-known aggregator URLs are always checked.
# =============================================================================

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourcesFile = Join-Path $ScriptDir "tracker-data\tracker-sources.json"
$UrlFile     = Join-Path $ScriptDir "tracker_urls.txt"

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $ts   = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$ts] [$Level] $Message"
    Write-Output $line
}

function Exit-Done {
    Write-Log "SCRIPT_FINISHED_SUCCESSFULLY" "SYSTEM"
    exit 0
}

if (-not (Test-Path $SourcesFile)) {
    Write-Log "tracker-sources.json not found. Run TrackerPing first or add sources via the GUI." "WARN"
    Exit-Done
}

# =============================================================================
# Load state
# =============================================================================
$src = $null
try { $src = Get-Content $SourcesFile -Raw | ConvertFrom-Json } catch {
    Write-Log "Could not parse tracker-sources.json: $_" "ERROR"
    Exit-Done
}

if (-not $src.PSObject.Properties["discovery"]) {
    $src | Add-Member -NotePropertyName discovery -NotePropertyValue ([PSCustomObject]@{
        lastGithubRun      = $null
        minimumIntervalDays = 7
        candidates         = @()
        dismissed          = @()
    })
}
if (-not $src.discovery.candidates) { $src.discovery.candidates = @() }
if (-not $src.discovery.dismissed)  { $src.discovery.dismissed  = @() }

# =============================================================================
# Build set of already-known source URLs
# =============================================================================
$knownUrls = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)

if (Test-Path $UrlFile) {
    Get-Content $UrlFile | Where-Object { $_.Trim() -ne "" -and $_ -notmatch '^\s*#' } |
        ForEach-Object { [void]$knownUrls.Add($_.Trim()) }
}
@($src.githubRepos) | Where-Object { $_ -and $_.url } | ForEach-Object { [void]$knownUrls.Add($_.url) }
@($src.websiteScrape) | Where-Object { $_ -and $_.url } | ForEach-Object { [void]$knownUrls.Add($_.url) }

$dismissed = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
@($src.discovery.dismissed) | Where-Object { $_ } | ForEach-Object { [void]$dismissed.Add($_) }

$candidateUrls = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
@($src.discovery.candidates) | Where-Object { $_ -and $_.url } | ForEach-Object { [void]$candidateUrls.Add($_.url) }

$candidates = [System.Collections.Generic.List[object]]::new()
@($src.discovery.candidates) | Where-Object { $_ -and $_.url } | ForEach-Object { $candidates.Add($_) }

# =============================================================================
# Get GitHub token (optional)
# =============================================================================
$githubToken = ""
if ($src.PSObject.Properties["githubToken"] -and ![string]::IsNullOrWhiteSpace($src.githubToken)) {
    try { $githubToken = [System.Net.NetworkCredential]::new("", (ConvertTo-SecureString $src.githubToken)).Password } catch {}
}
$headers = @{ 'User-Agent' = 'Trackarr-Discovery/1.0' }
if ($githubToken) { $headers['Authorization'] = "token $githubToken" }

$newCount = 0

function Test-IsTrackerList($content) {
    $lines        = $content -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
    $trackerLines = @($lines | Where-Object { $_ -match '^(https?|udp|wss?)://' })
    return ($trackerLines.Count -ge 5 -and ($trackerLines.Count / [Math]::Max($lines.Count,1)) -gt 0.5)
}

function Add-Candidate($url, $rawUrl, $type, $label, $stars, $lastCommit) {
    if ($knownUrls.Contains($url) -or $dismissed.Contains($url) -or $candidateUrls.Contains($url)) { return $false }
    [void]$candidateUrls.Add($url)
    $candidates.Add([PSCustomObject]@{
        url            = $url
        rawUrl         = if ($rawUrl) { $rawUrl } else { $url }
        sourceType     = $type
        label          = $label
        stars          = $stars
        lastCommit     = $lastCommit
        discoveredDate = (Get-Date).ToString("o")
        previewCount   = $null
    })
    return $true
}

# =============================================================================
# Step 1: Check well-known aggregator sources (ALWAYS runs)
# =============================================================================
Write-Log "=== Step 1: Checking well-known aggregator sources ==="

$wellKnown = @(
    [PSCustomObject]@{ url = "https://newtrackon.com/api/all";          label = "NewTrackon API (all)";                 type = "rawList"  },
    [PSCustomObject]@{ url = "https://newtrackon.com/api/stable";       label = "NewTrackon API (stable)";              type = "rawList"  },
    [PSCustomObject]@{ url = "https://trackers.run/s/wp_up_hp_hs_v4_v6.txt"; label = "trackers.run";                  type = "rawList"  },
    [PSCustomObject]@{ url = "https://cf.trackerslist.com/all.txt";     label = "cf.trackerslist.com (all)";            type = "rawList"  },
    [PSCustomObject]@{ url = "https://cf.trackerslist.com/best.txt";    label = "cf.trackerslist.com (best)";           type = "rawList"  },
    [PSCustomObject]@{ url = "https://raw.githubusercontent.com/DeSireFire/animeTrackerList/master/AT_all.txt"; label = "DeSireFire/animeTrackerList"; type = "rawList" },
    [PSCustomObject]@{ url = "https://trackerslist.com";                label = "trackerslist.com (website)";           type = "websiteScrape" },
    [PSCustomObject]@{ url = "https://github.com/ngosang/trackerslist"; label = "ngosang/trackerslist";                 type = "githubRepo" },
    [PSCustomObject]@{ url = "https://github.com/XIU2/TrackersListCollection"; label = "XIU2/TrackersListCollection";  type = "githubRepo" }
)

foreach ($src2 in $wellKnown) {
    if ($knownUrls.Contains($src2.url) -or $dismissed.Contains($src2.url) -or $candidateUrls.Contains($src2.url)) {
        Write-Log "  Skipping (already known): $($src2.label)"
        continue
    }
    if ($src2.type -eq "githubRepo") {
        if (Add-Candidate $src2.url $src2.url $src2.type $src2.label $null $null) {
            $newCount++
            Write-Log "[OK] New GitHub repo candidate: $($src2.label)"
        }
        continue
    }
    if ($src2.type -eq "websiteScrape") {
        if (Add-Candidate $src2.url $src2.url $src2.type $src2.label $null $null) {
            $newCount++
            Write-Log "[OK] New website scrape candidate: $($src2.label)"
        }
        continue
    }
    try {
        $resp = Invoke-RestMethod -Uri $src2.url -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
        if (Test-IsTrackerList $resp) {
            if (Add-Candidate $src2.url $src2.url "rawList" $src2.label $null $null) {
                $newCount++
                Write-Log "[OK] New raw list candidate: $($src2.label)"
            }
        } else {
            Write-Log "  Not a tracker list (content mismatch): $($src2.url)"
        }
    } catch {
        Write-Log "  Could not reach: $($src2.url)" "WARN"
    }
}

# =============================================================================
# Step 2: GitHub API search (rate-limited)
# =============================================================================
$minimumDays  = if ($src.discovery.minimumIntervalDays) { [int]$src.discovery.minimumIntervalDays } else { 7 }
$lastGhRun    = if ($src.discovery.lastGithubRun -and $src.discovery.lastGithubRun -ne "") {
    try { [datetime]::Parse($src.discovery.lastGithubRun) } catch { [datetime]::MinValue }
} else { [datetime]::MinValue }
$daysSinceLast = ([datetime]::UtcNow - $lastGhRun.ToUniversalTime()).TotalDays

if ($daysSinceLast -ge $minimumDays) {
    Write-Log "=== Step 2: GitHub API search (last run: $($lastGhRun.ToString('yyyy-MM-dd'))) ==="
    $searchTerms = @("torrent+tracker+list", "bittorrent+announce+trackers+list")
    $reposSeen   = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $rateLimited = $false

    foreach ($term in $searchTerms) {
        if ($rateLimited) { break }
        try {
            $searchUrl  = "https://api.github.com/search/repositories?q=$term&sort=stars&order=desc&per_page=15"
            $searchResp = Invoke-RestMethod -Uri $searchUrl -Headers $headers -UseBasicParsing -TimeoutSec 15 -ErrorAction Stop
            Write-Log "  GitHub search '$term': $($searchResp.total_count) total results."

            foreach ($repo in $searchResp.items) {
                $repoPath = $repo.full_name
                $repoUrl  = "https://github.com/$repoPath"
                if ($reposSeen.Contains($repoPath)) { continue }
                [void]$reposSeen.Add($repoPath)

                if ($knownUrls.Contains($repoUrl) -or $dismissed.Contains($repoUrl) -or $candidateUrls.Contains($repoUrl)) {
                    Write-Log "  Already known: $repoPath"
                    continue
                }

                try {
                    $treeResp = Invoke-RestMethod -Uri "https://api.github.com/repos/$repoPath/git/trees/HEAD?recursive=1" `
                        -Headers $headers -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
                    $txtFiles = @($treeResp.tree | Where-Object { $_.type -eq 'blob' -and $_.path -match '\.txt$' } | Select-Object -First 3)

                    if ($txtFiles.Count -gt 0) {
                        $sampleUrl     = "https://raw.githubusercontent.com/$repoPath/HEAD/$($txtFiles[0].path)"
                        $sampleContent = Invoke-RestMethod -Uri $sampleUrl -Headers $headers -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop

                        if (Test-IsTrackerList $sampleContent) {
                            if (Add-Candidate $repoUrl $repoUrl "githubRepo" $repoPath $repo.stargazers_count $repo.pushed_at) {
                                $newCount++
                                Write-Log "[OK] GitHub candidate: $repoPath ($($repo.stargazers_count) stars)"
                            }
                        } else {
                            Write-Log "  Not a tracker list: $repoPath"
                        }
                    } else {
                        Write-Log "  No .txt files found: $repoPath"
                    }
                    Start-Sleep -Milliseconds 400
                } catch {
                    $sc = $null; try { $sc = $_.Exception.Response.StatusCode.Value__ } catch {}
                    if ($sc -in @(403, 429)) {
                        Write-Log "GitHub API rate limit hit. Add a Personal Access Token in Sources for 5000 req/hr." "WARN"
                        $rateLimited = $true; break
                    }
                    Write-Log "  Could not check $repoPath - $_" "WARN"
                }
            }
        } catch {
            $sc = $null; try { $sc = $_.Exception.Response.StatusCode.Value__ } catch {}
            if ($sc -in @(403, 429)) {
                Write-Log "GitHub API rate limit hit on search. Add a Personal Access Token in Sources." "WARN"
                $rateLimited = $true
            } else {
                Write-Log "GitHub search failed: $_" "WARN"
            }
        }
        if (-not $rateLimited) { Start-Sleep -Milliseconds 500 }
    }

    $src.discovery.lastGithubRun = (Get-Date).ToString("o")
    Write-Log "GitHub search complete. Repos checked: $($reposSeen.Count). Rate limited: $rateLimited."
} else {
    $daysLeft = [math]::Ceiling($minimumDays - $daysSinceLast)
    Write-Log "=== Step 2: GitHub search skipped (next eligible in $daysLeft day(s)) ==="
}

# =============================================================================
# Save updated sources file
# =============================================================================
$src.discovery.candidates = @($candidates)
$src.discovery.dismissed  = @($dismissed)

$json = $src | ConvertTo-Json -Depth 10 -Compress
[System.IO.File]::WriteAllText($SourcesFile, $json, [System.Text.Encoding]::UTF8)

Write-Log "=== Discovery complete: $newCount new candidate(s). $($candidates.Count) total pending. ==="
Exit-Done
