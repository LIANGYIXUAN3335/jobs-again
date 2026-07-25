"""Add a role from an UNSUPPORTED ATS by hand — JD captured however you like (browser read,
copy-paste). It inserts the role into jobscan.db so it flows through the SAME pipeline as
scanned roles: tailoring → match score → optimize → a scored, [id]-named PDF. It also shows
in the UI and the pre-screen. No scanner / no API needed — works for Apple, Google, Workday,
SmartRecruiters, or any site whose JD you can read.

    from app import config, store, manual, tailoring
    cfg = config.load_config(); conn = store.connect_jobs(cfg)
    jid = manual.add_role(cfg, conn, "Apple",
                          "Software Engineer, Cloud Storage",
                          jd_text=JD, apply_url=URL, location="Cupertino, CA")
    tailoring.write_prompt(cfg, conn, jid)      # then tailor the yaml + ingest_file(...)
"""
from __future__ import annotations

import hashlib
import re

from . import config as cfgmod
from . import store
from .sources.base import posted_day_from_jd


def _slug(s: str, n: int = 44) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:n] or "role"


# Query params that ARE the job's identity, as opposed to view state (q=, location=,
# sort_by=, filter_*, utm_*…). Eightfold uses pid, Deel's careers site uses ats_id (the
# Ashby posting UUID), Greenhouse embeds gh_jid, Workday-adjacent portals use req ids.
# Everything not listed here is search/view state and gets stripped.
_IDENTITY_PARAMS = ("pid", "ats_id", "gh_jid", "jid", "job_id", "jobid",
                    "req_id", "reqid", "requisition_id", "token", "id")


def canonical_capture_url(url: str) -> str:
    """Stable identity for a captured posting. List/detail SPAs bury the job id in ONE query
    param among a dozen view params, so hashing the raw URL would mint a new cap: role every
    time the same job is reached through a different search. Keep only the identity params
    (see _IDENTITY_PARAMS — without this, every Deel job canonicalized to the same
    /careers/job/ path and all captures collided into one role); when none are present the
    path carries the id (Google, Apple, Tesla) and the query goes entirely. The result is
    still a working link, so it doubles as the stored apply_url."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    s = urlsplit((url or "").strip())
    q = dict(parse_qsl(s.query))
    keep = {k: q[k] for k in _IDENTITY_PARAMS if k in q}
    return urlunsplit((s.scheme, s.netloc, s.path,
                       urlencode(dict(sorted(keep.items()))) if keep else "", ""))


_SLUG_ACRONYMS = {"ai": "AI", "ml": "ML", "api": "API", "apis": "APIs", "qa": "QA",
                  "sre": "SRE", "ui": "UI", "ux": "UX", "gpu": "GPU", "hpc": "HPC",
                  "llm": "LLM", "iot": "IoT", "ios": "iOS", "sdet": "SDET", "sdk": "SDK",
                  "grpc": "gRPC", "sql": "SQL", "aws": "AWS", "gcp": "GCP"}


def title_from_url(url: str) -> str:
    """Best-effort role title from a careers-page URL slug
    (…/careers/search/job/software-engineer-generalist-ai-inference--221961 →
    "Software Engineer Generalist AI Inference").

    The fallback for when a page's own title extraction came back as site chrome: Tesla's
    careers SPA puts its header nav in the first <h1>, so 67 captures landed with the title
    "Tesla homepageCareersSkip to main content" — which the role-family gate then read as a
    CONTENT role and dropped. The URL slug was the real title all along."""
    from urllib.parse import unquote, urlsplit
    seg = unquote(urlsplit(url or "").path).rstrip("/").rsplit("/", 1)[-1]
    seg = re.sub(r"-*\d{4,}$", "", seg)                    # trailing req id
    words = [w for w in re.split(r"[-_]+", seg) if w and not w.isdigit()]
    return " ".join(_SLUG_ACRONYMS.get(w.lower(), w.capitalize()) for w in words)[:160]


# Title extractions that are site chrome, not a role — never store these; re-derive instead.
BAD_TITLE = re.compile(r"skip to (?:main )?content|homepage|access denied|^\s*careers?\s*$|"
                       r"cookie|sign.?in|log.?in", re.I)


def looks_blocked(jd_text: str) -> bool:
    """A bot-protection interstitial captured instead of a JD (Akamai's 'Access Denied' page).
    There is nothing to score in it, so refuse it loudly rather than storing a junk role."""
    t = (jd_text or "").strip()
    return len(t) < 500 and bool(re.search(
        r"access denied|don'?t have permission|request (?:was )?blocked|pardon our interruption|"
        r"verify you are a human|are you a robot", t, re.I))


def add_role(cfg: dict, conn, company: str, title: str, jd_text: str, *,
             apply_url: str = "", location: str = "", remote: bool = False,
             job_id: str | None = None, posted_at: str | None = None) -> str:
    """Insert (or update) one hand-captured role. Returns its job_id ('manual:<co>:<slug>')."""
    if not (company and title and jd_text):
        raise ValueError("company, title and jd_text are all required")
    norm = cfgmod.norm_key(company)
    job_id = job_id or f"manual:{norm}:{_slug(title)}"
    now = store.la_now_iso()
    row = {c: None for c in store.ROLE_COLS}
    row.update(
        job_id=job_id, ats="manual", company=company, company_norm=norm,
        title=title, title_norm=cfgmod.norm_key(title),
        # "" not None: the scanner's rows always carry a string here, and a NULL crashed the
        # list template's |truncate on every Apple role the extension captured without one.
        location=location or "", is_remote=1 if remote else 0,
        apply_url=apply_url or None, jd_text=jd_text,
        jd_hash=hashlib.sha256(jd_text.encode()).hexdigest(),
        # Captured pages usually print it ("Posted: Jul 24, 2026" on Apple's board); without
        # it the freshness bonus would fall back to first_seen, which for a hand-added role
        # says when SHE found it, not when it went live.
        posted_at=posted_at or posted_day_from_jd(jd_text),
        first_seen=now, last_seen=now,
        verdict="manual", why="added by hand (unsupported ATS)",
        scored_at=now, updated_at=now,
    )
    store.upsert_roles(conn, [row])
    return job_id
