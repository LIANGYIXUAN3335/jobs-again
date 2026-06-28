"""Avature-hosted career sites (e.g. Bloomberg): {tenant}.avature.net/{path}/SearchJobs.
No JSON API — but list + detail are clean server-rendered HTML, so we parse that.
Slug format: "tenant" or "tenant:path" (path defaults to "careers").
List pages are capped at 12 items; detail (lazy, kept postings only) carries the full JD.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import httpx

from .base import Posting, polite_get, strip_html

PAGE = 12
_ARTICLE = re.compile(r'<article class="article article--result"', re.I)
_LINK = re.compile(r'href="(https://[^"]+/JobDetail/[^"]+/(\d+))"[^>]*>\s*([^<]+?)\s*</a>', re.I)
_LOC = re.compile(r'class="list-item-location"[^>]*>\s*([^<]+?)\s*<', re.I)
_TOTAL = re.compile(r'(\d+)\s*(?:results|jobs|positions|openings)', re.I)
_JD_START = re.compile(r'Description\s*&amp;\s*Requirements|Description\s*&\s*Requirements', re.I)
_JD_END = re.compile(r'<[^>]+class="[^"]*(?:apply|job-actions|similar)[^"]*"|>\s*Apply\s*<', re.I)


def _parse_list_page(page_html: str) -> list[tuple[str, str, str, str]]:
    """→ [(detail_url, raw_id, title, location)] for one search-results page."""
    out = []
    starts = [m.start() for m in _ARTICLE.finditer(page_html)]
    for i, s in enumerate(starts):
        chunk = page_html[s:starts[i + 1] if i + 1 < len(starts) else s + 4000]
        link = _LINK.search(chunk)
        if not link:
            continue
        loc = _LOC.search(chunk)
        out.append((link.group(1), link.group(2),
                    html_mod.unescape(link.group(3)).strip(),
                    html_mod.unescape(loc.group(1)).strip() if loc else ""))
    return out


def _extract_jd(detail_html: str) -> str:
    start = _JD_START.search(detail_html)
    seg = detail_html[start.end():] if start else detail_html
    end = _JD_END.search(seg)
    if end:
        seg = seg[:end.start()]
    return strip_html(seg)


def fetch(client: httpx.Client, company: str, slug: str, retries: int = 2) -> list[Posting]:
    tenant, _, path = slug.partition(":")
    base = f"https://{tenant}.avature.net/{path or 'careers'}"

    seen: dict[str, tuple[str, str, str]] = {}
    offset = 0
    total = None
    while True:
        page_html = polite_get(
            client, f"{base}/SearchJobs/?jobRecordsPerPage={PAGE}&jobOffset={offset}",
            retries=retries).text
        if total is None:
            m = _TOTAL.search(page_html)
            total = int(m.group(1)) if m else 0
        rows = _parse_list_page(page_html)
        new = {rid: (url, title, loc) for url, rid, title, loc in rows if rid not in seen}
        if not new:
            break
        seen.update(new)
        offset += PAGE
        if offset >= total:
            break
        time.sleep(0.25)

    out: list[Posting] = []
    for raw_id, (url, title, location) in seen.items():

        def _jd(u: str = url) -> str:
            time.sleep(0.4)
            return _extract_jd(polite_get(client, u, retries=1).text)

        out.append(Posting(
            ats="avature",
            company=company,
            slug=slug,
            raw_id=raw_id,
            title=title,
            location=location,
            jd_text="",
            apply_url=url,
            is_remote="remote" in location.lower(),
            fetch_jd=_jd,
        ))
    return out
