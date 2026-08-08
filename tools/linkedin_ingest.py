#!/usr/bin/env python3
"""linkedin_ingest.py — step 3 of the LinkedIn-alert pipeline (see linkedin_alerts.py).

Reads the enriched per-job JSON files (written by Claude agents from guest-page fetches),
inserts every status="ok" job as a role with job_id "li:<id>" via manual.add_role, then
rescores the whole DB so they get real weighted_score/verdict (clearance/non-eng/template
checks included).

    jobscan/.venv/bin/python jobscan/tools/linkedin_ingest.py <enriched_dir>

Skips: status gone/authwall, jobs already in DB, bot-blocked JD text (manual.looks_blocked),
and jd_text shorter than 300 chars (nothing real to score against).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfgmod, filters, manual, store  # noqa: E402
from app.sources.base import Posting  # noqa: E402


def main(enriched_dir: str) -> None:
    cfg = cfgmod.load_config()
    conn = store.connect_jobs(cfg)
    in_db = {r["job_id"] for r in conn.execute("SELECT job_id FROM roles WHERE job_id LIKE 'li:%'")}

    added, skipped = [], []
    for f in sorted(Path(enriched_dir).glob("*.json")):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            skipped.append((f.name, f"bad json: {e}")); continue
        jid = f"li:{j.get('id') or f.stem}"
        if jid in in_db:
            skipped.append((jid, "already in DB")); continue
        status = j.get("status")
        if status != "ok":
            skipped.append((jid, f"status={status} ({(j.get('company') or '?')[:20]})")); continue
        jd = (j.get("jd_text") or "").strip()
        title, company = (j.get("title") or "").strip(), (j.get("company") or "").strip()
        if not (title and company):
            skipped.append((jid, "missing title/company")); continue
        if manual.looks_blocked(jd) or len(jd) < 300:
            skipped.append((jid, f"jd too thin ({len(jd)} chars)")); continue
        # Run the SAME hard filters the ATS scanners run. This path bypassed them entirely,
        # which is how a TikTok posting (current employer) plus ML/frontend/new-grad roles
        # the config explicitly excludes all reached the review queue — ~88 LinkedIn roles
        # land here every night, so an unfiltered channel quietly dominates the queue.
        res = filters.classify(
            Posting(ats="linkedin", company=company, slug="", raw_id=jid, title=title,
                    location=j.get("location") or "", jd_text=jd,
                    apply_url="", department=""),
            cfg.get("filters") or {})
        if not res.keep:
            skipped.append((jid, f"filtered {res.reason} ({company[:20]})")); continue
        if j.get("salary"):
            jd += f"\n\nSalary: {j['salary']}"
        manual.add_role(
            cfg, conn, company, title, jd,
            apply_url=f"https://www.linkedin.com/jobs/view/{j.get('id') or f.stem}/",
            location=j.get("location") or "",
            job_id=jid, posted_at=j.get("posted_date") or None)
        added.append((jid, company, title))

    print(f"added {len(added)}, skipped {len(skipped)}")
    for jid, co, t in added:
        print(f"  + {jid}  {co[:22]:22} {t[:48]}")
    for jid, why in skipped:
        print(f"  - {jid}  {why}")

    # Rescore if we added anything OR a previous run's rescore failed and left li: roles
    # unscored (add_role commits with weighted_score NULL before rescore runs) — without
    # this, a crashed rescore would leave them unscored forever: the next run skips them
    # as "already in DB" and `added` stays empty.
    unscored = conn.execute("SELECT COUNT(*) FROM roles WHERE job_id LIKE 'li:%' "
                            "AND weighted_score IS NULL").fetchone()[0]
    if added or unscored:
        print(f"\nrescoring … ({len(added)} new, {unscored} previously unscored)")
        subprocess.run([str(BASE / ".venv/bin/python"), str(BASE / "tools/rescore.py")],
                       cwd=BASE, check=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: linkedin_ingest.py <enriched_dir>")
    main(sys.argv[1])
