"""Lever postings API: api.lever.co/v0/postings/{slug}?mode=json"""
from __future__ import annotations

import httpx

from .base import Posting, polite_get, strip_html

API = "https://api.lever.co/v0/postings/{slug}?mode=json"


def parse_job(company: str, slug: str, j: dict) -> Posting:
    cats = j.get("categories") or {}
    locs = [cats.get("location") or ""] + list(cats.get("allLocations") or [])
    location = ", ".join(dict.fromkeys(l for l in locs if l))

    parts = [j.get("descriptionPlain") or strip_html(j.get("description") or "")]
    for lst in j.get("lists") or []:
        parts.append(lst.get("text") or "")
        parts.append(strip_html(lst.get("content") or ""))
    parts.append(j.get("additionalPlain") or "")
    jd_text = "\n".join(p for p in parts if p).strip()

    workplace = (j.get("workplaceType") or "").lower()
    department = "; ".join(x for x in [cats.get("team"), cats.get("department")] if x)
    return Posting(
        ats="lever",
        company=company,
        slug=slug,
        raw_id=str(j["id"]),
        title=(j.get("text") or "").strip(),
        location=location,
        jd_text=jd_text,
        apply_url=j.get("hostedUrl") or j.get("applyUrl") or "",
        department=department,
        is_remote=workplace == "remote" or "remote" in location.lower(),
    )


def fetch(client: httpx.Client, company: str, slug: str, retries: int = 2) -> list[Posting]:
    data = polite_get(client, API.format(slug=slug), retries=retries).json()
    if not isinstance(data, list):
        raise ValueError(f"lever board {slug!r}: unexpected response shape")
    return [parse_job(company, slug, j) for j in data]
