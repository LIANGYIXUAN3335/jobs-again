"""robots.txt gate for every outbound fetch.

Ported from MadsLorentzen/ai-job-search `tools/robots_check.py` (2026-08-19),
reimplemented on httpx instead of a curl subprocess to match this codebase, and
extended with a per-origin cache, a three-way mode switch, and an allowlist.

Why this exists: jobscan fetches eight ATS boards plus LinkedIn's guest
endpoints. Until now nothing read robots.txt at all — every request assumed
permission it had never checked.

RFC 9309, deliberately on the cautious side:
  * longest-match wins; on equal specificity Disallow wins
  * a Disallow for either our UA token or "*" blocks the fetch
  * blank lines inside a record do NOT end it — Python's stdlib robotparser
    drops the rest of the group in that case, which fails OPEN
  * patterns are percent-decoded before matching, so "Disallow: /foo%20bar"
    matches "/foo bar" instead of being silently skipped (another fail-open)
  * a 200 whose body is not a robots.txt (WAF/HTML error page) parses to zero
    rules, which would read as "allowed" — that soft-200 is treated as unreadable
  * 404 = no published policy = permission
  * anything else leaves permission UNCONFIRMED

Modes (config.yaml `robots.mode`):
  off      — no checking at all (previous behaviour)
  report   — check, log what WOULD be blocked, allow everything through
  enforce  — check and actually block; UNCONFIRMED blocks too (fail closed)
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import unquote, urlsplit

import httpx

UA_TOKEN = "jobscan"          # matches USER_AGENT in app/sources/base.py
CACHE_TTL_SECONDS = 6 * 3600

_ROBOTS_FIELDS = ("user-agent", "allow", "disallow", "sitemap", "crawl-delay", "host")

_cache: dict[str, tuple[float, str | None, str]] = {}   # origin -> (fetched_at, body|None, note)
_lock = threading.Lock()

# Set once per run by app.scan / whoever owns the config, so the hot path
# (polite_get) does not have to thread cfg through every source module.
_policy: dict = {}
_log = None


def configure(cfg: dict, log=None) -> None:
    """Install the run's robots policy. Call once, early."""
    global _policy, _log
    _policy = cfg or {}
    _log = log


class RobotsDenied(RuntimeError):
    """Raised in enforce mode when robots.txt does not permit a fetch."""


# ---------------------------------------------------------------- parsing

def is_robots_body(text: str) -> bool:
    """A misconfigured host can answer /robots.txt with 200 and an HTML error page.

    That body parses to zero rules, and zero rules read as "allowed" — a soft-200
    would grant permission that was never given. An empty/whitespace-only body IS
    a valid allow-all under RFC 9309 and stays allowed.
    """
    if not text.strip():
        return True
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip().lower()
        if ":" in line and line.split(":", 1)[0].strip() in _ROBOTS_FIELDS:
            return True
    return False


def _groups(text: str) -> dict[str, list[tuple[bool, str]]]:
    """user-agent -> [(is_allow, pattern)], tolerating blank lines inside a record."""
    out: dict[str, list[tuple[bool, str]]] = {}
    agents: list[str] = []
    expect = True
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if not expect:
                agents, expect = [], True
            agents.append(value.lower())
            out.setdefault(value.lower(), [])
        elif field in ("allow", "disallow") and agents:
            expect = False
            for a in agents:
                out[a].append((field == "allow", value))
    return out


def _match(pattern: str, path: str) -> int:
    """RFC 9309 wildcard match; returns match length or -1."""
    if pattern == "":
        return -1
    pattern = unquote(pattern)
    rx = "^" + "".join(
        ".*" if c == "*" else ("$" if c == "$" else re.escape(c)) for c in pattern
    )
    return len(pattern) if re.match(rx, path) else -1


def allowed(text: str, agent: str, path: str) -> bool:
    g = _groups(text)
    rules = g.get(agent.lower()) or g.get("*") or []
    best_len, best_allow = -1, True
    for is_allow, pat in rules:
        n = _match(pat, path)
        if n > best_len or (n == best_len and n >= 0 and not is_allow):
            best_len, best_allow = n, is_allow      # ties -> Disallow wins
    return True if best_len < 0 else best_allow


# ---------------------------------------------------------------- fetching

def _fetch_robots(client: httpx.Client, origin: str) -> tuple[str | None, str]:
    """Return (body, note). body is None when permission could not be confirmed."""
    try:
        r = client.get(f"{origin}/robots.txt", timeout=12.0)
    except Exception as exc:                       # noqa: BLE001
        return None, type(exc).__name__
    if r.status_code == 404:
        return "", "no robots.txt published"
    if r.status_code == 200:
        if not is_robots_body(r.text):
            return None, "HTTP 200 but body is not a robots.txt"
        return r.text, "ok"
    return None, f"HTTP {r.status_code}"


def _body_for(client: httpx.Client, origin: str) -> tuple[str | None, str]:
    now = time.time()
    with _lock:
        hit = _cache.get(origin)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1], hit[2]
    body, note = _fetch_robots(client, origin)
    with _lock:
        _cache[origin] = (now, body, note)
    return body, note


# ---------------------------------------------------------------- the gate

def check(client: httpx.Client, url: str) -> tuple[bool, str]:
    """(permitted, reason). Never raises; callers decide what to do."""
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = unquote(parts.path) or "/"
    if parts.query:
        path += "?" + parts.query

    body, note = _body_for(client, origin)
    if body is None:
        return False, f"UNCONFIRMED ({note})"
    for agent in (UA_TOKEN, "*"):
        if not allowed(body, agent, path):
            return False, f"DISALLOWED for {agent}"
    return True, "allowed"


def gate(client: httpx.Client, url: str, cfg: dict | None = None,
         log=None) -> None:
    """Enforce the configured policy for `url`. Raises RobotsDenied in enforce mode."""
    rc = ((cfg if cfg is not None else _policy) or {}).get("robots") or {}
    log = log if log is not None else _log
    mode = str(rc.get("mode", "report")).lower()
    if mode == "off":
        return

    host = urlsplit(url).netloc
    for pattern in rc.get("allow_hosts") or []:
        if host == pattern or host.endswith("." + str(pattern).lstrip(".")):
            return

    ok, reason = check(client, url)
    if ok:
        return
    msg = f"robots: {reason} — {url}"
    if mode == "enforce":
        raise RobotsDenied(msg)
    if log is not None:
        log(f"robots[report-only] WOULD BLOCK: {msg}")
    else:
        print(f"robots[report-only] WOULD BLOCK: {msg}")
