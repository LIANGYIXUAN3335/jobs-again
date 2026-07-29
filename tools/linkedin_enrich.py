#!/usr/bin/env python3
"""linkedin_enrich.py — step 2 of the LinkedIn pipeline (flow overview in linkedin_alerts.py).

Reads the pending worklist (scratch_priors/linkedin_alerts_pending.json — written by
linkedin_alerts.py and linkedin_search.py), fetches each job's guest posting page to fill
JD text, and writes one enriched JSON per job into scratch_priors/linkedin_enriched/ for
linkedin_ingest.py. Pure guest fetches — no login, no LLM.

Politeness / self-defence:
  * delay_seconds (+ jitter) between requests, enrich_cap per run
  * 3 consecutive blocked responses → circuit-break, leftovers stay pending for tomorrow
  * a job blocked max_attempts times, or older than max_age_days, is dropped (logged)

Pending item shape: {id, title, company, location, url, email_date|found_date, attempts?}

Usage:
    jobscan/.venv/bin/python jobscan/tools/linkedin_enrich.py                  # config caps
    jobscan/.venv/bin/python jobscan/tools/linkedin_enrich.py --cap 90 --max-age-days 14
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # jobscan/
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import linkedin_guest as lg  # noqa: E402
from app import config as cfgmod, store  # noqa: E402

PENDING = "scratch_priors/linkedin_alerts_pending.json"
ENRICHED_DIR = "scratch_priors/linkedin_enriched"
BREAKER = 3          # consecutive blocked responses before giving up for tonight


def _item_date(item: dict) -> str:
    return item.get("email_date") or item.get("found_date") or ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=None, help="max detail fetches this run")
    ap.add_argument("--max-age-days", type=int, default=None,
                    help="drop pending items older than this (default from config, 21)")
    args = ap.parse_args()

    cfg = cfgmod.load_config()
    li = cfg.get("linkedin") or {}
    if not li.get("enabled", True):
        print("linkedin.enabled is false — skipping")
        return
    delay = float(li.get("delay_seconds", 5))
    cap = args.cap if args.cap is not None else int(li.get("enrich_cap", 60))
    max_attempts = int(li.get("max_attempts", 3))
    max_age = args.max_age_days if args.max_age_days is not None else int(li.get("max_age_days", 21))

    pending_path = cfgmod.resolve_path(PENDING)
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pending = []
    if not pending:
        print("pending worklist empty — nothing to enrich")
        return

    out_dir = cfgmod.resolve_path(ENRICHED_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = store.connect_jobs(cfg)
    in_db = {r["job_id"][3:] for r in conn.execute(
        "SELECT job_id FROM roles WHERE job_id LIKE 'li:%'")}

    cutoff = (datetime.date.today() - datetime.timedelta(days=max_age)).isoformat()
    work, keep, dropped = [], [], []
    for item in pending:
        jid = str(item.get("id") or "")
        if not jid:
            continue
        if jid in in_db:
            dropped.append((jid, "already in DB"))
        elif _item_date(item) and _item_date(item) < cutoff:
            dropped.append((jid, f"older than {max_age}d ({_item_date(item)})"))
        elif int(item.get("attempts", 0)) >= max_attempts:
            dropped.append((jid, f"gave up after {item.get('attempts')} blocked attempts"))
        else:
            work.append(item)

    print(f"pending {len(pending)} -> workable {len(work)}, dropped {len(dropped)}, cap {cap}")
    for jid, why in dropped:
        print(f"  - {jid}  {why}")

    client = lg.make_client()
    consecutive_blocked = 0
    ok = gone = blocked_n = empty = 0
    charged_thin: list[dict] = []      # items charged an attempt for a thin parse this run
    for i, item in enumerate(work):
        if i >= cap:
            keep.extend(work[i:])
            print(f"cap {cap} reached — {len(work) - i} left pending for next run")
            break
        if consecutive_blocked >= BREAKER:
            keep.extend(work[i:])
            print(f"CIRCUIT BREAK: {BREAKER} consecutive blocked responses — "
                  f"stopping, {len(work) - i} left pending. Alerts-only mode still works.")
            break
        jid = str(item["id"])
        lg.polite_pause(delay)
        try:
            resp = client.get(lg.POSTING_URL.format(id=jid))
        except Exception as exc:  # noqa: BLE001 — transport error counts as blocked
            print(f"  ? {jid}  transport error: {exc}")
            resp = None
        if resp is not None and resp.status_code in (404, 410):
            (out_dir / f"{jid}.json").write_text(json.dumps(
                {"id": jid, "status": "gone", **{k: item.get(k, "") for k in
                 ("title", "company", "location")}}, ensure_ascii=False), encoding="utf-8")
            gone += 1
            consecutive_blocked = 0
            continue
        if lg.blocked(resp):
            # IP-level signal (throttle / machine offline), not a verdict on THIS job —
            # keep it pending with NO attempts charge, else a sustained outage burns the
            # same head-of-queue items run after run. max_age_days is the backstop.
            keep.append(item)
            blocked_n += 1
            consecutive_blocked += 1
            code = resp.status_code if resp is not None else "n/a"
            print(f"  ! {jid}  blocked (HTTP {code}) — kept pending, no attempt charged")
            continue
        consecutive_blocked = 0
        parsed = lg.parse_posting(resp.text)
        title = parsed["title"] or item.get("title", "")
        company = parsed["company"] or item.get("company", "")
        # Mirror ingest's acceptance gate (jd >= 300 chars, title+company present): anything
        # ingest would reject as thin must RETRY here, not ship as status "ok" — the ok path
        # removes the item from pending and seen.json blocks rediscovery, so a thin "ok"
        # (e.g. the JD selector drifts while the title selector still matches) would be a
        # silent permanent loss.
        if len(parsed["jd_text"]) < 300 or not title or not company:
            item["attempts"] = int(item.get("attempts", 0)) + 1
            charged_thin.append(item)
            keep.append(item)
            empty += 1
            print(f"  ? {jid}  thin/empty parse (jd {len(parsed['jd_text'])} ch, "
                  f"attempt {item['attempts']}/{max_attempts})")
            continue
        enriched = {
            "id": jid, "status": "ok",
            "title": title,
            "company": company,
            "location": parsed["location"] or item.get("location", ""),
            "jd_text": parsed["jd_text"],
            "salary": parsed["salary"],
            "posted_date": parsed["posted_date"] or item.get("posted_date"),
            "url": item.get("url") or f"https://www.linkedin.com/jobs/view/{jid}/",
        }
        (out_dir / f"{jid}.json").write_text(
            json.dumps(enriched, indent=1, ensure_ascii=False), encoding="utf-8")
        ok += 1
        print(f"  + {jid}  {enriched['company'][:22]:22} {enriched['title'][:44]:44} "
              f"jd {len(enriched['jd_text'])} ch")

    if ok == 0 and empty >= 3:
        # EVERY parse thin and none ok = markup drift, not bad jobs — a run-level failure.
        # Roll back this run's attempt charges so drift can't expire the whole worklist.
        for item in charged_thin:
            item["attempts"] = max(0, int(item.get("attempts", 1)) - 1)
        print("WARNING: every parse came back thin/empty — likely LinkedIn markup drift; "
              "this run's attempt charges rolled back. Check tools/linkedin_guest.py selectors")
    elif empty and empty >= ok:
        print("WARNING: parses came back mostly thin/empty — LinkedIn markup may have "
              "changed; check tools/linkedin_guest.py selectors")

    pending_path.write_text(json.dumps(keep, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"enriched ok {ok}, gone {gone}, blocked {blocked_n}, thin/empty {empty}; "
          f"pending now {len(keep)} -> {PENDING}")


if __name__ == "__main__":
    main()
