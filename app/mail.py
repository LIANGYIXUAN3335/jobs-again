"""Read + reply to email for the authorized job-search mailbox (sample-5fbfd918@example.com),
using the same Gmail App Password as mailcode (jobscan/secrets.yaml). IMAP to read/search,
SMTP to send. Replies thread correctly (Re: subject + In-Reply-To/References headers).

SAFETY: sending is never automatic. The intended flow is draft -> user reviews -> user
confirms -> send_reply() is called. Nothing here sends without an explicit call.
"""
from __future__ import annotations

import datetime
import email
import imaplib
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from . import mailcode


def _dec(s: str) -> str:
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return s or ""


def _creds(cfg: dict):
    mc, addr, pw = mailcode._cfg(cfg)
    if not (addr and pw):
        raise RuntimeError("no Gmail app password / address (jobscan/secrets.yaml + config mailcode.email)")
    return mc, addr, pw


def _plain_body(msg) -> str:
    """Prefer text/plain; fall back to a tag-stripped text/html."""
    plain, html = None, None
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        try:
            txt = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
        except Exception:
            continue
        if ct == "text/plain" and plain is None:
            plain = txt
        elif ct == "text/html" and html is None:
            html = txt
    if plain is not None:
        return plain.strip()
    if html is not None:
        import re
        return re.sub(r"<[^>]+>", " ", html).strip()
    return ""


def search_recent(cfg: dict, query: str = "", days: int = 14, limit: int = 25) -> list[dict]:
    """List recent inbox messages (newest first): {uid, subject, from, date}. `query` does a
    Gmail-side full-text search (X-GM-RAW) when given, else just the time window."""
    mc, addr, pw = _creds(cfg)
    M = imaplib.IMAP4_SSL(mc.get("imap_host", "imap.gmail.com"))
    try:
        M.login(addr, pw)
        M.select("INBOX")
        since = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=days)).strftime("%d-%b-%Y")
        if query:
            typ, data = M.search(None, "X-GM-RAW", f'"{query}"')
        else:
            typ, data = M.search(None, f"(SINCE {since})")
        ids = (data[0] or b"").split()
        out = []
        for num in reversed(ids[-limit:]):
            typ, d = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
            if not d or not d[0]:
                continue
            hm = email.message_from_bytes(d[0][1])
            out.append({"uid": num.decode(), "subject": _dec(hm.get("Subject")),
                        "from": _dec(hm.get("From")), "date": hm.get("Date")})
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


def get_message(cfg: dict, uid: str) -> dict:
    """Full message for drafting a reply: includes message_id / references so send_reply can
    thread, and reply_to (the address to answer)."""
    mc, addr, pw = _creds(cfg)
    M = imaplib.IMAP4_SSL(mc.get("imap_host", "imap.gmail.com"))
    try:
        M.login(addr, pw)
        M.select("INBOX")
        typ, d = M.fetch(str(uid).encode(), "(RFC822)")
        if not d or not d[0]:
            raise RuntimeError(f"message uid {uid} not found")
        msg = email.message_from_bytes(d[0][1])
        reply_to = msg.get("Reply-To") or msg.get("From")
        return {
            "uid": str(uid),
            "subject": _dec(msg.get("Subject")),
            "from": _dec(msg.get("From")),
            "reply_to": parseaddr(reply_to)[1],
            "date": msg.get("Date"),
            "message_id": msg.get("Message-ID"),
            "references": msg.get("References") or "",
            "body": _plain_body(msg),
        }
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _re_subject(subject: str) -> str:
    s = (subject or "").strip()
    return s if s.lower().startswith("re:") else f"Re: {s}"


def send_reply(cfg: dict, to_addr: str, subject: str, body: str,
               in_reply_to: str | None = None, references: str | None = None,
               from_name: str = "Casey Example") -> dict:
    """Send a reply via SMTP. Threads via In-Reply-To/References when the original headers
    are passed. NEVER call this without the user having reviewed and approved the draft."""
    mc, addr, pw = _creds(cfg)
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, addr))
    msg["To"] = to_addr
    msg["Subject"] = _re_subject(subject)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip() if references else in_reply_to
    msg.set_content(body)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(mc.get("smtp_host", "smtp.gmail.com"), int(mc.get("smtp_port", 465)), context=ctx) as s:
        s.login(addr, pw)
        s.send_message(msg)
    return {"sent": True, "to": to_addr, "subject": msg["Subject"]}


def smtp_check(cfg: dict) -> bool:
    """Verify SMTP login works WITHOUT sending anything."""
    mc, addr, pw = _creds(cfg)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(mc.get("smtp_host", "smtp.gmail.com"), int(mc.get("smtp_port", 465)), context=ctx) as s:
        s.login(addr, pw)
    return True
