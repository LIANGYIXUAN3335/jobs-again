#!/usr/bin/env python3
"""linkedin_alerts.py — read LinkedIn Job Alert digests from Gmail (IMAP, same app-password
channel as mailsearch.py) and extract the recommended jobs into a pending-worklist JSON.

READ-ONLY: no DB writes here. The flow (nightly via tools/linkedin_nightly.sh, launchd
08:20 — DESIGN.md §5c) is
    1. this script + tools/linkedin_search.py (guest search sweep)
                    → scratch_priors/linkedin_alerts_pending.json   (MERGED worklist)
    2. tools/linkedin_enrich.py → guest jobPosting fetch fills JD text (plain httpx with
                      a browser UA — see tools/linkedin_guest.py; paced + circuit-broken)
    3. tools/linkedin_ingest.py → manual.add_role(job_id="li:<id>") + rescore

Usage:
    jobscan/.venv/bin/python jobscan/tools/linkedin_alerts.py            # last 7 days
    jobscan/.venv/bin/python jobscan/tools/linkedin_alerts.py --days 14

Dedup: skips job ids already in jobscan.db (li:<id>) AND ids listed in
scratch_priors/linkedin_alert_seen.json (appended on every run), so re-runs are incremental.
"""
from __future__ import annotations

import argparse
import email
import email.utils
import imaplib
import json
import re
import sys
from email.header import decode_header, make_header
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent.parent          # jobscan/
sys.path.insert(0, str(BASE))

from app import config as cfgmod, mail as _mail, store  # noqa: E402

PENDING = "scratch_priors/linkedin_alerts_pending.json"
SEEN = "scratch_priors/linkedin_alert_seen.json"
FROM_ADDR = "sample-e9b38a32@example.com"

_VIEW = re.compile(r"https://www\.linkedin\.com/(?:comm/)?jobs/view/(\d+)")
# lines that sit between a job's title/company/location and its "View job:" link but carry
# no role identity — skipped when walking backwards from the link
_NOISE = re.compile(
    r"^\d+\s+connections?$|^view job|actively (?:hiring|recruiting)|this company|"
    r"^\$|salary|/year|^easy apply|^promoted|^new$|alumni|work here|^\W*$", re.I)


def _creds():
    sec = yaml.safe_load((BASE / "secrets.yaml").read_text(encoding="utf-8")) or {}
    pw = (sec.get("gmail_app_password") or "").strip()
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
    addr = ((cfg.get("mailcode") or {}).get("email")
            or (cfg.get("profile") or {}).get("email"))
    if not (pw and addr):
        sys.exit("secrets.yaml 缺 gmail_app_password, 或 config.yaml 缺邮箱地址")
    return addr, pw


def _text_of(msg) -> str:
    """Prefer the text/plain part; fall back to crudely de-tagged HTML."""
    plain, html = "", ""
    for part in msg.walk():
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True).decode(
                part.get_content_charset() or "utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        if ct == "text/plain" and not plain:
            plain = payload
        elif ct == "text/html" and not html:
            html = payload
    if plain:
        return plain
    return re.sub(r"<[^>]+>", "\n", html)


def _jobs_from_text(text: str) -> list[dict]:
    """Walk each 'View job' link; the informative lines directly above it are
    [title, company, location] (noise lines skipped)."""
    lines = [ln.strip() for ln in text.splitlines()]
    jobs = []
    for i, ln in enumerate(lines):
        m = _VIEW.search(ln)
        if not m:
            continue
        info: list[str] = []
        j = i - 1
        while j >= 0 and len(info) < 3:
            cand = lines[j]
            j -= 1
            if not cand or _NOISE.search(cand) or _VIEW.search(cand):
                continue
            info.append(cand)
        info.reverse()                       # [title, company, location] best-effort
        jobs.append({
            "id": m.group(1),
            "title": info[0] if len(info) > 0 else "",
            "company": info[1] if len(info) > 1 else "",
            "location": info[2] if len(info) > 2 else "",
            "url": f"https://www.linkedin.com/jobs/view/{m.group(1)}/",
        })
    return jobs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    addr, pw = _creds()
    cfg = cfgmod.load_config()
    conn = store.connect_jobs(cfg)
    in_db = {r["job_id"][3:] for r in conn.execute(
        "SELECT job_id FROM roles WHERE job_id LIKE 'li:%'")}
    seen_path = cfgmod.resolve_path(SEEN)
    try:
        seen = set(json.loads(seen_path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        seen = set()

    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    _mail.imap_login(imap, addr, pw)
    imap.select("INBOX", readonly=True)
    import datetime
    since = (datetime.date.today() - datetime.timedelta(days=args.days)).strftime("%d-%b-%Y")
    _, data = imap.search(None, f'(FROM "{FROM_ADDR}" SINCE {since})')
    uids = data[0].split()
    print(f"{len(uids)} alert emails since {since}")

    found: dict[str, dict] = {}
    for uid in uids:
        _, msg_data = imap.fetch(uid, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])
        subj = str(make_header(decode_header(msg.get("Subject") or "")))
        date = email.utils.parsedate_to_datetime(msg.get("Date")).date().isoformat()
        for job in _jobs_from_text(_text_of(msg)):
            j = found.setdefault(job["id"], {**job, "email_date": date, "subject": subj})
            # keep the richest parse if the same job appears in several digests
            if not j["title"] and job["title"]:
                j.update(title=job["title"], company=job["company"], location=job["location"])
    imap.logout()

    fresh = [j for jid, j in found.items() if jid not in in_db and jid not in seen]
    print(f"jobs in alerts: {len(found)} | new (not in DB/seen): {len(fresh)}")
    for j in fresh:
        print(f"  {j['id']}  {j['title'][:44]:46} | {j['company'][:20]:20} | {j['email_date']}")

    # MERGE into pending, don't overwrite: not-yet-enriched leftovers are already in `seen`,
    # so overwriting with `fresh` alone would silently drop them from the worklist.
    pending_path = cfgmod.resolve_path(PENDING)
    try:
        prev = json.loads(pending_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        prev = []
    fresh_ids = {j["id"] for j in fresh}
    keep = [j for j in prev
            if str(j.get("id")) not in in_db and str(j.get("id")) not in fresh_ids]
    merged = keep + fresh
    pending_path.write_text(
        json.dumps(merged, indent=1, ensure_ascii=False), encoding="utf-8")
    seen |= set(found)
    seen_path.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    print(f"pending -> {PENDING} ({len(merged)} total: {len(keep)} carried + {len(fresh)} new)")


if __name__ == "__main__":
    main()
