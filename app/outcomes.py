"""Match inbox signals to application records."""
from __future__ import annotations

import argparse
import email
import imaplib
import re
import sys
from datetime import datetime, timedelta, timezone

from . import applied_scan
from . import config as cfgmod
from . import mail, store

# --- what an email means. Order matters: the first match wins, most decisive first. ---
_SIGNALS: list[tuple[str, str, str, re.Pattern]] = [
    # (name, stage, outcome, pattern)
    ("offer", "offer", "offer", re.compile(
        r"offer letter|(?:pleased|delighted|excited) to (?:extend|offer)|"
        r"we(?:'d| would) like to (?:extend|make) (?:you )?an offer|your offer\b", re.I)),
    ("rejection", "", "rejected", re.compile(
        r"not (?:be )?(?:moving|move|proceeding|progressing) forward|regret to inform|"
        r"unfortunately,? (?:we|after|your)|decided (?:to move|not to)|"
        r"pursuing other candidates|(?:will|are) not (?:be )?(?:moving|proceeding)|"
        r"no longer under consideration|won'?t be moving forward|"
        r"decided to move forward with other|not (?:be )?selected|"
        r"filled (?:this|the) (?:role|position)", re.I)),
    ("onsite", "onsite", "", re.compile(
        r"onsite interview|on-site interview|final round|virtual onsite|"
        r"interview loop|full loop|panel interview", re.I)),
    ("interview", "interview", "", re.compile(
        r"schedule (?:a|an|your) (?:call|chat|interview|conversation|time)|"
        r"phone screen|technical (?:screen|interview|phone)|"
        r"(?:invite|invitation|inviting) (?:you )?to (?:an? )?interview|"
        r"interview (?:invitation|invite|request)|"
        r"like to (?:set up|schedule|arrange)|meet (?:with )?the team|"
        r"next steps? in (?:the|your) (?:process|interview)|"
        r"your (?:interview|availability) (?:is|for)|recruiter (?:call|screen)", re.I)),
    ("assessment", "screen", "", re.compile(
        r"online assessment|coding (?:challenge|assessment|exercise|test)|take[- ]home|"
        r"hackerrank|codesignal|coderpad|\bkarat\b|codility|technical assessment|"
        r"skills? (?:assessment|challenge)", re.I)),
]

# Never counts as a response, however human it looks: the ATS auto-acknowledgement, and the
# account-plumbing mail that mentions an assessment vendor without being an invite ("You shared
# some Google Account data with CodeSignal" is an OAuth consent receipt, not a screen).
_NOT_A_RESPONSE = re.compile(
    r"do not reply|automated (?:message|response)|application (?:was |has been )?received|"
    r"you (?:shared|granted)|account data with|signed in to|access to your (?:google|account)", re.I)


def available(cfg: dict) -> bool:
    return applied_scan.available(cfg)


def _classify(subject: str, body: str) -> tuple[str, str, str] | None:
    """(signal, stage, outcome) or None. Subject first — bodies quote whole threads and a
    rejection quoted inside a later email must not re-classify that email."""
    for name, stage, outcome, pat in _SIGNALS:
        if pat.search(subject):
            return name, stage, outcome
    head = (body or "")[:1500]                 # first screenful only, before quoted history
    for name, stage, outcome, pat in _SIGNALS:
        if pat.search(head):
            return name, stage, outcome
    return None


def _open_roles(conn) -> dict[str, list[dict]]:
    """Applications that could still receive news, grouped by normalized company."""
    rows = conn.execute(
        "SELECT r.job_id, r.company, r.company_norm, r.title, r.weighted_score,"
        "       s.status, s.applied_date, s.stage, s.outcome"
        " FROM jobs.roles r JOIN role_state s ON s.job_id = r.job_id"
        " WHERE s.status IN ('applied','interviewing')"
        "   AND (s.outcome IS NULL OR s.outcome IN ('in_progress'))").fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["company_norm"], []).append(dict(r))
    return out


def _match(company_hint: str, title_hint: str, subject: str, by_co: dict) -> list[dict]:
    cn = applied_scan._norm(company_hint)
    if not cn or len(cn) < 3:
        return []
    hits = [r for co, rs in by_co.items() if (cn in co or co in cn) for r in rs]
    if title_hint and len(hits) > 1:
        tn = applied_scan._norm(title_hint)
        titled = [r for r in hits if tn and (tn in applied_scan._norm(r["title"])
                                             or applied_scan._norm(r["title"]) in tn)]
        if titled:
            hits = titled
    # Subjects carry the role title in a dozen shapes ("Your application - Senior SWE, Streaming
    # Platform"), too many to parse. Reading it the other way round is exact and shape-free: ask
    # which of HER open titles at this company appears in the subject. Longest wins, so
    # "Streaming Platform" doesn't lose to "Streaming Platform Client".
    if len(hits) > 1:
        sn = applied_scan._norm(subject)
        named = [r for r in hits if len(applied_scan._norm(r["title"])) >= 8
                 and applied_scan._norm(r["title"]) in sn]
        if named:
            hits = sorted(named, key=lambda r: -len(r["title"]))[:1]
    hits.sort(key=lambda r: -(r["weighted_score"] or 0))
    return hits


def scan(cfg: dict, days: int = 120, max_emails: int = 600) -> dict:
    """Return {proposals: [...], scanned, matched}. Nothing is written."""
    mc, addr, pw = mail._creds(cfg)
    M = imaplib.IMAP4_SSL(mc.get("imap_host", "imap.gmail.com"))
    M.login(addr, pw)
    conn = store.connect_view(cfg)
    try:
        M.select("INBOX", readonly=True)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f"(SINCE {since})")
        uids = (data[0].split() if data and data[0] else [])[-max_emails:]

        by_co = _open_roles(conn)
        proposals, seen, scanned = [], set(), 0

        for uid in reversed(uids):                                  # newest first
            typ, md = M.fetch(uid, "(RFC822.HEADER)")
            if typ != "OK" or not md or not md[0]:
                continue
            hdr = email.message_from_bytes(md[0][1])
            subject = applied_scan._decode(hdr.get("Subject"))
            sender = applied_scan._decode(hdr.get("From"))
            date = hdr.get("Date")
            scanned += 1
            if applied_scan._NOT_CONFIRM.search(f"{subject} {sender}"):
                continue
            if applied_scan._CONFIRM.search(subject):               # "thanks for applying"
                continue

            body = ""
            hit = _classify(subject, "")
            if not hit:                                             # subject was inconclusive
                typ2, bd = M.fetch(uid, "(RFC822)")
                if typ2 == "OK" and bd and bd[0]:
                    body = applied_scan._body_text(email.message_from_bytes(bd[0][1]))
                if not body or applied_scan._CONFIRM.search(body):
                    continue
                hit = _classify(subject, body)
            if not hit:
                continue
            signal, stage, outcome = hit
            if signal != "rejection" and _NOT_A_RESPONSE.search(f"{subject} {body[:800]}"):
                continue                                            # an auto-ack isn't a reply

            dom = (re.search(r"@([\w.-]+)", sender) or [None, ""])[1]
            disp = re.sub(r"<.*?>", "", sender).strip().strip('"')
            m = applied_scan._TITLE_IN_SUBJECT.search(subject)
            title_hint = (m.group(1).strip() if m else "")
            hits = (_match(disp, title_hint, subject, by_co)
                    or _match(dom.split(".")[0], title_hint, subject, by_co))
            if not hits:
                continue

            key = (hits[0]["company"], signal, subject[:40])
            if key in seen:
                continue
            seen.add(key)
            when = _date_of(date)
            proposals.append({
                "signal": signal, "stage": stage, "outcome": outcome,
                "date": when, "subject": subject[:130], "from": (disp or dom)[:60],
                "company": hits[0]["company"],
                "confidence": "high" if len(hits) == 1 else ("medium" if title_hint and len(hits) <= 2 else "low"),
                "roles": [{"job_id": r["job_id"], "title": r["title"], "status": r["status"],
                           "stage": r["stage"], "applied_date": r["applied_date"]} for r in hits[:5]],
            })
        return {"scanned": scanned, "matched": len(proposals), "proposals": proposals}
    finally:
        conn.close()
        try:
            M.logout()
        except Exception:
            pass


def _date_of(raw: str | None) -> str:
    try:
        return email.utils.parsedate_to_datetime(raw).astimezone(store.LA).date().isoformat()
    except Exception:
        return store.la_today()


def apply_proposal(cfg: dict, conn, proposal: dict, job_id: str) -> dict:
    """Commit ONE proposal against ONE chosen role. Idempotent via record_signal's monotonicity."""
    note = f"{proposal['signal']}: {proposal['subject']}"
    return store.record_signal(
        conn, job_id,
        stage=proposal["stage"] or ("responded" if proposal["signal"] != "rejection" else None),
        outcome=proposal["outcome"] or None,
        note=note, when=proposal["date"])


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--apply", action="store_true", help="commit the high-confidence proposals")
    args = ap.parse_args(argv)

    cfg = cfgmod.load_config()
    if not available(cfg):
        print("mailbox not configured (mailcode.email + gmail_app_password in secrets.yaml)")
        return 1
    res = scan(cfg, days=args.days)
    icon = {"offer": "🎉", "rejection": "❌", "onsite": "🏢", "interview": "📞", "assessment": "📝"}
    conf_order = {"high": 0, "medium": 1, "low": 2}
    props = sorted(res["proposals"], key=lambda p: (conf_order[p["confidence"]], p["date"]))
    print(f"scanned {res['scanned']} emails over {args.days}d → {res['matched']} outcome signals\n")
    for p in props:
        print(f"{icon.get(p['signal'], '·')} [{p['confidence']:6}] {p['date']}  {p['company']:18} "
              f"{p['signal']:10} {p['subject'][:60]}")
        for r in p["roles"]:
            print(f"        {r['job_id']:34} {r['title'][:52]}")
    if not args.apply:
        n_high = sum(1 for p in props if p["confidence"] == "high")
        print(f"\nnothing written. `--apply` would commit the {n_high} high-confidence ones; "
              f"the rest need a pick.")
        return 0

    conn = store.connect_view(cfg)
    try:
        n = 0
        for p in props:
            if p["confidence"] != "high":
                continue
            apply_proposal(cfg, conn, p, p["roles"][0]["job_id"])
            n += 1
        print(f"\ncommitted {n} high-confidence signals")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
