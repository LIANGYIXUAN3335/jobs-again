"""SmartRecruiters public postings API: api.smartrecruiters.com/v1/companies/{slug}/postings.
NOTE: companies control public-API visibility — some (e.g. Visa) expose only a handful of
postings here even though their careers site lists many more. Detail (lazy): posting `ref`
URL whose jobAd.sections carry the JD HTML.
"""
from __future__ import annotations

import time

import httpx

from .base import Posting, polite_get, posted_day, strip_html

API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
PAGE = 100


def fetch(client: httpx.Client, company: str, slug: str, retries: int = 2) -> list[Posting]:
    rows: list[dict] = []
    offset = 0
    total = 1
    while offset < total:
        data = polite_get(client, f"{API.format(slug=slug)}?limit={PAGE}&offset={offset}",
                          retries=retries).json()
        total = int(data.get("totalFound") or 0)
        page = data.get("content") or []
        if not page:
            break
        rows.extend(page)
        offset += PAGE
        time.sleep(0.25)

    out: list[Posting] = []
    for j in rows:
        raw_id = str(j.get("id") or "")
        if not raw_id:
            continue
        loc = j.get("location") or {}
        location = (loc.get("fullLocation")
                    or ", ".join(x for x in (loc.get("city"), loc.get("region"),
                                             loc.get("country")) if x))
        detail_url = j.get("ref") or f"{API.format(slug=slug)}/{raw_id}"

        def _jd(u: str = detail_url) -> str:
            time.sleep(0.4)
            sections = ((polite_get(client, u, retries=1).json().get("jobAd") or {})
                        .get("sections") or {})
            return strip_html("\n".join(
                (s or {}).get("text") or "" for s in sections.values()))

        out.append(Posting(
            ats="smartrecruiters",
            company=company,
            slug=slug,
            raw_id=raw_id,
            title=(j.get("name") or "").strip(),
            location=location,
            jd_text="",
            apply_url=f"https://jobs.smartrecruiters.com/{slug}/{raw_id}",
            department=((j.get("department") or {}).get("label") or ""),
            is_remote=bool(loc.get("remote")),
            posted_at=posted_day(j.get("releasedDate")),
            fetch_jd=_jd,
        ))
    return out
