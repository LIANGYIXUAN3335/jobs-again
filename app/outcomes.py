"""Match inbox signals to application records."""
from __future__ import annotations

import argparse
import email
import imaplib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

# --- the company-level layer -----------------------------------------------------------------
#
# The patterns below are the same ones `app/company_board` and `tools/funnel_report` use to READ
# the mailbox; they live here because this is the module that WRITES, and an unattended writer
# must not depend on a display module (company_board imports this file, not the other way
# round). They are duplicated on purpose and the duplication is the bug risk — the two readers
# should collapse onto these once their owners are free (noted 2026-08-10).

# Ranks the funnel so "furthest reached" survives a later scheduling mail dragging a row back.
# 'rejection' is deliberately absent: it is terminal, not a rung.
_RANK = {"contact": 0, "applied": 1, "assessment": 2, "interview": 3, "onsite": 4, "offer": 5}
_REAL = _RANK["assessment"]        # assessment and above is a stage; "contact" is not a response

# `_SIGNALS` demands a whole phrase because it drives a write. Once the credibility filters
# below have run, the bare word is safe and necessary: "Next steps for your Google interviews"
# is plainly an interview thread and under-reading it loses a real callback.
_BARE = [("onsite", re.compile(r"\bon-?site\b|final round", re.I)),
         ("interview", re.compile(r"\binterviews?\b", re.I)),
         ("assessment", re.compile(r"\bassessment\b|\bcoding (?:test|challenge)\b", re.I)),
         # "Next Steps with Microsoft". After assessment — an OA mail says "next steps" too, so
         # let the more specific word win. The lookahead drops "Next Steps for Your (Job)
         # Application", which is the ATS talking to everyone who clicked apply.
         ("interview", re.compile(r"\bnext steps?\b(?!\s+for\s+your\s+(?:job\s+)?application)", re.I))]

# A calendar invite is a booked human and outranks whatever the subject says. "Invitation from
# an unknown sender: Pinterest Chat | Alex & Crystal" carries no interview keyword at all and
# is the single most decisive message in that thread.
_BOOKED = re.compile(
    r"^\s*(?:invitation|updated invitation|invitación|邀请|日程邀请|confirmed|accepted|"
    r"reminder|canceled|cancelled)\s*[:：]|"
    r"^\s*invitation from an unknown sender\s*[:：]|"
    r"(?:has been|is|was) scheduled|appointment booked|calendar invite|"
    r"upcoming interview\b|interview reminder\b|"
    r"you'?re all set|see you (?:on|at)\b", re.I)

# Broadcast/receipt subjects. Even from a human-looking sender these are not a reply to her.
_BULKISH = re.compile(
    r"job alert|new jobs?\b|newsletter|digest\b|webinar|course\b|certificat|"
    r"we'?re hiring|now hiring|journey begins|profile has been submitted|"
    r"thank you for filling out|roundup|top picks", re.I)

_NOREPLY = re.compile(r"no-?reply|donotreply|do-not-reply|notification|mailer-daemon", re.I)
# Wider than _NOREPLY by one word: Cisco's "Your Cisco Journey Begins Here – Next Steps for Your
# Application" comes from sample-5cc464b5@example.com, reads as an interview invite to any
# subject matcher, and goes to everyone who clicks apply.
_MACHINE_SENDER = re.compile(r"no-?reply|donotreply|do-not-reply|jobalert|notification", re.I)
_ASSESSMENT_VENDOR = re.compile(
    r"codesignal|hackerrank|coderpad|karat|codility|hackerearth|woven|byteboard", re.I)
# The other half of the machine-sender rule. Greenhouse and Ashby send genuine invitations from
# no-reply@ all day ("Anthropic invitation to interview for Fellow program"); a subject this
# specific is evidence on its own, where "next steps for your application" never is.
_EXPLICIT_INVITE = re.compile(
    r"invit(?:ation|e|ing) (?:you )?to (?:an? )?interview|interview (?:invitation|invite|confirmation)|"
    r"schedule your interview|interview availability|phone screen|"
    r"(?:invitation|invite) to interview", re.I)
# Recruiter-initiated contact. Not a stage — its only job is to prove the conversation started
# before the application did, which disqualifies the company from "attributable".
_OUTREACH = re.compile(
    r"opportunity (?:at|with|for)\b|hello from |reaching out|invitation from .+ to apply|"
    r"are you (?:still )?(?:job |open |looking)|following up on your|"
    r"would you be (?:interested|open)|exciting .+(?:role|opportunity|developer|engineer)|"
    r"new .+ opportunity|let'?s (?:connect|chat)|quick (?:chat|call)\b", re.I)
_REFERRAL = re.compile(r"invitation from .+ to apply|referr", re.I)

# signal name -> (stage, outcome) for store.record_signal. Mirrors the `_SIGNALS` table so the
# widened ladder above commits exactly what the narrow one would have.
_COMMIT_AS = {
    "assessment": ("screen", ""), "interview": ("interview", ""), "onsite": ("onsite", ""),
    "offer": ("offer", "offer"), "rejection": ("", "rejected"),
}


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


def signal_of(subject: str) -> str | None:
    """Subject -> signal name, the widened ladder. Bodies are never read here: a thread quotes
    its own history, so a rejection quoted inside a later reply would re-kill the whole thread."""
    subject = subject or ""
    if applied_scan._CONFIRM.search(subject) and not applied_scan._NOT_CONFIRM.search(subject):
        return "applied"                       # an auto-ack, ranked below every real stage
    hit = _classify(subject, "")
    if hit:
        return hit[0]
    for name, pat in _BARE:
        if pat.search(subject):
            return name
    return None


def _ackish(subject: str, sender: str) -> bool:
    """The ATS talking to itself: receipts, blasts, and no-reply mail whose subject says nothing."""
    subject, sender = subject or "", sender or ""
    if _BULKISH.search(subject):
        return True
    if applied_scan._CONFIRM.search(subject) and not applied_scan._NOT_CONFIRM.search(subject):
        return True
    if _NOT_A_RESPONSE.search(subject):
        return True
    # A rejection or an OA invite often comes from no-reply@, but its subject is readable and it
    # never reaches this line.
    return bool(_NOREPLY.search(sender)) and signal_of(subject) in (None, "applied")


def _credible(subject: str, sender: str, thread_has_reply: bool) -> bool:
    """Does this message prove a human decision, or is it the ATS talking to itself?"""
    subject, sender = subject or "", sender or ""
    if _ASSESSMENT_VENDOR.search(f"{sender} {subject}"):
        return True                            # CodeSignal/HackerRank are real screens
    if _EXPLICIT_INVITE.search(subject):
        return True                            # too specific for an auto-acknowledgement
    if _BOOKED.search(subject):
        return True                            # a calendar invite is a booked human
    if thread_has_reply:
        return True                            # she answered it, so it was worth answering
    return not _MACHINE_SENDER.search(sender)


def company_signals(threads: list[dict]) -> dict[str, dict]:
    """{normalized company: earliest credible stage signal, earliest hiring-shaped contact}.

    A thread is `{company, tier, from, messages: [{date, subject, from, from_me}]}` — the shape
    `data/mail_label_scan.json` already stores, so the live scan and the backfill agree by
    construction. Only the `auto` tier: a `review` thread is one mail_label refused to attribute
    to a company, and guessing there puts another company's interview on this row.
    """
    out: dict[str, dict] = {}
    for t in threads:
        if t.get("tier", "auto") != "auto" or not t.get("company"):
            continue
        co = cfgmod.norm_key(t["company"])
        rec = out.setdefault(co, {"company": t["company"], "real": None, "contact": None,
                                  "referral": None, "rejected": None, "messages": 0,
                                  "evidence": {}})
        msgs = t.get("messages") or []
        replied = any(m.get("from_me") for m in msgs)
        rec["messages"] += len(msgs)
        for m in msgs:
            subject, when = m.get("subject") or "", m.get("date") or ""
            sender = m.get("from") or t.get("from") or ""
            if not when:
                continue
            if _REFERRAL.search(subject) and (rec["referral"] is None or when < rec["referral"]):
                rec["referral"] = when
            if m.get("from_me"):
                continue
            sig = signal_of(subject)
            if sig == "rejection":
                rec["rejected"] = max(rec["rejected"] or "", when)
                rec["evidence"].setdefault(when, {"kind": sig, "subject": subject[:110],
                                                  "from": sender[:60]})
                continue
            hiring = sig is not None or bool(_OUTREACH.search(subject))
            human = not _MACHINE_SENDER.search(sender)
            if hiring and (human or _ASSESSMENT_VENDOR.search(subject)) \
                    and not _ackish(subject, sender):
                if rec["contact"] is None or when < rec["contact"]:
                    rec["contact"] = when
            if _RANK.get(sig, -1) < _REAL and _BOOKED.search(subject) \
                    and not _ackish(subject, sender):
                sig = "interview"
            if _RANK.get(sig, -1) < _REAL:
                continue
            if not _credible(subject, sender, replied):
                continue
            if rec["real"] is None or when < rec["real"]:
                rec["real"] = when
            rec["evidence"].setdefault(when, {"kind": sig, "subject": subject[:110],
                                              "from": sender[:60]})
    return out


def merge_signals(*layers: dict[str, dict]) -> dict[str, dict]:
    """Fold several `company_signals` results into one, keeping the EARLIEST of every date.

    The unattended run scans 14 days (launchd, 2026-08-03) — far too narrow to see that
    Microsoft's recruiter opened the thread on 07-17. Folding the long-window
    `data/mail_label_scan.json` cache in behind the fresh scan is what stops a pre-existing
    pipeline from graduating into "the machine did this" the moment the InMail scrolls out of
    the window. Evidence is merged too, but only the fresh layer proposes writes.
    """
    out: dict[str, dict] = {}
    for layer in layers:
        for co, rec in layer.items():
            cur = out.setdefault(co, {"company": rec["company"], "real": None, "contact": None,
                                      "referral": None, "rejected": None, "messages": 0,
                                      "evidence": {}})
            for k in ("real", "contact", "referral"):
                if rec.get(k) and (cur[k] is None or rec[k] < cur[k]):
                    cur[k] = rec[k]
            if rec.get("rejected"):
                cur["rejected"] = max(cur["rejected"] or "", rec["rejected"])
            cur["messages"] += rec.get("messages", 0)
            for when, ev in (rec.get("evidence") or {}).items():
                cur["evidence"].setdefault(when, ev)
    return out


# tools/mail_label.CACHE — hard-coded there too, so it is named here rather than imported: an
# unattended writer in app/ must not take a dependency on tools/.
MAIL_CACHE = "data/mail_label_scan.json"


def cached_threads(cfg: dict) -> list[dict]:
    """The mail_label scan cache, or [] when it has never run. Read-only, never refreshed here:
    `tools/mail_label` owns that file and runs on its own launchd schedule."""
    p = cfgmod.resolve_path((cfg.get("mail_label") or {}).get("cache", MAIL_CACHE))
    try:
        return json.loads(Path(p).read_text(encoding="utf-8")).get("threads", [])
    except Exception:
        return []


# --- her side of it: which applications could the mail be about ------------------------------

# Everything past 'applied' counts, rejected included: a company's first_applied date has to be
# right even after one of its requisitions has already been closed out.
APPLIED_STATUSES = ("applied", "interviewing", "rejected")


def _open_roles(conn, statuses: tuple[str, ...] = ("applied", "interviewing")) -> dict[str, list[dict]]:
    """Applications that could still receive news, grouped by normalized company."""
    rows = conn.execute(
        "SELECT r.job_id, r.company, r.company_norm, r.title, r.weighted_score,"
        "       s.status, s.applied_date, s.stage, s.outcome, s.first_response_at"
        " FROM jobs.roles r JOIN role_state s ON s.job_id = r.job_id"
        f" WHERE s.status IN ({','.join('?' * len(statuses))})"
        "   AND (s.outcome IS NULL OR s.outcome IN ('in_progress','rejected'))", statuses).fetchall()
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


def _best_role(hits: list[dict], when: str) -> dict:
    """The likeliest of N when the mail names no requisition.

    The most recently submitted application that was already in before the mail arrived: a
    reply on 08-03 cannot be about a form filed on 08-05, and among the rest the freshest one
    is the one still moving through review. Ties break on score, then job_id, so the same inbox
    always picks the same row — a backfill that re-ran and moved the signal to a different
    requisition would be worse than not running it. The guess is recorded as a guess
    (`response_confidence='company'`); nothing downstream may read it as certainty.
    """
    def key(r):
        d = r.get("applied_date") or ""
        before = bool(d) and bool(when) and d <= when
        return (0 if before else 1,               # already in when the mail landed
                _neg(d) if before else d,         # newest of those, else oldest of the rest
                -(r.get("weighted_score") or 0), r["job_id"])
    return sorted(hits, key=key)[0]


def _neg(d: str) -> str:
    """Sort ISO dates descending inside an ascending tuple sort."""
    return "".join(chr(ord("9") - (ord(c) - ord("0"))) if c.isdigit() else c for c in d)


def attribute(messages: list[dict], signals: dict[str, dict], by_co: dict) -> list[dict]:
    """Turn classified messages + company signals into proposals, one per distinct mail.

    `messages` are already resolved to candidate roles (`hits`); `signals` carries the company's
    whole history, including mail older than the scan window, and decides the verdict.
    """
    first_applied: dict[str, str] = {}
    for co, roles in by_co.items():
        dates = [r["applied_date"] for r in roles if r.get("applied_date")]
        if dates:
            first_applied[co] = min(dates)

    props: list[dict] = []
    seen: set[tuple] = set()
    for m in messages:
        hits = m.get("hits") or []
        if not hits:
            continue
        sig = m["signal"]
        co = cfgmod.norm_key(hits[0]["company"])
        rec = signals.get(co) or signals.get(cfgmod.norm_key(m.get("company") or "")) or {}
        key = (co, sig, (m["subject"] or "")[:40])
        if key in seen:
            continue
        seen.add(key)

        first = min((r["applied_date"] for r in hits if r.get("applied_date")), default=None)
        first = min([d for d in (first, first_applied.get(co)) if d], default=None)
        real, contact = rec.get("real"), rec.get("contact")
        if first and ((real and real < first) or (contact and contact < first)):
            verdict = "pre-existing"      # the conversation was open before she applied
        else:
            verdict = "attributable"

        if len(hits) == 1:
            confidence, scope, role = "high", "role", hits[0]
        else:
            confidence, scope, role = "medium", "company", _best_role(hits, m["date"])
        if verdict == "pre-existing":
            confidence = "low"            # real, but not something this system caused

        stage, outcome = _COMMIT_AS.get(sig, ("", ""))
        props.append({
            "signal": sig, "stage": stage, "outcome": outcome,
            "date": m["date"], "subject": (m["subject"] or "")[:130], "from": (m["from"] or "")[:60],
            "company": hits[0]["company"], "company_norm": co,
            "confidence": confidence, "scope": scope, "verdict": verdict,
            "n_candidates": len(hits), "first_applied": first,
            "contact_at": contact, "referral_at": rec.get("referral"),
            "job_id": role["job_id"],
            "roles": [{"job_id": r["job_id"], "title": r["title"], "status": r["status"],
                       "stage": r["stage"], "applied_date": r["applied_date"]}
                      for r in [role] + [h for h in hits if h["job_id"] != role["job_id"]][:4]],
        })
    props.sort(key=lambda p: ({"high": 0, "medium": 1, "low": 2}[p["confidence"]], p["date"]))
    return props


def committable(p: dict) -> bool:
    """`--apply` writes a response only when the company is certain AND the application caused
    it. Everything else is reported and left for /outcomes."""
    return p["verdict"] == "attributable" and p["confidence"] in ("high", "medium")


# --- the live scan ---------------------------------------------------------------------------

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
        messages, scanned = [], 0

        for uid in reversed(uids):                                  # newest first
            typ, md = M.fetch(uid, "(RFC822.HEADER)")
            if typ != "OK" or not md or not md[0]:
                continue
            hdr = email.message_from_bytes(md[0][1])
            subject = applied_scan._decode(hdr.get("Subject"))
            sender = applied_scan._decode(hdr.get("From"))
            scanned += 1
            if applied_scan._NOT_CONFIRM.search(f"{subject} {sender}"):
                continue

            body = ""
            sig = signal_of(subject)
            if sig == "applied":
                continue                                            # "thanks for applying"
            if sig is None:                                         # subject was inconclusive
                typ2, bd = M.fetch(uid, "(RFC822)")
                if typ2 == "OK" and bd and bd[0]:
                    body = applied_scan._body_text(email.message_from_bytes(bd[0][1]))
                if not body or applied_scan._CONFIRM.search(body):
                    continue
                hit = _classify(subject, body)
                if not hit:
                    continue
                sig = hit[0]
            if sig != "rejection" and _NOT_A_RESPONSE.search(f"{subject} {body[:800]}"):
                continue                                            # an auto-ack isn't a reply

            dom = (re.search(r"@([\w.-]+)", sender) or [None, ""])[1]
            disp = re.sub(r"<.*?>", "", sender).strip().strip('"')
            m = applied_scan._TITLE_IN_SUBJECT.search(subject)
            title_hint = (m.group(1).strip() if m else "")
            hits = (_match(disp, title_hint, subject, by_co)
                    or _match(dom.split(".")[0], title_hint, subject, by_co))
            if not hits:
                continue
            messages.append({"date": _date_of(hdr.get("Date")), "subject": subject,
                             "from": sender, "from_me": False, "signal": sig,
                             "company": hits[0]["company"], "hits": hits})

        # One pseudo-thread per company, so the company-level rules see the same shape they see
        # in the cache. The window is short by design (launchd runs --days 14), so the long
        # history behind it comes from mail_label's cache — see merge_signals.
        threads: dict[str, dict] = {}
        for m in messages:
            t = threads.setdefault(m["company"], {"company": m["company"], "tier": "auto",
                                                  "from": m["from"], "messages": []})
            t["messages"].append(m)
        fresh = company_signals(list(threads.values()))
        signals = merge_signals(company_signals(cached_threads(cfg)), fresh)

        proposals = attribute(messages, signals, by_co)
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
    """Commit ONE proposal against ONE chosen role. Idempotent via record_signal's monotonicity.

    A `pre-existing` proposal is committed as inbound contact: the note lands on the row so the
    conversation is visible, but nothing marks it a response to this application."""
    note = f"{proposal['signal']}: {proposal['subject']}"
    if proposal.get("scope") == "company":
        note += f" [company-level: 1 of {proposal.get('n_candidates')} open reqs]"
    return store.record_signal(
        conn, job_id,
        stage=proposal["stage"] or ("responded" if proposal["signal"] != "rejection" else None),
        outcome=proposal["outcome"] or None,
        note=note, when=proposal["date"],
        confidence=("role" if proposal.get("scope", "role") == "role" else "company"),
        inbound=(proposal.get("verdict") == "pre-existing"))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--apply", action="store_true",
                    help="commit the responses this system can be shown to have caused")
    args = ap.parse_args(argv)

    cfg = cfgmod.load_config()
    if not available(cfg):
        print("mailbox not configured (mailcode.email + gmail_app_password in secrets.yaml)")
        return 1
    res = scan(cfg, days=args.days)
    icon = {"offer": "🎉", "rejection": "❌", "onsite": "🏢", "interview": "📞", "assessment": "📝"}
    props = res["proposals"]
    print(f"scanned {res['scanned']} emails over {args.days}d → {res['matched']} outcome signals\n")
    for p in props:
        tag = p["scope"] if p["verdict"] == "attributable" else p["verdict"]
        print(f"{icon.get(p['signal'], '·')} [{p['confidence']:6}] {p['date']}  {p['company']:18} "
              f"{p['signal']:10} {tag:12} {p['subject'][:50]}")
        for r in p["roles"][:1 if p["scope"] == "company" else 5]:
            extra = (f"  (1 of {p['n_candidates']} open at {p['company']})"
                     if p["scope"] == "company" else "")
            print(f"        {r['job_id']:34} {r['title'][:46]}{extra}")
    n_ok = sum(1 for p in props if committable(p))
    if not args.apply:
        print(f"\nnothing written. `--apply` would commit {n_ok} responses "
              f"({sum(1 for p in props if committable(p) and p['scope'] == 'role')} role-level, "
              f"{sum(1 for p in props if committable(p) and p['scope'] == 'company')} company-level); "
              f"the rest are pre-existing pipelines or need a pick.")
        return 0

    conn = store.connect_view(cfg)
    try:
        n = 0
        for p in props:
            if not committable(p):
                continue
            apply_proposal(cfg, conn, p, p["job_id"])
            n += 1
        print(f"\ncommitted {n} signals "
              f"({sum(1 for p in props if committable(p) and p['scope'] == 'company')} company-level)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
