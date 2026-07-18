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


def _slug(s: str, n: int = 44) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:n] or "role"


def add_role(cfg: dict, conn, company: str, title: str, jd_text: str, *,
             apply_url: str = "", location: str = "", remote: bool = False,
             job_id: str | None = None) -> str:
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
        location=location or None, is_remote=1 if remote else 0,
        apply_url=apply_url or None, jd_text=jd_text,
        jd_hash=hashlib.sha256(jd_text.encode()).hexdigest(),
        first_seen=now, last_seen=now,
        verdict="manual", why="added by hand (unsupported ATS)",
        scored_at=now, updated_at=now,
    )
    store.upsert_roles(conn, [row])
    return job_id
