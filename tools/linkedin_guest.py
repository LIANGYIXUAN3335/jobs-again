"""linkedin_guest.py — shared guest-endpoint plumbing for the LinkedIn pipeline
(used by linkedin_search.py and linkedin_enrich.py; flow overview in linkedin_alerts.py).

Everything here is UNAUTHENTICATED: the jobs-guest endpoints LinkedIn serves to logged-out
visitors. No cookies, no account, browser UA only — the account is never touched. LinkedIn
throttles these per-IP (429/999/403 or an authwall redirect), so callers must pace every
request with polite_pause() and stop via the blocked() signal instead of hammering.

Selectors below were confirmed against live responses on 2026-07-29; if LinkedIn renames
its CSS classes the parsers return empty fields, which linkedin_enrich.py reports loudly
(and linkedin_ingest.py refuses thin JDs), so drift fails visibly rather than silently.
"""
from __future__ import annotations

import datetime
import html as htmlmod
import random
import re
import sys
import time
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent.parent          # jobscan/
sys.path.insert(0, str(BASE))

from app.sources.base import strip_html, today_la  # noqa: E402

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"

# A browser UA is required — the honest jobscan/0.1 UA gets the bot-wall immediately.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def make_client(timeout_s: float = 30.0) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout_s, connect=10.0),
        headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
        follow_redirects=True,
    )


def polite_pause(delay_s: float) -> None:
    """Base delay + jitter so the request train doesn't look metronomic."""
    time.sleep(delay_s + random.uniform(0.0, 2.0))


def blocked(resp: httpx.Response | None) -> bool:
    """LinkedIn's throttle/bot-wall signals. None means the transport itself failed."""
    if resp is None:
        return True
    if resp.status_code in (403, 429, 999):
        return True
    return "authwall" in str(resp.url) or "/checkpoint/" in str(resp.url)


# ---------------------------------------------------------------- posting page

_MARKUP = re.compile(r'<div[^>]*class="[^"]*show-more-less-html__markup[^"]*"[^>]*>')
_TITLE = re.compile(r'top-card-layout__title[^>]*>\s*([^<]+)')
_COMPANY = re.compile(r'topcard__org-name-link[^>]*>\s*([^<]+)')
_FLAVORS = re.compile(r'topcard__flavor[^"]*"[^>]*>\s*([^<]+)')
_POSTED = re.compile(r'posted-time-ago__text[^>]*>\s*([^<]+)')
_SALARY = re.compile(r'compensation__salary[^>]*>\s*([^<]+)')
_REL_AGO = re.compile(r'(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago', re.I)
_REL_UNIT_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def _div_block(page: str, start: int) -> str:
    """Slice out one balanced <div>…</div> starting at `start` (the opening tag).
    Non-greedy .*?</div> would truncate at the first nested close."""
    depth = 0
    for m in re.finditer(r"<(/?)div\b", page[start:]):
        if m.group(1):
            depth -= 1
            if depth == 0:
                return page[start:start + m.end()]
        else:
            depth += 1
    return page[start:]


def _clean(s: str | None) -> str:
    return htmlmod.unescape(s or "").strip()


def rel_to_date(text: str, today: datetime.date | None = None) -> str | None:
    """'10 hours ago' / '3 days ago' → YYYY-MM-DD (guest pages only give relative time)."""
    m = _REL_AGO.search(text or "")
    if not m:
        return None
    days = int(m.group(1)) * _REL_UNIT_DAYS[m.group(2).lower()]
    return ((today or today_la()) - datetime.timedelta(days=days)).isoformat()


def parse_posting(page: str) -> dict:
    """Guest jobPosting page → {title, company, location, jd_text, salary, posted_date}."""
    title = _clean(m.group(1)) if (m := _TITLE.search(page)) else ""
    company = _clean(m.group(1)) if (m := _COMPANY.search(page)) else ""
    # flavors are [.., company, location, ..] with empty decorative spans mixed in
    flavors = [f for f in (_clean(x) for x in _FLAVORS.findall(page)) if f and f != company]
    location = flavors[0] if flavors else ""
    jd_text = ""
    if m := _MARKUP.search(page):
        jd_text = strip_html(_div_block(page, m.start()))
    salary = _clean(m.group(1)) if (m := _SALARY.search(page)) else ""
    posted = rel_to_date(_clean(m.group(1))) if (m := _POSTED.search(page)) else None
    return {"title": title, "company": company, "location": location,
            "jd_text": jd_text, "salary": salary, "posted_date": posted}


# ---------------------------------------------------------------- search page

_CARD_SPLIT = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"')
_S_TITLE = re.compile(r'base-search-card__title[^>]*>\s*([^<]+)')
_S_COMPANY = re.compile(r'base-search-card__subtitle[^>]*>\s*<a[^>]*>\s*([^<]+)', re.S)
_S_LOCATION = re.compile(r'job-search-card__location[^>]*>\s*([^<]+)')
_S_DATE = re.compile(r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})"')


def parse_search(page: str) -> list[dict]:
    """Guest search response → [{id, title, company, location, url, posted_date}]."""
    parts = _CARD_SPLIT.split(page)
    cards = []
    # parts = [preamble, id1, chunk1, id2, chunk2, ...]
    for jid, chunk in zip(parts[1::2], parts[2::2]):
        cards.append({
            "id": jid,
            "title": _clean(m.group(1)) if (m := _S_TITLE.search(chunk)) else "",
            "company": _clean(m.group(1)) if (m := _S_COMPANY.search(chunk)) else "",
            "location": _clean(m.group(1)) if (m := _S_LOCATION.search(chunk)) else "",
            "url": f"https://www.linkedin.com/jobs/view/{jid}/",
            "posted_date": m.group(1) if (m := _S_DATE.search(chunk)) else None,
        })
    return cards
