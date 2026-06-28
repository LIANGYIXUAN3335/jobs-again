"""Greenhouse job-board API: boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"""
from __future__ import annotations

import httpx

from .base import Posting, polite_get, strip_html

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


def parse_job(company: str, slug: str, j: dict) -> Posting:
    loc = ((j.get("location") or {}).get("name") or "").strip()
    offices = [o.get("name", "") for o in (j.get("offices") or []) if o.get("name")]
    parts = [loc] + [o for o in offices if o and o != loc]
    location = " · ".join(p for p in parts if p)
    departments = "; ".join(d.get("name", "") for d in (j.get("departments") or []) if d.get("name"))
    raw_id = str(j["id"])
    return Posting(
        ats="greenhouse",
        company=company,
        slug=slug,
        raw_id=raw_id,
        title=(j.get("title") or "").strip(),
        location=location,
        jd_text=strip_html(j.get("content") or ""),
        # NOT absolute_url — many companies (MongoDB, Stripe, Databricks…) set it to a vanity
        # careers domain that either 403s (MongoDB/CloudFront) or redirects away from greenhouse,
        # AND the browser extension only fires on greenhouse.io. The ATS-hosted embed form loads
        # reliably, stays on greenhouse, and is the exact page the extension is verified against.
        apply_url=f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={raw_id}",
        department=departments,
        is_remote="remote" in location.lower(),
    )


def fetch(client: httpx.Client, company: str, slug: str, retries: int = 2) -> list[Posting]:
    data = polite_get(client, API.format(slug=slug), retries=retries).json()
    return [parse_job(company, slug, j) for j in data.get("jobs", [])]
