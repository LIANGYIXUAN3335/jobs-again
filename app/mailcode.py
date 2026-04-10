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

# Skip provider notification emails that aren't codes (e.g. Google's login alert).
_SKIP_SUBJECT = re.compile(r"security alert|new sign-?in|was signed in|critical security|"
                           r"suspicious sign|password (?:was )?changed|2-?step verification|"
                           r"verification (?:turned on|is on|enabled|was turned on)|turned on|"
                           r"recovery (?:email|phone)|account (?:was )?(?:created|recovered)", re.IGNORECASE)
# Strong: a 4–8 digit code sitting right next to a real verification phrase (both orders).
_STRONG = re.compile(
    r"(?:verification code|one[\s-]?time (?:code|passcode|password|pin)|passcode|security code|"
    r"access code|confirmation code|your code is|use code|enter (?:the )?code|code[:\s]+is|\botp\b)"
    r"[^0-9]{0,20}(\d{4,8})", re.IGNORECASE)
_STRONG_REV = re.compile(
    r"(\d{4,8})[^0-9]{0,20}(?:is your (?:verification|one[\s-]?time|security|login) |"
    r"verification code|one[\s-]?time (?:code|passcode))", re.IGNORECASE)
_VERIFYISH = re.compile(r"verif|one[\s-]?time|passcode|confirm your|\botp\b|log ?in code", re.IGNORECASE)


def _is_year(s: str) -> bool:
    return len(s) == 4 and (s.startswith("19") or s.startswith("20"))


def _is_junk(s: str) -> bool:
    return _is_year(s) or len(set(s)) == 1        # 2026 (year) or 000000/111111 (placeholder)


def _extract_code(text: str):
    cand = [m.group(1) for m in _STRONG.finditer(text)] + [m.group(1) for m in _STRONG_REV.finditer(text)]
    cand = [c for c in cand if not _is_junk(c)]
    for length in (6, 5, 7, 8, 4):                       # OTPs are usually 6 digits — prefer them
        for c in cand:
            if len(c) == length:
                return c
    # weak fallback: clearly a verification email AND exactly one non-year 6-digit number
    if _VERIFYISH.search(text):
        sixes = {n for n in re.findall(r"\b\d{6}\b", text) if not _is_junk(n)}
        if len(sixes) == 1:
            return next(iter(sixes))
    return None


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
            if _SKIP_SUBJECT.search(subject):
                continue                                  # provider login/security notice, not a code
            hay = subject + " " + _body_text(msg)
            code = _extract_code(hay)
            if code:
                return {"code": code, "subject": subject, "from": _decode(msg.get("From")),
                        "date": idate.isoformat() if idate else None}
        return {"code": None}
    finally:
        try:
            M.logout()
        except Exception:
            pass
