"""Reconcile 'already applied' from the job-search inbox.

Scans the last N days of candidate for application-confirmation emails ("Thanks for applying",
"We received your application", …), pulls the company (and title when the email states it), and
matches each against roles in the DB. It returns a REVIEW QUEUE — {email, best-matched role(s)} —
it never changes status itself. The UI shows the queue and the owner one-click confirms each,
because a confirmation email names the company but rarely pins down WHICH of (e.g.) 197 OpenAI
roles it was; auto-marking on a company match alone would corrupt the DB. Read-only on the inbox
and the jobs DB; writes status only through the existing confirm endpoint on the owner's click.
"""
from __future__ import annotations

import email
import imaplib
import re
from datetime import datetime, timedelta
from email.header import decode_header

from . import config as cfgmod
from . import mail, store

# Subjects/bodies that mean "your application was received" — deliberately tight, because job
# alerts, "apply now" blasts and recruiter outreach must NOT count as proof you applied.
_CONFIRM = re.compile(
    r"thank you for (your interest|applying)|thanks for applying|we(?:'ve| have)? received your "
    r"application|your application (?:has been|was) (?:received|submitted)|application (?:received|"
    r"submitted|confirmation)|received your application|successfully applied|application to .* "
    r"(?:has been|was) received",
    re.I,
)
# Hard excludes — recruiter/marketing/alert mail that often contains "application" or "apply".
_NOT_CONFIRM = re.compile(
    r"job alert|jobs for you|recommended for you|apply now|we're hiring|new roles?|"
    r"complete your application|finish your application|saw your (profile|resume)|"
    r"interested in (you|your)|unsubscribe from (job|alerts)",
    re.I,
)
_TITLE_IN_SUBJECT = re.compile(
    r"appl(?:ication|ying) (?:for|to)[:\s]+(?:the\s+)?(.+?)(?:\s+(?:at|position|role|@)\b|[.!—-]|$)", re.I,
)


def available(cfg: dict) -> bool:
    try:
        _mc, addr, pw = mail._creds(cfg)
        return bool(addr and pw)
    except Exception:
        return False


def _decode(s) -> str:
    if not s:
        return ""
    return "".join(
        (part.decode(enc or "utf-8", "replace") if isinstance(part, bytes) else part)
        for part, enc in decode_header(s)
    )


def _body_text(msg) -> str:
    if not msg.is_multipart():
        try:
            return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
        except Exception:
            return ""
    out = []
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            try:
                out.append(part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace"))
            except Exception:
                pass
    return "\n".join(out)[:4000]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _match_roles(conn, company_hint: str, title_hint: str, roles_by_co: dict) -> list[dict]:
    """Best DB roles for an email. Company by normalized-name containment; title narrows it."""
    cn = _norm(company_hint)
    if not cn:
        return []
    hits = [r for co, rs in roles_by_co.items() if cn and (cn in co or co in cn) for r in rs]
    if not hits:
        return []
    if title_hint:
        tn = _norm(title_hint)
        titled = [r for r in hits if tn and (tn in _norm(r["title"]) or _norm(r["title"]) in tn)]
        if titled:
            hits = titled
    hits.sort(key=lambda r: (r["_status"] == "applied", -(r["weighted_score"] or 0)))
    return hits[:4]


def scan(cfg: dict, days: int = 90, max_emails: int = 400) -> dict:
    """Return {candidates: [...], scanned, matched} — a review queue, nothing written."""
    mc, addr, pw = mail._creds(cfg)
    M = imaplib.IMAP4_SSL(mc.get("imap_host", "imap.gmail.com"))
    M.login(addr, pw)
    try:
        M.select("INBOX", readonly=True)
        since = (datetime.utcnow() - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE {since})')
        uids = (data[0].split() if data and data[0] else [])[-max_emails:]

        conn = store.connect_view(cfg)
        rows = conn.execute(
            "SELECT r.job_id, r.company, r.company_norm, r.title, r.apply_url, r.weighted_score,"
            " COALESCE(s.status,'new') AS _status FROM jobs.roles r"
            " LEFT JOIN role_state s ON s.job_id=r.job_id"
        ).fetchall()
        roles_by_co: dict[str, list] = {}
        for r in rows:
            roles_by_co.setdefault(r["company_norm"] or _norm(r["company"]), []).append(r)

        candidates, seen_keys = [], set()
        scanned = 0
        for uid in reversed(uids):                       # newest first
            typ, md = M.fetch(uid, "(RFC822.HEADER)")
            if typ != "OK" or not md or not md[0]:
                continue
            hdr = email.message_from_bytes(md[0][1])
            subject = _decode(hdr.get("Subject"))
            sender = _decode(hdr.get("From"))
            scanned += 1
            blob = f"{subject} {sender}"
            if _NOT_CONFIRM.search(blob):
                continue
            if not _CONFIRM.search(subject):             # cheap check on subject first
                typ2, bd = M.fetch(uid, "(RFC822)")
                body = _body_text(email.message_from_bytes(bd[0][1])) if typ2 == "OK" and bd and bd[0] else ""
                if not _CONFIRM.search(body) or _NOT_CONFIRM.search(body):
                    continue
            # company: sender display-name / domain; title: from the subject if present
            dom = (re.search(r"@([\w.-]+)", sender) or [None, ""])[1]
            disp = re.sub(r"<.*?>", "", sender).strip().strip('"')
            company_hint = disp or dom.split(".")[0]
            m = _TITLE_IN_SUBJECT.search(subject)
            title_hint = (m.group(1).strip() if m else "")

            matches = _match_roles(conn, company_hint, title_hint, roles_by_co)
            if not matches:
                # try the sender domain root as the company (e.g. greenhouse-mail → skip; stripe → Stripe)
                matches = _match_roles(conn, dom.split(".")[0], title_hint, roles_by_co)
            if not matches:
                continue
            key = matches[0]["company"] + "|" + (title_hint or subject[:30])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append({
                "subject": subject[:120],
                "from": (disp or dom)[:60],
                "company": matches[0]["company"],
                "title_hint": title_hint[:60],
                "roles": [{
                    "job_id": r["job_id"], "title": r["title"],
                    "status": r["_status"], "apply_url": r["apply_url"],
                } for r in matches],
            })
        conn.close()
        return {"scanned": scanned, "matched": len(candidates), "candidates": candidates}
    finally:
        try:
            M.logout()
        except Exception:
            pass
