"""Rippling's own ATS public board API: api.rippling.com/platform/api/ats/v1/board/{slug}.
List has no JD; detail (lazy, kept postings only): .../board/{slug}/jobs/{uuid} whose
`description` is a dict of HTML sections.
"""
from __future__ import annotations

import time

import httpx

from .base import Posting, polite_get, strip_html

API = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"


def fetch(client: httpx.Client, company: str, slug: str, retries: int = 2) -> list[Posting]:
    jobs = polite_get(client, API.format(slug=slug), retries=retries).json()

    out: list[Posting] = []
    for j in jobs:
        uuid = str(j.get("uuid") or "")
        if not uuid:
            continue
        location = ((j.get("workLocation") or {}).get("label") or "").strip()
        detail_url = f"{API.format(slug=slug)}/{uuid}"

        def _jd(u: str = detail_url) -> str:
            time.sleep(0.4)
            desc = (polite_get(client, u, retries=1).json().get("description") or {})
            if isinstance(desc, dict):
                return strip_html("\n".join(v for v in desc.values() if v))
            return strip_html(str(desc))

        out.append(Posting(
            ats="rippling",
            company=company,
            slug=slug,
            raw_id=uuid,
            title=(j.get("name") or "").strip(),
            location=location,
            jd_text="",
            apply_url=j.get("url") or f"https://ats.rippling.com/{slug}/jobs/{uuid}",
            department=((j.get("department") or {}).get("label") or ""),
            is_remote="remote" in location.lower(),
            fetch_jd=_jd,
        ))
    return out
