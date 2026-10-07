"""Read + reply to email for the workspace's job-search mailbox (the owner's is
sample-5fbfd918@example.com), using the same credential as mailcode (jobscan/secrets.yaml app
password, or the workspace's Google OAuth token from /settings). IMAP to read/search, SMTP to
send. Replies thread correctly (Re: subject + In-Reply-To/References headers).

SAFETY: sending is never automatic. The intended flow is draft -> user reviews -> user
confirms -> send_reply() is called. Nothing here sends without an explicit call.
"""
from __future__ import annotations

import base64
import datetime
import email
import imaplib
import mimetypes
import pathlib
import re
import smtplib
import ssl
import time
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from . import google_auth, mailcode


def imap_login(M, addr: str, secret) -> None:
    """Log in to an IMAP connection with whatever secret the workspace has: a Gmail app
    password (`str` → M.login, the original path, unchanged) or a mailcode.OAuthSecret
    (→ SASL XOAUTH2 with a live access token). Every IMAP site in app/ and tools/ calls this
    instead of M.login so one workspace token lights up all of them."""
    if not isinstance(secret, mailcode.OAuthSecret):
        M.login(addr, secret)
        return
    rounds: list[bytes] = []

    def _xoauth2(challenge):
        # Round 1: the server's empty "+" → send the SASL string. A round 2 only happens on
        # failure — Gmail then sends a base64 JSON error and waits; returning None makes
        # imaplib answer "*" (abort) so we get a clean NO instead of re-sending the token.
        rounds.append(challenge or b"")
        if len(rounds) > 1:
            return None
        return google_auth.xoauth2_string(addr, secret.token()).encode()

    try:
        M.authenticate("XOAUTH2", _xoauth2)
    except imaplib.IMAP4.error as e:
        detail = rounds[1].decode("utf-8", "replace") if len(rounds) > 1 else ""   # Google's JSON, no secrets in it
        raise imaplib.IMAP4.error(f"XOAUTH2 login failed for {addr}: {e} {detail}".strip()) from e


def smtp_login(s, addr: str, secret) -> None:
    """SMTP twin of imap_login: `str` → s.login (unchanged); OAuthSecret → AUTH XOAUTH2."""
    if not isinstance(secret, mailcode.OAuthSecret):
        s.login(addr, secret)
        return
    s.ehlo()
    auth = base64.b64encode(google_auth.xoauth2_string(addr, secret.token()).encode()).decode()
    code, resp = s.docmd("AUTH", "XOAUTH2 " + auth)
    if code == 334:
        # Gmail answers a bad token with 334 + a base64 JSON error and waits for an empty line
        # before the final 535; send it so the connection is left in a sane state, and keep
        # the decoded JSON for the error (it names the failure, never the token).
        try:
            detail = base64.b64decode(resp).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            detail = resp.decode("utf-8", "replace") if isinstance(resp, bytes) else str(resp)
        code, resp = s.docmd("")
        raise smtplib.SMTPAuthenticationError(code, f"{detail} {resp!r}".encode())
    if code != 235:
        raise smtplib.SMTPAuthenticationError(code, resp)


def _imap_connect(mc: dict, attempts: int = 3):
    last: Exception | None = None
    for i in range(attempts):
        try:
            return imaplib.IMAP4_SSL(mc.get("imap_host", "imap.gmail.com"), timeout=30)
        except OSError as e:
            last = e
            if i == attempts - 1:
                break
            time.sleep(5 * (i + 1))
    raise last or RuntimeError("IMAP connection failed")


def _dec(s: str) -> str:
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return s or ""


def _creds(cfg: dict):
    mc, addr, pw = mailcode._cfg(cfg)
    if not (addr and pw):
        raise RuntimeError("no mailbox credential / address (jobscan/secrets.yaml app password or a "
                           "Google OAuth token from /settings + config mailcode.email)")
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
    M = _imap_connect(mc)
    try:
        imap_login(M, addr, pw)
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
    M = _imap_connect(mc)
    try:
        imap_login(M, addr, pw)
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


def _hdr(v: str) -> str:
    """Header-safe: strip CR/LF and collapse runs of whitespace. Some senders wrap long
    Subjects across lines (OpenArt's had a literal newline), which SMTP rejects in a header."""
    return re.sub(r"\s+", " ", (v or "").replace("\r", " ").replace("\n", " ")).strip()


def _re_subject(subject: str) -> str:
    s = _hdr(subject)
    return s if s.lower().startswith("re:") else f"Re: {s}"


def send_reply(cfg: dict, to_addr: str, subject: str, body: str,
               in_reply_to: str | None = None, references: str | None = None,
               from_name: str | None = None, attachments: list[str] | None = None,
               new_thread: bool = False) -> dict:
    """Send a reply via SMTP. Threads via In-Reply-To/References when the original headers
    are passed (unless new_thread). Optional `attachments` = list of file paths to attach (e.g. a
    résumé PDF). NEVER call this without the user having reviewed and approved the draft."""
    mc, addr, pw = _creds(cfg)
    if not from_name:                       # the workspace's own name (owner 2026-09-23), never a hardcoded one
        from . import tailoring
        from_name = tailoring.candidate_name(cfg)
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, addr))
    msg["To"] = _hdr(to_addr)
    msg["Subject"] = _hdr(subject) if new_thread else _re_subject(subject)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if in_reply_to and not new_thread:
        msg["In-Reply-To"] = _hdr(in_reply_to)
        msg["References"] = _hdr((references + " " + in_reply_to) if references else in_reply_to)
    msg.set_content(body)
    attached = []
    for path in (attachments or []):
        p = pathlib.Path(path)
        if not p.exists():
            raise FileNotFoundError(f"attachment not found: {path}")
        ctype, _ = mimetypes.guess_type(p.name)
        maintype, subtype = (ctype.split("/", 1) if ctype else ("application", "octet-stream"))
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
        attached.append(p.name)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(mc.get("smtp_host", "smtp.gmail.com"), int(mc.get("smtp_port", 465)), context=ctx) as s:
        smtp_login(s, addr, pw)
        s.send_message(msg)
    return {"sent": True, "to": to_addr, "subject": msg["Subject"], "attachments": attached}


def smtp_check(cfg: dict) -> bool:
    """Verify SMTP login works WITHOUT sending anything."""
    mc, addr, pw = _creds(cfg)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(mc.get("smtp_host", "smtp.gmail.com"), int(mc.get("smtp_port", 465)), context=ctx) as s:
        smtp_login(s, addr, pw)
    return True
