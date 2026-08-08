"""Hard filters: seniority, role type, location. Config-driven, bias to recall —
a false positive costs one glance in the UI; a false negative is invisible forever."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .sources.base import Posting


@dataclass
class FilterResult:
    keep: bool
    reason: str = ""          # why it was dropped, e.g. "seniority:staff"
    seniority: str = "mid"    # mid | senior (for kept roles)


# A multi-location posting that lists any of these alongside a non-US city survives.
US_HINTS = [
    "united states", "usa", "u.s.", "north america", "remote (us", "remote - us",
    "remote, us", "us remote", "new york", "san francisco", "seattle", "austin",
    "denver", "chicago", "boston", "atlanta", "san jose", "sunnyvale",
    "mountain view", "palo alto", "menlo park", "bellevue", "redmond",
    "los angeles", "san diego", "portland", "washington", "philadelphia",
    "salt lake", "raleigh", "durham", "nashville", "columbus", "pittsburgh",
    "minneapolis", "miami", "dallas", "houston", "phoenix",
]


@lru_cache(maxsize=4096)
def _term_re(term: str) -> re.Pattern:
    # Word-ish boundaries so "ios" doesn't match "bios", "api" doesn't match "rapid".
    return re.compile(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])")


def _hit(term: str, text: str) -> bool:
    return bool(_term_re(term.lower()).search(text))


def _first_hit(terms: list[str], text: str) -> str | None:
    for t in terms:
        if _hit(t, text):
            return t
    return None


def classify(posting: Posting, filters_cfg: dict) -> FilterResult:
    title = posting.title.lower()
    dept = (posting.department or "").lower()
    title_dept = f"{title} | {dept}"

    # --- current employer: never surface her own company (or its sibling brands). Substring,
    # not word-boundary, match — LinkedIn writes entity names like "TikTok USDS Joint Venture"
    # and "ByteDance Ltd.", and a blocklist that misses one of those is worse than useless. ---
    company = posting.company.lower()
    for term in filters_cfg.get("company_exclude", []):
        if term.lower() in company:
            return FilterResult(False, f"company:{term}")

    # --- seniority (title only; departments say things like "Engineering Leadership") ---
    # "Member of Technical Staff" is an IC title (Cockroach Labs et al.) — its "staff"
    # is not a level. Neutralize the phrase before applying the seniority excludes.
    seniority_title = title.replace("member of technical staff", "")
    hit = _first_hit(filters_cfg.get("seniority_exclude", []), seniority_title)
    if hit:
        return FilterResult(False, f"seniority:{hit}")
    senior = _hit("senior", seniority_title) or bool(re.search(r"\bsr\.?(?=\s)", seniority_title))
    if senior and not filters_cfg.get("keep_senior_titles", True):
        return FilterResult(False, "seniority:senior")

    # Clearance requirements may appear in the job description.
    if filters_cfg.get("drop_clearance", True):
        hay = f"{title} {(posting.jd_text or '').lower()}"
        hit = _first_hit(filters_cfg.get("clearance_exclude", []), hay)
        if hit:
            return FilterResult(False, f"clearance:{hit}")

    # --- pure-SRE-oncall rule ---
    if filters_cfg.get("drop_sre_titles", True) and (
        "site reliability" in title or _hit("sre", title)
    ):
        return FilterResult(False, "role:sre")

    # --- role type (title + department; exclusion wins) ---
    hit = _first_hit(filters_cfg.get("role_exclude", []), title_dept)
    if hit:
        return FilterResult(False, f"role:{hit}")
    if not _first_hit(filters_cfg.get("role_include", []), title_dept):
        return FilterResult(False, "role:no-include-match")

    # --- location: drop only on a clear non-US signal; ambiguous stays ---
    loc = posting.location.lower()
    if loc:
        for term in (filters_cfg.get("location") or {}).get("non_us_signals", []):
            if term in loc:
                if any(h in loc for h in US_HINTS):
                    break  # multi-location posting with a US option — keep
                return FilterResult(False, f"location:{term}")

    return FilterResult(True, seniority="senior" if senior else "mid")
