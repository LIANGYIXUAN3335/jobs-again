"""Workday CXS API. Slug format: "tenant:wdN:SiteName", e.g. "mastercard:wd1:CorporateCareers".
List: POST {base}/wday/cxs/{tenant}/{site}/jobs {limit,offset} (limit max 20).
Detail (lazy, kept postings only): GET {base}/wday/cxs/{tenant}/{site}{externalPath}.
"""
from __future__ import annotations

import time

import httpx

from .. import robots
from .base import Posting, posted_day_relative, strip_html

PAGE = 20


def _post(client: httpx.Client, url: str, body: dict, retries: int) -> dict:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            robots.gate(client, url)
            r = client.post(url, json=body, headers={"Accept": "application/json"})
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(2.0 * (2 ** attempt))
    raise last  # type: ignore[misc]


def fetch(client: httpx.Client, company: str, slug: str, retries: int = 2) -> list[Posting]:
    tenant, host, site = slug.split(":")
    base = f"https://{tenant}.{host}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"

    rows: list[dict] = []
    offset = 0
    total = 1
    while offset < total:
        data = _post(client, api, {"limit": PAGE, "offset": offset}, retries)
        if offset == 0:  # Workday reports the real total only on the first page
            total = int(data.get("total") or 0)
        page = data.get("jobPostings") or []
        if not page:
            break
        rows.extend(page)
        offset += PAGE
        time.sleep(0.25)

    out: list[Posting] = []
    for j in rows:
        path = j.get("externalPath") or ""
        if not path:
            continue
        bullets = j.get("bulletFields") or []
        raw_id = str(bullets[0]) if bullets else path
        detail_url = f"{base}/wday/cxs/{tenant}/{site}{path}"

        def _jd(u: str = detail_url) -> str:
            time.sleep(0.4)
            robots.gate(client, u)
            r = client.get(u, headers={"Accept": "application/json"})
            r.raise_for_status()
            info = r.json().get("jobPostingInfo") or {}
            country = (info.get("country") or {}).get("descriptor") or ""
            jd = strip_html(info.get("jobDescription") or "")
            # Country from the detail is authoritative; surface it for the record.
            return f"{jd}\n\nCountry: {country}".strip() if country else jd

        out.append(Posting(
            ats="workday",
            company=company,
            slug=slug,
            raw_id=raw_id,
            title=(j.get("title") or "").strip(),
            location=(j.get("locationsText") or "").strip(),
            jd_text="",
            apply_url=f"{base}/en-US/{site}{path}",
            is_remote="remote" in (j.get("locationsText") or "").lower(),
            # Workday exposes no date field, only the rendered "Posted 5 Days Ago" string.
            posted_at=posted_day_relative(j.get("postedOn") or ""),
            fetch_jd=_jd,
        ))
    return out
