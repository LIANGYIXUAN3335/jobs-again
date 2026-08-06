#!/usr/bin/env python3
"""linkedin_search.py — 方案 B: guest search sweep for discovery beyond the alert emails.

Runs each search group from config.yaml `linkedin.searches` against the guest
seeMoreJobPostings endpoint (no login), windowed to the last `tpr_seconds` (default 24h),
and merges NEW job ids into the same pending worklist the alert reader feeds
(scratch_priors/linkedin_alerts_pending.json), sharing the seen-set and DB dedupe.
linkedin_enrich.py then fills the JDs. Flow overview in linkedin_alerts.py.

Usage:
    jobscan/.venv/bin/python jobscan/tools/linkedin_search.py
    jobscan/.venv/bin/python jobscan/tools/linkedin_search.py --tpr 604800   # one-off 7d sweep
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

BASE = Path(__file__).resolve().parent.parent          # jobscan/
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import linkedin_guest as lg  # noqa: E402
from app import config as cfgmod, store  # noqa: E402

PENDING = "scratch_priors/linkedin_alerts_pending.json"
SEEN = "scratch_priors/linkedin_alert_seen.json"
BREAKER = 2          # consecutive blocked queries before giving up for tonight


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tpr", type=int, default=None,
                    help="posting-age window in seconds (default from config, 86400)")
    args = ap.parse_args()

    cfg = cfgmod.load_config()
    li = cfg.get("linkedin") or {}
    if not li.get("enabled", True):
        print("linkedin.enabled is false — skipping")
        return
    searches = li.get("searches") or []
    if not searches:
        print("no linkedin.searches configured — nothing to sweep")
        return
    delay = float(li.get("delay_seconds", 5))
    tpr = args.tpr if args.tpr is not None else int(li.get("tpr_seconds", 86400))
    pages = int(li.get("search_pages", 1))

    conn = store.connect_jobs(cfg)
    in_db = {r["job_id"][3:] for r in conn.execute(
        "SELECT job_id FROM roles WHERE job_id LIKE 'li:%'")}
    seen_path = cfgmod.resolve_path(SEEN)
    try:
        seen = set(json.loads(seen_path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        seen = set()
    pending_path = cfgmod.resolve_path(PENDING)
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pending = []
    in_pending = {str(j.get("id")) for j in pending}

    client = lg.make_client()
    today = datetime.date.today().isoformat()
    fresh: list[dict] = []
    consecutive_blocked = 0
    for group in searches:
        if consecutive_blocked >= BREAKER:
            print(f"CIRCUIT BREAK: {BREAKER} consecutive blocked queries — stopping sweep")
            break
        label = f"{group.get('keywords', '?')} × {group.get('location', '?')}" \
                + (" × remote" if group.get("remote") else "")
        cards_total, new_here = 0, 0
        for page in range(pages):
            params = {
                "keywords": str(group.get("keywords", "")),
                "location": str(group.get("location", "")),
                "f_TPR": f"r{tpr}",
                "start": page * 10,
            }
            if group.get("remote"):
                params["f_WT"] = "2"
            lg.polite_pause(delay)
            try:
                resp = client.get(f"{lg.SEARCH_URL}?{urlencode(params)}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! [{label}] transport error: {exc}")
                resp = None
            # an exhausted result window returns 400 — that's fine, just stop paging
            if resp is not None and resp.status_code == 400 and page > 0:
                break
            if lg.blocked(resp):
                code = resp.status_code if resp is not None else "n/a"
                print(f"  ! [{label}] blocked (HTTP {code})")
                consecutive_blocked += 1
                break
            consecutive_blocked = 0
            cards = lg.parse_search(resp.text)
            cards_total += len(cards)
            for c in cards:
                if c["id"] in in_db or c["id"] in seen or c["id"] in in_pending:
                    continue
                fresh.append({**c, "found_date": today, "source": "search"})
                in_pending.add(c["id"])
                seen.add(c["id"])
                new_here += 1
            if len(cards) < 10:      # short page = end of results
                break
        print(f"  [{label}] {cards_total} cards, {new_here} new")

    pending.extend(fresh)
    pending_path.write_text(json.dumps(pending, indent=1, ensure_ascii=False), encoding="utf-8")
    seen_path.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    print(f"search sweep: {len(fresh)} new jobs -> pending now {len(pending)}")


if __name__ == "__main__":
    main()
