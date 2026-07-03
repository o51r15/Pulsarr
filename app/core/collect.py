"""
collect.py — Source collection pipeline

Gathers tracker URLs from all configured sources:
  - Raw .txt list URLs (tracker_urls.txt)
  - GitHub repos (crawled for .txt files, cached by commit SHA)
  - Website scrapes (regex extraction)
  - Manual entries

All sources are deduplicated case-insensitively into a single set.
IPv6-only trackers and trailing junk characters are filtered/cleaned.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

VALID_SCHEMES = re.compile(r"^(https?|udp|wss?)://", re.IGNORECASE)
IPV6_PATTERN = re.compile(r"^(udp|https?|wss?)://\[", re.IGNORECASE)
TRAILING_JUNK = re.compile(r"[\"'\\]+$")
SCRAPE_PATTERN = re.compile(
    r"""(?:udp|https?|wss?)://[a-zA-Z0-9._\-\[\]]+:\d+(?:/[^\s"'<>]*)?""",
    re.IGNORECASE,
)

DEFAULT_USER_AGENT = "Pulsarr/2.0"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class GithubRepoSource:
    id: str
    url: str
    label: str = ""


@dataclass
class WebsiteScrapeSource:
    id: str
    url: str
    label: str = ""


@dataclass
class CollectionResult:
    trackers: set[str] = field(default_factory=set)
    counts: dict[str, int] = field(default_factory=dict)   # per-source counts for logging
    ipv6_filtered: int = 0


LogFn = Callable[[str, str], Awaitable[None]]   # async (message, level) -> None


# ---------------------------------------------------------------------------
# Raw URL lists
# ---------------------------------------------------------------------------

async def fetch_raw_lists(
    session: aiohttp.ClientSession,
    urls: list[str],
    log: LogFn,
) -> set[str]:
    found: set[str] = set()
    await log(f"Step 1a: Downloading {len(urls)} raw list URL(s)...", "info")
    for url in urls:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": DEFAULT_USER_AGENT},
            ) as resp:
                text = await resp.text(errors="ignore")
            lines = [
                ln.strip() for ln in text.split("\n")
                if ln.strip() and VALID_SCHEMES.match(ln.strip())
            ]
            found.update(lines)
            await log(f"  {len(lines)} from: {url}", "info")
        except Exception as exc:
            await log(f"  Failed: {url} ({exc})", "warn")
    return found


# ---------------------------------------------------------------------------
# GitHub repos — crawled and cached by commit SHA
# ---------------------------------------------------------------------------

async def _get_repo_txt_files(
    session: aiohttp.ClientSession,
    repo_path: str,
    headers: dict,
    cache: dict,
) -> list[str]:
    """Returns list of raw.githubusercontent.com URLs for .txt files in the repo."""
    try:
        async with session.get(
            f"https://api.github.com/repos/{repo_path}/commits/HEAD",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                latest_sha = data.get("sha")
            elif resp.status in (403, 429):
                logger.warning("GitHub rate limit hit for %s", repo_path)
                latest_sha = None
            else:
                latest_sha = None
    except Exception as exc:
        logger.warning("GitHub API unreachable for %s: %s", repo_path, exc)
        latest_sha = None

    cached = cache.get(repo_path)

    if latest_sha is None:
        return cached.get("files", []) if cached else []

    if cached and cached.get("commit_sha") == latest_sha:
        return cached.get("files", [])

    try:
        async with session.get(
            f"https://api.github.com/repos/{repo_path}/git/trees/HEAD",
            params={"recursive": "1"},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return cached.get("files", []) if cached else []
            tree = await resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch tree for %s: %s", repo_path, exc)
        return cached.get("files", []) if cached else []

    txt_files = [
        f"https://raw.githubusercontent.com/{repo_path}/HEAD/{item['path']}"
        for item in tree.get("tree", [])
        if item.get("type") == "blob" and item.get("path", "").endswith(".txt")
    ][:20]

    cache[repo_path] = {"commit_sha": latest_sha, "files": txt_files}
    return txt_files


async def fetch_github_repos(
    session: aiohttp.ClientSession,
    repos: list[GithubRepoSource],
    github_token: str,
    cache_file: Path,
    log: LogFn,
) -> set[str]:
    if not repos:
        await log("Step 1b: No GitHub repos configured.", "info")
        return set()

    headers = {"User-Agent": DEFAULT_USER_AGENT}
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    cache: dict = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    found: set[str] = set()
    await log(f"Step 1b: Fetching {len(repos)} GitHub repo(s)...", "info")

    for repo in repos:
        repo_path = re.sub(r"^https://github\.com/", "", repo.url).rstrip("/").removesuffix(".git")
        if not re.match(r"^[^/]+/[^/]+$", repo_path):
            await log(f"  Invalid repo URL: {repo.url}", "warn")
            continue

        label = repo.label or repo.url
        txt_files = await _get_repo_txt_files(session, repo_path, headers, cache)

        repo_trackers: set[str] = set()
        for file_url in txt_files:
            try:
                async with session.get(
                    file_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    text = await resp.text(errors="ignore")
                lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
                tracker_lines = [ln for ln in lines if VALID_SCHEMES.match(ln)]
                if lines and len(tracker_lines) >= 5 and len(tracker_lines) / len(lines) > 0.5:
                    repo_trackers.update(tracker_lines)
            except Exception as exc:
                logger.warning("Failed to download %s: %s", file_url, exc)

        found.update(repo_trackers)
        await log(f"  {len(repo_trackers)} from: {label}", "info")

    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not save GitHub cache: %s", exc)

    return found


# ---------------------------------------------------------------------------
# Website scrapes
# ---------------------------------------------------------------------------

async def fetch_website_scrapes(
    session: aiohttp.ClientSession,
    sites: list[WebsiteScrapeSource],
    log: LogFn,
) -> set[str]:
    if not sites:
        await log("Step 1c: No website scrape sources configured.", "info")
        return set()

    found: set[str] = set()
    await log(f"Step 1c: Scraping {len(sites)} website(s)...", "info")

    for site in sites:
        try:
            async with session.get(
                site.url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": DEFAULT_USER_AGENT},
            ) as resp:
                text = await resp.text(errors="ignore")
            matches = {m.rstrip("/") for m in SCRAPE_PATTERN.findall(text)}
            found.update(matches)
            label = site.label or site.url
            await log(f"  {len(matches)} from: {label}", "info")
        except Exception as exc:
            await log(f"  Scrape failed ({site.url}): {exc}", "warn")

    return found


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------

def clean_trackers(trackers: set[str]) -> tuple[set[str], int]:
    """
    Removes IPv6-only trackers and trailing junk characters
    (stray quotes/backslashes scraped from poorly-formatted sources).
    Returns (cleaned_set, ipv6_filtered_count).
    """
    cleaned: set[str] = set()
    ipv6_count = 0

    for t in trackers:
        if IPV6_PATTERN.match(t):
            ipv6_count += 1
            continue
        fixed = TRAILING_JUNK.sub("", t)
        if VALID_SCHEMES.match(fixed):
            cleaned.add(fixed)

    return cleaned, ipv6_count


# ---------------------------------------------------------------------------
# Hostname resolution and IP-level deduplication
# ---------------------------------------------------------------------------

# Default port assumptions when none is specified in the URL
_DEFAULT_PORT = {"udp": 6969, "http": 80, "https": 443, "ws": 80, "wss": 443}

# Lower number = preferred when picking one URL from a duplicate group
_PROTO_PRIORITY = {"udp": 0, "http": 1, "https": 2, "ws": 3, "wss": 4}


async def _resolve_ip(host: str, loop, timeout: float = 5.0) -> str | None:
    """Resolve a hostname to its first IPv4 address. Returns None on any failure."""
    try:
        infos = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM),
            ),
            timeout=timeout,
        )
        return infos[0][4][0] if infos else None
    except Exception:
        return None


async def _resolve_all_via_container(
    urls: set[str],
    vpn_container: str,
) -> list[tuple[str, str | None]]:
    """
    Resolves tracker hostnames inside an ephemeral container sharing the VPN
    network namespace so DNS queries exit through the tunnel.
    Returns list of (url, ip_or_none). Falls back to (url, None) on any failure.
    """
    from .ping import IMAGE_NAME

    try:
        loop = asyncio.get_event_loop()

        def _run() -> tuple[int, str, str]:
            r = subprocess.run(
                [
                    "docker", "run", "--rm", "-i",
                    f"--network=container:{vpn_container}",
                    IMAGE_NAME,
                    "python3", "-m", "app.dns_worker",
                ],
                input="\n".join(urls),
                capture_output=True,
                text=True,
                timeout=120,
            )
            return r.returncode, r.stdout, r.stderr

        returncode, stdout, stderr = await loop.run_in_executor(None, _run)

        if returncode != 0:
            logger.error(
                "DNS container exited with code %d. stderr: %s",
                returncode, stderr[:500],
            )
            return [(url, None) for url in urls]

        if not stdout.strip():
            logger.error("DNS container produced no output. stderr: %s", stderr[:500])
            return [(url, None) for url in urls]

        raw = json.loads(stdout)
        return [(item["url"], item.get("ip")) for item in raw]

    except Exception as exc:
        logger.warning("Container DNS resolution failed: %s", exc)
        return [(url, None) for url in urls]


async def resolve_and_deduplicate(
    urls: set[str],
    log: LogFn,
    vpn_container: str = "",
    concurrency: int = 50,
) -> set[str]:
    """
    Resolves each tracker's hostname to an IP address and removes entries
    where multiple URLs resolve to the same (IP, port) — true duplicates
    that would ping the same physical server twice.

    Selection when duplicates exist: UDP preferred over HTTP/HTTPS; ties
    broken by shorter URL.

    URLs that fail DNS resolution are kept and deduplicated only against
    other URLs with the identical hostname:port, so a resolution failure
    never causes a tracker to be silently dropped.
    """
    await log(f"Resolving hostnames and checking for IP-level duplicates ({len(urls)} trackers)...", "info")

    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(concurrency)

    # Parse scheme/host/port for every URL up front
    meta: dict[str, tuple[str, str, int]] = {}
    for url in urls:
        p = urlparse(url)
        scheme = p.scheme.lower()
        host   = p.hostname or ""
        port   = p.port or _DEFAULT_PORT.get(scheme, 80)
        meta[url] = (scheme, host, port)

    async def _resolve_one(url: str) -> tuple[str, str | None]:
        _, host, _ = meta[url]
        if not host:
            return url, None
        async with sem:
            ip = await _resolve_ip(host, loop)
        return url, ip

    if vpn_container:
        await log(f"Routing DNS through VPN container '{vpn_container}'...", "info")
        resolved = await _resolve_all_via_container(urls, vpn_container)
    else:
        resolved = await asyncio.gather(*[_resolve_one(u) for u in urls])

    # Group by (ip, port) for resolved URLs, (hostname, port) for unresolved.
    # Unresolved URLs are only deduped against identical hostname:port strings.
    groups: dict[tuple, list[str]] = {}
    for url, ip in resolved:
        _, host, port = meta[url]
        key = ("ip", ip, port) if ip else ("host", host, port)
        groups.setdefault(key, []).append(url)

    kept: set[str] = set()
    total_dupes = 0

    for group_urls in groups.values():
        if len(group_urls) == 1:
            kept.add(group_urls[0])
            continue
        # Pick best: lowest protocol priority, then shortest URL as tiebreak
        best = min(group_urls, key=lambda u: (_PROTO_PRIORITY.get(meta[u][0], 9), len(u)))
        kept.add(best)
        total_dupes += len(group_urls) - 1

    if total_dupes > 0:
        await log(
            f"Removed {total_dupes} duplicate tracker(s) resolving to the same IP:port. "
            f"{len(kept)} remain.",
            "info",
        )
    else:
        await log(f"No IP-level duplicates found. {len(kept)} trackers ready.", "info")

    return kept


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

async def collect_all(
    session: aiohttp.ClientSession,
    tracker_urls: list[str],
    github_repos: list[GithubRepoSource],
    website_scrapes: list[WebsiteScrapeSource],
    manual_trackers: list[str],
    github_token: str,
    cache_file: Path,
    log: LogFn,
) -> CollectionResult:
    result = CollectionResult()

    await log("=== Collecting trackers from all sources ===", "step")

    raw = await fetch_raw_lists(session, tracker_urls, log)
    result.trackers.update(raw)

    before_gh = len(result.trackers)
    gh = await fetch_github_repos(session, github_repos, github_token, cache_file, log)
    result.trackers.update(gh)
    if github_repos:
        await log(f"  GitHub total: {len(result.trackers) - before_gh} new unique trackers.", "info")

    before_web = len(result.trackers)
    web = await fetch_website_scrapes(session, website_scrapes, log)
    result.trackers.update(web)
    if website_scrapes:
        await log(f"  Website total: {len(result.trackers) - before_web} new unique trackers.", "info")

    if manual_trackers:
        before_manual = len(result.trackers)
        valid_manual = {t for t in manual_trackers if VALID_SCHEMES.match(t)}
        result.trackers.update(valid_manual)
        await log(f"Step 1d: {len(result.trackers) - before_manual} manual trackers added.", "info")
    else:
        await log("Step 1d: No manual trackers configured.", "info")

    cleaned, ipv6_count = clean_trackers(result.trackers)
    result.trackers = cleaned
    result.ipv6_filtered = ipv6_count

    await log(
        f"[OK] Collection complete: {len(result.trackers)} unique trackers. "
        f"IPv6-only filtered: {ipv6_count}.",
        "ok",
    )

    return result
