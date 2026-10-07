#!/usr/bin/env python3
"""outreach_batch.py — write every outreach draft for this workspace in one go (owner 2026-09-23).

For each contact (mailbox recruiters + the curated list's A/B tiers): find the company's open roles
that fit THIS candidate — first in JobScan's own review queue, and when the company is not
scanned, by probing its public board (Greenhouse / Lever / Ashby, the same slug guesses
tools/li_resolve.py uses) and keeping the engineering titles — then draft with the configured
model (config ai.model, opus) and store the draft for the /outreach page. Never sends anything.

    JOBSCAN_CONFIG=config.robin.yaml .venv/bin/python tools/outreach_batch.py [--limit N] [--redo] [--only EMAIL] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tools"))

from app import config as cfgmod, outreach, store  # noqa: E402
from app.sources.base import make_client  # noqa: E402
import li_resolve  # noqa: E402

_ENG = re.compile(r"software|engineer|developer|backend|back-end|full.?stack|infrastructure|platform|\bsre\b|"
                  r"reliability|distributed|data engineer|api\b|services", re.I)
_NOT = re.compile(r"\b(manager|director|vp|vice president|head of|chief|intern|internship|principal|architect|"
                  r"recruit|sales|account|marketing|designer|product manager|hardware|mechanical|electrical|"
                  r"embedded|firmware|c\+\+|rust|ios|android|security engineer|qa\b|test engineer)\b", re.I)
_NONUS = re.compile(r"london|\buk\b|united kingdom|germany|berlin|paris|france|israel|tel aviv|india|bangalore|"
                    r"canada|toronto|vancouver|singapore|australia|sydney|netherlands|amsterdam|spain|barcelona|"
                    r"poland|warsaw|ireland|dublin|japan|tokyo|brazil|mexico|estonia|tallinn|\bemea\b|\bapac\b", re.I)
CACHE = BASE / "data" / "outreach_probe_cache.json"


def probe_roles(client, company: str, cache: dict, delay: float = 0.4) -> list[dict]:
    """Open engineering roles from the company's public board (cached 24h). [] when no board."""
    key = cfgmod.norm_key(company)
    hit = cache.get(key)
    if hit and time.time() - hit.get("at", 0) < 86400:
        return hit["roles"]
    ats, slug, postings, definitive, trusted = li_resolve.find_board(client, company, delay)
    roles: list[dict] = []
    if postings and trusted:
        for p in postings:
            if not _ENG.search(p.title or "") or _NOT.search(p.title or ""):
                continue
            loc = p.location or ""
            if _NONUS.search(loc) and not re.search(r"remote|united states|\busa?\b", loc, re.I):
                continue
            roles.append({"job_id": f"probe:{ats}:{slug}:{p.raw_id}", "title": p.title, "location": loc,
                          "apply_url": li_resolve.swap_url(ats, p), "score": None,
                          "tailor_status": "none", "tailored_resume_path": None, "source": "board"})
        # Prefer mid-level titles the owner actually targets: plain / II / senior over lead-ish ones
        roles.sort(key=lambda r: (bool(re.search(r"\bstaff\b|\blead\b|\bsenior staff\b", r["title"], re.I)),
                                  not bool(re.search(r"\bII\b|\b2\b|software engineer\b", r["title"], re.I))))
        roles = roles[:5]
    cache[key] = {"at": time.time(), "ats": ats, "slug": slug, "roles": roles, "definitive": definitive}
    return roles


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--redo", action="store_true", help="regenerate even when a draft exists")
    ap.add_argument("--dry-run", action="store_true", help="match + probe only, no model calls")
    ap.add_argument("--delay", type=float, default=0.4)
    a = ap.parse_args()

    cfg = cfgmod.load_config()
    conn = store.connect_view(cfg)
    tiers = set((cfg.get("outreach") or {}).get("tiers") or ["A", "B"]) | {"mailbox"}
    contacts = [c for c in outreach.all_contacts(cfg) if c.get("tier") in tiers]
    if a.only:
        contacts = [c for c in contacts if c["email"] == a.only.strip().lower()]
    existing = {d["contact_email"]: d for d in store.outreach_list(conn)}
    todo = [c for c in contacts if a.redo or existing.get(c["email"], {}).get("status") in (None, "error")]
    if a.limit:
        todo = todo[:a.limit]
    print(f"workspace={cfgmod.workspace_name(cfg) or 'default'} contacts={len(contacts)} to draft={len(todo)} "
          f"model={(cfg.get('outreach') or {}).get('model') or (cfg.get('ai') or {}).get('model')}", flush=True)

    by = outreach.roles_by_company(conn, cfg)
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    client = make_client(cfg.get("http") or {})
    done = err = probed = 0
    for i, c in enumerate(todo, 1):
        _, roles = outreach.match_company(by, c["company"])
        briefs = [outreach.role_brief(r) for r in roles[:3]]
        src = "jobscan"
        if not briefs:
            briefs = probe_roles(client, c["company"], cache, a.delay)[:3]
            src = "board" if briefs else "none"
            probed += 1
            CACHE.write_text(json.dumps(cache, indent=1))
        print(f"[{i}/{len(todo)}] {c['company']} <{c['email']}> roles={len(briefs)} ({src})", flush=True)
        if a.dry_run:
            continue
        rec = {"contact_email": c["email"], "contact_name": c.get("name", ""), "company": c["company"],
               "source": c.get("source", ""), "roles": briefs}
        try:
            rec["resume_path"] = outreach.resume_for(cfg, roles[:3])
            rec.update(outreach.build_draft(cfg, c, briefs), status="draft", error="")
            done += 1
        except Exception as e:  # noqa: BLE001
            rec.update(status="error", error=f"{type(e).__name__}: {e}", subject="", body="")
            err += 1
            print(f"    ✗ {type(e).__name__}: {str(e)[:160]}", flush=True)
        store.outreach_upsert(conn, rec)
        time.sleep(1.0)
    print(f"done: {done} drafts, {err} errors, {probed} companies probed on public boards", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
