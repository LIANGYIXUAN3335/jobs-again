"""Fetch the latest email verification code (signup/login OTP) over IMAP, so the extension
can fill a "security code" field. Opt-in: config `mailcode.enabled` + a Gmail app password in
gitignored secrets.yaml (`gmail_app_password`). Read-only; local-only.
"""
from __future__ import annotations

import datetime
import email
import email.utils
import imaplib
import re
from email.header import decode_header, make_header

import yaml

from . import config as cfgmod

# A digit run (4–8) sitting near a verification keyword — the reliable signal.
_CODE_NEAR = re.compile(
    r"(?:verification|verify|one[\s-]?time|passcode|security|confirm(?:ation)?|access|otp|pin|code)"
    r"[^0-9]{0,40}(\d{4,8})", re.IGNORECASE)
_CODE_ANY = re.compile(r"\b(\d{4,8})\b")
_VERIFYISH = re.compile(r"verif|one[\s-]?time|passcode|confirm|security|\botp\b|\bcode\b", re.IGNORECASE)


def _cfg(cfg: dict):
    mc = cfg.get("mailcode") or {}
    addr = mc.get("email") or (cfg.get("profile") or {}).get("email")
    p = cfgmod.resolve_path("secrets.yaml")
    pw = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("gmail_app_password") if p.exists() else None
    return mc, addr, (pw or "").strip()


def available(cfg: dict) -> bool:
    mc, addr, pw = _cfg(cfg)
    return bool((cfg.get("mailcode") or {}).get("enabled")) and bool(addr and pw)


def _decode(s: str) -> str:
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return s or ""


def _body_text(msg) -> str:
    parts = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                parts.append(part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", "ignore"))
            except Exception:
                pass
    text = " ".join(parts)
    return re.sub(r"<[^>]+>", " ", text)  # strip HTML tags for keyword search


def fetch_latest_code(cfg: dict) -> dict:
    """Return {code, subject, from, date} for the newest verification email within the window,
    or {code: None} if none found. Raises on connection/login failure."""
    mc, addr, pw = _cfg(cfg)
    if not (addr and pw):
        raise RuntimeError("no gmail_app_password in secrets.yaml (or mailcode.email unset)")
    within = int(mc.get("within_minutes", 15))
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(minutes=within)

    M = imaplib.IMAP4_SSL(mc.get("imap_host", "imap.gmail.com"))
    try:
        M.login(addr, pw)
        M.select("INBOX")
        since = (now - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f"(SINCE {since})")
        ids = data[0].split()
        for num in reversed(ids[-40:]):                 # newest first, cap the scan
            typ, d = M.fetch(num, "(RFC822)")
            if not d or not d[0]:
                continue
            msg = email.message_from_bytes(d[0][1])
            try:
                idate = email.utils.parsedate_to_datetime(msg.get("Date"))
                if idate and idate.tzinfo is None:
                    idate = idate.replace(tzinfo=datetime.timezone.utc)
            except Exception:
                idate = None
            if idate and idate < cutoff:
                break                                    # everything older follows → stop
            subject = _decode(msg.get("Subject"))
            hay = subject + " " + _body_text(msg)
            m = _CODE_NEAR.search(hay)
            code = m.group(1) if m else (
                (_CODE_ANY.search(hay).group(1)) if _VERIFYISH.search(hay) and _CODE_ANY.search(hay) else None)
            if code:
                return {"code": code, "subject": subject, "from": _decode(msg.get("From")),
                        "date": idate.isoformat() if idate else None}
        return {"code": None}
    finally:
        try:
            M.logout()
        except Exception:
            pass
