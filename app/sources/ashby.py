"""Ashby posting API: api.ashbyhq.com/posting-api/job-board/{slug}"""
from __future__ import annotations

import httpx

from .base import Posting, polite_get, strip_html

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def parse_job(company: str, slug: str, j: dict) -> Posting:
    secondary = [s.get("location", "") for s in (j.get("secondaryLocations") or [])]
    locs = [j.get("location") or ""] + secondary
    location = ", ".join(dict.fromkeys(l for l in locs if l))
    jd_text = (j.get("descriptionPlain") or "").strip() or strip_html(j.get("descriptionHtml") or "")
    department = "; ".join(x for x in [j.get("department"), j.get("team")] if x)
    workplace = (j.get("workplaceType") or "").lower()
    return Posting(
        ats="ashby",
        company=company,
        slug=slug,
        raw_id=str(j["id"]),
        title=(j.get("title") or "").strip(),
        location=location,
        jd_text=jd_text,
        apply_url=j.get("jobUrl") or j.get("applyUrl") or "",
        department=department,
        is_remote=bool(j.get("isRemote")) or workplace == "remote" or "remote" in location.lower(),
    )


def fetch(client: httpx.Client, company: str, slug: str, retries: int = 2) -> list[Posting]:
    data = polite_get(client, API.format(slug=slug), retries=retries).json()
    jobs = [j for j in data.get("jobs", []) if j.get("isListed", True)]
    return [parse_job(company, slug, j) for j in jobs]
