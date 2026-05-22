"""Daily scanner: fetch → hard-filter → heuristic-score → upsert. Idempotent.
Usage: python -m app.scan [--companies stripe,ramp]"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter

from . import config as cfgmod
from . import scoring, store
from .filters import classify
from .sources import FETCHERS
from .sources.base import make_client


def run_scan(cfg: dict, only: set[str] | None = None, mode: str = "local") -> dict:
    conn = store.connect_jobs(cfg)
    notes_all = cfgmod.load_company_notes(cfg)
    http_cfg = cfg.get("http") or {}
    client = make_client(http_cfg)
    delay = float(http_cfg.get("delay_seconds", 1.5))
    retries = int(http_cfg.get("retries", 2))

    today, now = store.la_today(), store.la_now_iso()
    existing = store.get_existing(conn)
    cur_hash = scoring.config_hash(cfg)
    rescore_all = store.meta_get(conn, "scoring_config_hash") != cur_hash
    if rescore_all and existing:
        print("scoring config changed — rescoring everything")

    details: dict[str, dict] = {}
    totals = Counter()
    rows_out: list[dict] = []

    cooldowns = cfgmod.active_cooldowns(cfg)
    for c in cfg.get("companies", []):
        name, ats, slug = c["name"], c["ats"], str(c.get("slug", ""))
        if only and slug.lower() not in only and name.lower() not in only:
            continue
        cd = cooldowns.get(cfgmod.norm_key(name))
        if cd:
            details[name] = {"skipped": f"cooldown until {cd['until']} ({cd['reason']})"}
            print(f"{name:<16} skipped — cooldown until {cd['until']}")
            continue
        if ats not in FETCHERS:
            details[name] = {"skipped": str(c.get("note", ats))}
            continue
        try:
            postings = FETCHERS[ats](client, name, slug, retries=retries)
        except Exception as exc:  # noqa: BLE001 — one company must not kill the run
            details[name] = {"error": f"{type(exc).__name__}: {exc}"}
            totals["errors"] += 1
            print(f"ERROR {name}: {exc}", file=sys.stderr)
            time.sleep(delay)
            continue

        drop_reasons: Counter = Counter()
        kept = []
        for p in postings:
            fr = classify(p, cfg.get("filters") or {})
            if fr.keep:
                kept.append((p, fr.seniority))
            else:
                drop_reasons[fr.reason.split(":")[0]] += 1

        comp_notes = scoring.notes_for(name, notes_all)
        n_new = 0
        n_jd_fetched = 0
        for p, seniority in kept:
            prev = existing.get(p.job_id)
            # Lazy-JD boards (Workday/Rippling/SmartRecruiters): the list endpoint has no
            # JD, so fetch the detail only for postings that survived the filters — and
            # reuse the stored JD for roles we already have, so daily scans stay cheap.
            if not p.jd_text and p.fetch_jd is not None:
                if prev is not None and prev["jd_text"]:
                    p.jd_text = prev["jd_text"]
                else:
                    try:
                        p.jd_text = p.fetch_jd()
                        n_jd_fetched += 1
                    except Exception as exc:  # noqa: BLE001 — score on title alone
                        print(f"  jd fetch failed {p.job_id}: {exc}", file=sys.stderr)
            h = scoring.jd_hash(p.jd_text)
            if prev is not None and not rescore_all and prev["jd_hash"] == h:
                sc = {k: prev[k] for k in (
                    "s_stability", "s_culture", "s_depth", "s_wlb",
                    "weighted_score", "verdict", "why", "caution", "scored_at")}
            else:
                s = scoring.score(p, comp_notes, cfg)
                sc = {
                    "s_stability": s.s_stability, "s_culture": s.s_culture,
                    "s_depth": s.s_depth, "s_wlb": s.s_wlb,
                    "weighted_score": s.weighted, "verdict": s.verdict,
                    "why": s.why, "caution": s.caution, "scored_at": now,
                }
            if prev is None:
                n_new += 1
            rows_out.append({
                "job_id": p.job_id, "ats": p.ats, "company": p.company,
                "company_norm": cfgmod.norm_key(p.company),
                "title": p.title, "title_norm": cfgmod.norm_title(p.title),
                "department": p.department, "location": p.location,
                "is_remote": int(p.is_remote), "seniority": seniority,
                "apply_url": p.apply_url, "jd_text": p.jd_text, "jd_hash": h,
                "first_seen": today, "last_seen": today, "updated_at": now,
                **sc,
            })

        details[name] = {
            "fetched": len(postings), "kept": len(kept), "new": n_new,
            "dropped": dict(drop_reasons),
        }
        if n_jd_fetched:
            details[name]["jd_fetched"] = n_jd_fetched
        totals["fetched"] += len(postings)
        totals["kept"] += len(kept)
        totals["new"] += n_new
        print(f"{name:<16} fetched {len(postings):>4} → kept {len(kept):>3} (new {n_new})")
        time.sleep(delay)

    store.upsert_roles(conn, rows_out)
    store.meta_set(conn, "scoring_config_hash", cur_hash)
    store.record_run(
        conn, mode=mode, n_fetched=totals["fetched"], n_kept=totals["kept"],
        n_new=totals["new"], n_errors=totals["errors"], details=details,
    )
    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    print(f"\nscan done: fetched {totals['fetched']}, kept {totals['kept']}, "
          f"new {totals['new']}, errors {totals['errors']}")
    return dict(totals)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.scan", description=__doc__)
    ap.add_argument("--companies", default="",
                    help="comma-separated slugs or names to scan (default: all)")
    args = ap.parse_args(argv)
    only = {s.strip().lower() for s in args.companies.split(",") if s.strip()} or None
    cfg = cfgmod.load_config()
    totals = run_scan(cfg, only=only)
    sys.exit(0 if totals.get("errors", 0) < len(cfg.get("companies", [])) else 1)


if __name__ == "__main__":
    main()
