"""Gem job boards: jobs.gem.com/{slug}

Gem renders the board entirely client-side — fetching the page HTML gets a ~3KB React shell
with zero job data, so there is no scrape-the-HTML path. What it does have is a PUBLIC batched
GraphQL endpoint that the board's own JS calls with no auth:

    POST https://jobs.gem.com/api/public/graphql/batch
    body: [{"operationName": ..., "query": ..., "variables": {...}}]

The two operations below are lifted VERBATIM from Gem's own bundles
(static.gem.com/scripts/ExternalJobBoardList.*.js and jobBoards.*.js, read 2026-08-16), so the
field selections match what the site itself asks for. Introspection is disabled on this
endpoint, so if Gem changes the schema this file has to be re-derived from those bundles the
same way — grep the bundle for `query JobBoardList` / `query ExternalJobPosting`.

Lazy JD (same contract as workday/rippling/smartrecruiters): the list query returns titles and
locations but no description, so `fetch_jd` pulls the detail per posting and app/scan.py only
calls it for postings that survived the filters.
"""
from __future__ import annotations

import time

import httpx

from .. import robots
from .base import Posting, posted_day, strip_html

API = "https://jobs.gem.com/api/public/graphql/batch"
BOARD_URL = "https://jobs.gem.com/{slug}"
JOB_URL = "https://jobs.gem.com/{slug}/{ext_id}"

LIST_QUERY = """
query JobBoardList($boardId: String!) {
  oatsExternalJobPostings(boardId: $boardId) {
    jobPostings {
      id
      extId
      title
      locations { id name city isoCountry isRemote extId }
      job { id department { id name extId } locationType employmentType }
    }
  }
}
"""

DETAIL_QUERY = """
query ExternalJobPosting($boardId: String!, $extId: String!) {
  oatsExternalJobPosting(boardId: $boardId, extId: $extId) {
    id
    title
    descriptionHtml
    extId
    firstPublishedTsSec
    locations { name city isoCountry isRemote }
    job { locationType employmentType department { name } }
  }
}
"""


def _post(client: httpx.Client, op: str, query: str, variables: dict, retries: int = 2):
    """One batched GraphQL call. Gem always answers 200 — errors live in the body, so a
    non-empty `errors` array has to be treated as the failure, not the status code."""
    body = [{"operationName": op, "query": query, "variables": variables}]
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            robots.gate(client, API)
            r = client.post(API, json=body, headers={"content-type": "application/json"})
            r.raise_for_status()
            payload = r.json()
            first = payload[0] if isinstance(payload, list) and payload else payload
            if first.get("errors"):
                raise RuntimeError(str(first["errors"])[:200])
            return first.get("data") or {}
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"gem graphql {op} failed: {last}")


def _location(posting: dict) -> tuple[str, bool]:
    locs = posting.get("locations") or []
    names = [l.get("name") or l.get("city") or "" for l in locs]
    location = ", ".join(dict.fromkeys(n for n in names if n))
    remote = any(l.get("isRemote") for l in locs) \
        or (posting.get("job") or {}).get("locationType", "").upper() == "REMOTE" \
        or "remote" in location.lower()
    return location, bool(remote)


def parse_job(company: str, slug: str, posting: dict,
              client: httpx.Client | None = None) -> Posting:
    ext_id = posting.get("extId") or posting.get("id") or ""
    location, remote = _location(posting)
    job = posting.get("job") or {}
    department = ((job.get("department") or {}).get("name") or "")

    def _jd() -> str:
        # Detail call is deliberately per-posting and paced: the board's own UI does the same
        # one-at-a-time, and this only runs for postings that already passed the filters.
        time.sleep(0.4)
        data = _post(client, "ExternalJobPosting", DETAIL_QUERY,
                     {"boardId": slug, "extId": ext_id}, retries=1)
        detail = data.get("oatsExternalJobPosting") or {}
        return strip_html(detail.get("descriptionHtml") or "")

    return Posting(
        ats="gem",
        company=company,
        slug=slug,
        raw_id=str(ext_id),
        title=(posting.get("title") or "").strip(),
        location=location,
        jd_text="",
        apply_url=JOB_URL.format(slug=slug, ext_id=ext_id),
        department=department,
        is_remote=remote,
        posted_at=posted_day(posting.get("firstPublishedTsSec")),
        fetch_jd=_jd if client is not None else None,
    )


def fetch(client: httpx.Client, company: str, slug: str, retries: int = 2) -> list[Posting]:
    data = _post(client, "JobBoardList", LIST_QUERY, {"boardId": slug}, retries=retries)
    postings = ((data.get("oatsExternalJobPostings") or {}).get("jobPostings")) or []
    return [parse_job(company, slug, p, client) for p in postings]
