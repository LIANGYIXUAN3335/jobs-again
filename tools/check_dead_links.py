#!/usr/bin/env python3
"""check_dead_links.py — the "click it and the job is gone" check for roles the scanner
can't see.

prune_delisted.py covers scanner-sourced roles: the daily scan refreshes `last_seen`, so a
role whose last_seen lags its company's newest one is delisted, and that needs no network.
That signal does not exist for `li:` (LinkedIn), `cap:` (browser capture) and `manual:`
roles — nothing re-lists them — so those have to be fetched.

Classification, deliberately conservative (a false "dead" deletes a live opportunity):
  gone    — 404/410, or an ATS body that says the posting closed  → safe to prune
  blocked — 999/403/bot wall, or a login redirect                 → UNKNOWN, never pruned
  alive   — 200 and no closed-marker
Only `gone` is ever acted on, and only with --apply.

    jobscan/.venv/bin/python jobscan/tools/check_dead_links.py [--limit N] [--apply]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfgmod, store  # noqa: E402

BACKUP = "data/purged_dead_links_backup.json"

# Phrases ATSes actually render on a closed posting. Kept tight on purpose — a loose match
# ("not available") would hit cookie banners and kill live roles.
CLOSED = re.compile(
    r"no longer (accepting|available|open|active)"
    r"|position (has been )?(closed|filled)"
    r"|this (job|position|posting|opening) (is|has been) (closed|filled|removed|expired)"
    r"|job (posting )?(has )?expired"
    r"|posting is no longer"
    r"|we are no longer accepting applications"
    r"|this role is no longer",
    re.I)
BOT = re.compile(r"are you a human|unusual traffic|access denied|captcha|"
                 r"enable javascript and cookies|sign in to continue", re.I)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_SCRIPT = re.compile(r"<(script|style|template)\b[^>]*>.*?</\1>", re.I | re.S)
# Microsoft's careers page ships an i18n string table — `"Position Closed": "Not selected"` —
# on EVERY posting, open or not. Matching raw HTML flagged 9 live Microsoft roles as dead.
# Any phrase sitting inside a JSON key/value pair like that is a dictionary entry, not a
# message addressed to the reader.
_JSONISH = re.compile(r"(&#34;|\"|')[^\"'&<>]{0,60}\1\s*:\s*(&#34;|\"|')")


def visible_text(html: str) -> str:
    """Body text with scripts, styles and embedded JSON string tables removed."""
    t = _SCRIPT.sub(" ", html or "")
    # drop lines that look like JSON dictionaries (localisation blobs, __NEXT_DATA__, …)
    return "\n".join(ln for ln in t.splitlines() if not _JSONISH.search(ln))


def classify(resp) -> tuple[str, str]:
    if resp is None:
        return "blocked", "transport error"
    if resp.status_code in (404, 410):
        return "gone", f"HTTP {resp.status_code}"
    if resp.status_code in (999, 403, 429) or resp.status_code >= 500:
        return "blocked", f"HTTP {resp.status_code}"
    body = resp.text or ""
    if BOT.search(body[:6000]):
        return "blocked", "bot wall / login"
    # LinkedIn serves 200 + an empty guest shell for a pulled posting.
    if "linkedin.com" in str(resp.url) and len(body) < 1500:
        return "gone", "linkedin guest shell (empty)"
    m = CLOSED.search(visible_text(body))
    if m:
        return "gone", f"body: {m.group(0)[:48]}"
    return "alive", f"HTTP {resp.status_code}"


GUEST_POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"


def _linkedin_alive(c: httpx.Client, jid: str, delay: float) -> tuple[str, str]:
    """A pulled LinkedIn posting 404s on the guest endpoint; a live one returns its markup.

    429/999 means we're being throttled, not that the job died — back off once, then give
    up as `blocked` rather than risk deleting live roles during a rate-limit episode.
    """
    for attempt in (0, 1):
        try:
            resp = c.get(GUEST_POSTING.format(id=jid))
        except Exception:  # noqa: BLE001
            return "blocked", "transport error"
        if resp.status_code in (404, 410):
            return "gone", f"guest endpoint HTTP {resp.status_code}"
        if resp.status_code in (429, 999, 403):
            if attempt == 0:
                time.sleep(max(5.0, delay * 10))
                continue
            return "blocked", f"throttled HTTP {resp.status_code}"
        body = resp.text or ""
        if len(body.strip()) < 400:
            return "gone", "guest endpoint returned empty posting"
        if CLOSED.search(visible_text(body)):
            return "gone", "guest body: closed"
        return "alive", "guest endpoint OK"
    return "blocked", "throttled"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--apply", action="store_true", help="actually delete the `gone` roles")
    args = ap.parse_args()

    cfg = cfgmod.load_config()
    conn = store.connect_view(cfg)
    rows = [r for r in store.fetch_view(conn, cfg, status="all")["rows"]
            if r.get("in_review")
            and not r["job_id"].startswith(("gh:", "lv:", "ab:", "wd:", "av:", "rp:", "sr:"))
            and (r.get("own_status") or "new") == "new"]
    if args.limit:
        rows = rows[:args.limit]
    print(f"checking {len(rows)} non-scanner roles (LinkedIn / captured / manual)")

    out = {"gone": [], "blocked": [], "alive": []}
    with httpx.Client(timeout=20, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}) as c:
        for i, r in enumerate(rows, 1):
            url = r.get("apply_url") or ""
            if not url:
                out["blocked"].append({**{k: r[k] for k in ("job_id", "company", "title")},
                                       "why": "no apply_url"})
                continue
            time.sleep(args.delay)
            if r["job_id"].startswith("li:"):
                # www.linkedin.com/jobs/view/<id> bounces an anonymous client to a login
                # wall, so every LinkedIn role read as "blocked". The GUEST posting endpoint
                # the nightly already uses needs no session and 404s a pulled posting — same
                # no-login rule as the rest of the pipeline.
                verdict, why = _linkedin_alive(c, r["job_id"][3:], args.delay)
            else:
                try:
                    resp = c.get(url)
                except Exception:  # noqa: BLE001
                    resp = None
                verdict, why = classify(resp)
            out[verdict].append({"job_id": r["job_id"], "company": r["company"],
                                 "title": r["title"], "url": url, "why": why})
            if i % 25 == 0:
                print(f"  {i}/{len(rows)} … gone {len(out['gone'])}, "
                      f"blocked {len(out['blocked'])}, alive {len(out['alive'])}")

    print(f"\ngone {len(out['gone'])} | blocked {len(out['blocked'])} | alive {len(out['alive'])}")
    for r in out["gone"]:
        print(f"  ✗ {r['company'][:20]:20} {r['title'][:44]:44} {r['why']}")
    if out["blocked"]:
        print(f"\n  ({len(out['blocked'])} blocked — bot wall or login; left alone, "
              f"check these by hand)")

    rep = cfgmod.resolve_path("output/dead_links.json")
    rep.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nreport -> {rep}")

    if not args.apply:
        print("dry run — nothing deleted. Re-run with --apply to prune the `gone` ones.")
        conn.close()
        return

    ids = [r["job_id"] for r in out["gone"]]
    if ids:
        bak = cfgmod.resolve_path(BACKUP)
        hist = json.loads(bak.read_text()) if bak.exists() else {}
        hist[store.la_today()] = out["gone"]
        bak.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
        q = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM jobs.roles WHERE job_id IN ({q})", ids)
        conn.execute(f"DELETE FROM role_state WHERE job_id IN ({q})", ids)
        conn.commit()
        print(f"deleted {len(ids)}; backup appended to {BACKUP}")
    conn.close()


if __name__ == "__main__":
    main()
