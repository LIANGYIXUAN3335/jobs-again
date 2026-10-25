"""Fetch the latest email verification code (signup/login OTP) over IMAP, so the extension
can fill a "security code" field. Opt-in: config `mailcode.enabled` + a mailbox credential —
either a Gmail app password in gitignored secrets.yaml (`gmail_app_password`, the owner's) or
a per-workspace Google OAuth token from /settings (app/google_auth.py). Read-only; local-only.
"""
from __future__ import annotations

import datetime
import email
import email.utils
import hashlib
import imaplib
import json
import re
import threading
from email.header import decode_header, make_header

import yaml

from . import config as cfgmod
from . import google_auth

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
# Some senders (Greenhouse: "paste this code ... : dW9hkRYX") issue MIXED alphanumeric codes,
# which the digit-only patterns above structurally cannot see. Match the code phrase, then take
# the first mixed letters+digits token after it — the mixed requirement is what keeps ordinary
# English words out, so the trailing window can be generous.
_CODE_PHRASE = re.compile(
    r"verification code|one[\s-]?time (?:code|passcode|password|pin)|passcode|security code|"
    r"access code|confirmation code|your code is|use code|paste (?:in )?this code|"
    r"enter (?:the )?code|code[:\s]+is|\botp\b", re.IGNORECASE)
_ALNUM_TOKEN = re.compile(r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)([A-Za-z0-9]{5,10})\b")
# Conference invites carry "Meeting ID: 220313541110825 / Passcode: g7Fi9Fs7" — a passcode, but
# for the CALL, not for the form. 2026-09-25: a Heron Power calendar cancel was returned as the
# "verification code" (g7Fi9Fs7). When the mail is a meeting invite, blank those tokens first.
_MEETING = re.compile(r"teams\.microsoft\.com|zoom\.us|meet\.google\.com|webex\.com|"
                      r"meeting id|dial[- ]in|join the meeting", re.IGNORECASE)
_MEETING_TOKENS = re.compile(r"(?:meeting id|passcode|password|pwd|access code)\s*[:=]?\s*[A-Za-z0-9]{4,}", re.IGNORECASE)


def _is_year(s: str) -> bool:
    return len(s) == 4 and (s.startswith("19") or s.startswith("20"))


def _is_junk(s: str) -> bool:
    return _is_year(s) or len(set(s)) == 1        # 2026 (year) or 000000/111111 (placeholder)


_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_NOT_A_CODE_PHRASE = re.compile(r"dress code|zip code|postal code|area code|country code|source code|code of conduct", re.IGNORECASE)


def _extract_code(text: str):
    # A form code is never inside a URL, and "Dress code is casual" is not a code phrase —
    # 2026-09-25: a Heron Power onsite agenda yielded a token out of the Teams link.
    text = _URL.sub(" ", text)
    text = _NOT_A_CODE_PHRASE.sub(" ", text)
    if _MEETING.search(text):
        text = _MEETING_TOKENS.sub(" ", text)
    cand = [m.group(1) for m in _STRONG.finditer(text)] + [m.group(1) for m in _STRONG_REV.finditer(text)]
    cand = [c for c in cand if not _is_junk(c)]
    for length in (6, 8, 5, 7, 4):                       # 6 and 8 digits are the common OTP lengths
        for c in cand:
            if len(c) == length:
                return c
    # mixed alphanumeric code right after a code phrase (Greenhouse et al.)
    for pm in _CODE_PHRASE.finditer(text):
        window = text[pm.end():pm.end() + 120]
        tm = _ALNUM_TOKEN.search(window)
        if tm:
            return tm.group(1)
    # weak fallback: clearly a verification email AND exactly one non-year 6-digit number
    if _VERIFYISH.search(text):
        sixes = {n for n in re.findall(r"\b\d{6}\b", text) if not _is_junk(n)}
        if len(sixes) == 1:
            return next(iter(sixes))
    return None


class OAuthSecret:
    """Stands in for the Gmail app password when the workspace authorized Google on /settings.
    Every IMAP/SMTP login site goes through mail.imap_login / mail.smtp_login, which dispatch on
    this type (SASL XOAUTH2); a plain `str` there is still the app password, byte-for-byte the
    old path. Truthy on purpose: callers test `if not (addr and pw)`."""
    __slots__ = ("cfg", "addr")

    def __init__(self, cfg: dict, addr: str):
        self.cfg = cfg
        self.addr = addr

    def token(self) -> str:
        """A live access token (google_auth refreshes it). Raises when the token is gone or
        cannot be refreshed, so the login site fails loudly instead of sending garbage."""
        tok = google_auth.access_token(self.cfg)
        if not tok:
            raise RuntimeError(f"Google OAuth token for {self.addr} is missing or cannot be "
                               f"refreshed — re-authorize on /settings")
        return tok

    def __repr__(self) -> str:        # never the token: this object may end up in a log line
        return f"OAuthSecret({self.addr})"


def _secrets() -> dict:
    p = cfgmod.resolve_path("secrets.yaml")
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


def _password(cfg: dict, mc: dict, secrets: dict) -> str:
    """The primary mailbox's app password from secrets.yaml, or ''.

    secrets.yaml is ONE shared file at the repo root and `gmail_app_password` in it is the
    owner's (candidate). A derived workspace (config.<name>.yaml, `extends: config.yaml`)
    would otherwise pair ITS address with HER password — login fails, and available() lies.
    So: the owner's base config gets the default key; a derived workspace (cfgmod.EXTENDS_KEY
    set by load_config) gets a password only from a key it names itself, mailcode.secret_key
    (same shape as extra_accounts[].secret_key)."""
    key = mc.get("secret_key") or ("" if cfg.get(cfgmod.EXTENDS_KEY) else "gmail_app_password")
    return (secrets.get(key) or "").strip() if key else ""


def _primary(cfg: dict, mc: dict, addr: str | None, secrets: dict):
    """The primary mailbox's secret: app password (str) first, else an OAuthSecret when the
    workspace token carries the Gmail scope, else ''."""
    pw = _password(cfg, mc, secrets)
    if not pw and addr and google_auth.status(cfg)["mail_scope"]:
        return OAuthSecret(cfg, addr)
    return pw


def _cfg(cfg: dict):
    mc = cfg.get("mailcode") or {}
    addr = mc.get("email") or (cfg.get("profile") or {}).get("email")
    return mc, addr, _primary(cfg, mc, addr, _secrets())


def _accounts(cfg: dict) -> list[tuple[dict, str, str | OAuthSecret]]:
    """Every configured mailbox, primary first: [(mc, addr, pw), …].

    Extras come from config mailcode.extra_accounts ([{email, secret_key}]); each password
    lives in secrets.yaml under its own key. First extra: 小号 candidate（投递身份
    alex, 2026-09-09）— replies to its applications land there, not in the primary box."""
    mc = cfg.get("mailcode") or {}
    secrets = _secrets()
    out = []
    addr = mc.get("email") or (cfg.get("profile") or {}).get("email")
    pw = _primary(cfg, mc, addr, secrets)
    if addr and pw:
        out.append((mc, addr, pw))
    for extra in mc.get("extra_accounts") or []:
        a = (extra or {}).get("email")
        pw2 = (secrets.get((extra or {}).get("secret_key") or "") or "").strip()
        if a and pw2:
            out.append((mc, a, pw2))
    return out


def within_minutes(cfg: dict) -> int:
    """How old a code mail may be and still count. Some senders' codes arrive after 15 minutes, hence the setting."""
    try:
        minutes = int((cfg.get("mailcode") or {}).get("within_minutes") or 15)
    except (TypeError, ValueError):
        return 15
    return minutes if minutes > 0 else 15


def available(cfg: dict) -> bool:
    """True when the mailbox is switched on AND has a credential: the app password or a
    Google OAuth token with the Gmail scope (an unauthorized workspace stays quietly off)."""
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


def _mails_with_codes(account: tuple[dict, str, str], cutoff: datetime.datetime):
    """Newest first, every mail since the cutoff that carries a code. Reading leaves them unread."""
    mc, addr, pw = account
    from . import mail as _mail            # local: mail imports this module (avoid the cycle)
    M = imaplib.IMAP4_SSL(mc.get("imap_host", "imap.gmail.com"))
    try:
        _mail.imap_login(M, addr, pw)
        M.select("INBOX", readonly=True)
        since = cutoff.strftime("%d-%b-%Y")
        typ, data = M.search(None, f"(SINCE {since})")
        for num in reversed(data[0].split()[-40:]):
            typ, d = M.fetch(num, "(BODY.PEEK[])")
            if not d or not d[0]:
                continue
            msg = email.message_from_bytes(d[0][1])
            try:
                arrived = email.utils.parsedate_to_datetime(msg.get("Date"))
                if arrived.tzinfo is None:
                    arrived = arrived.replace(tzinfo=datetime.timezone.utc)
            except Exception:
                continue                              # no date: nothing proves it came after the request
            if arrived < cutoff:
                break
            subject = _decode(msg.get("Subject"))
            if _SKIP_SUBJECT.search(subject) or any(part.get_content_type() == "text/calendar" for part in msg.walk()):
                continue
            code = _extract_code(subject + " " + _body_text(msg))
            if code:
                yield {"id": msg.get("Message-ID") or f"{addr}:{num.decode()}", "arrived": arrived, "subject": subject,
                       "from": _decode(msg.get("From")), "code": code}
    finally:
        try:
            M.logout()
        except Exception:
            pass


_used_lock = threading.Lock()


def _used_path(cfg: dict):
    name = re.sub(r"\W+", "_", cfgmod.workspace_name(cfg)).strip("_").lower()
    return cfgmod.ROOT / "data" / "agent_lab" / f"used_code_mails.{name}.json"


def _company_name(company: str):
    """A whole-word test for this company's name in raw text, or None if the name is too short to
    trust. "Ro" sits inside "Microsoft", "Exa" inside "example", "Fal" inside "Falcon". Only the gap
    between the name's own words may vary, so "Acme Labs" still finds acme-labs.com and acmelabs.com.
    [^\\W_] is a letter or a digit: an underscore separates words here, as in exa_jobs@."""
    if len(cfgmod.norm_key(company)) < 3:
        return None
    words = re.findall(r"[^\W_]+", company)
    return re.compile(r"(?<![^\W_])" + r"[\W_]*".join(words) + r"(?![^\W_])", re.IGNORECASE)


def fetch_code_for(cfg: dict, company: str, after: datetime.datetime, scan=_mails_with_codes) -> dict:
    """The code this company sent after `after`, or none at all.

    A wrong code typed into a form cannot be taken back, so a mail from anyone else never counts,
    two different codes mean nobody can tell which is right, and a mail is used only once.
    Only hashes of Message-IDs are stored. The code itself goes to the caller and nowhere else.
    """
    name = _company_name(company)
    if not name:
        return {"code": None, "matches": 0}
    path = _used_path(cfg)
    with _used_lock:
        try:
            used = set(json.loads(path.read_text()))
        except (OSError, ValueError):
            used = set()
        hits = []
        for account in _accounts(cfg):
            try:
                for item in scan(account, after):
                    mark = hashlib.sha256(item["id"].encode()).hexdigest()
                    # Each on its own: a name must not form across the end of one and the start of the other.
                    if mark not in used and (name.search(item["subject"]) or name.search(item["from"])):
                        hits.append({**item, "mark": mark})
            except Exception:  # noqa: BLE001 — one mailbox down must not hide the other
                continue
        if len({hit["code"] for hit in hits}) != 1:
            return {"code": None, "matches": len({hit["code"] for hit in hits})}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(sorted(used | {hit["mark"] for hit in hits})))
        path.chmod(0o600)
        return {"code": hits[0]["code"], "subject": hits[0]["subject"], "from": hits[0]["from"]}


def fetch_latest_code(cfg: dict, account: tuple[dict, str, str] | None = None) -> dict:
    """Return {code, subject, from, date} for the newest verification email within the window,
    or {code: None} if none found. Raises on connection/login failure. `account` overrides
    which mailbox is read (default: the primary one)."""
    mc, addr, pw = account or _cfg(cfg)
    if not (addr and pw):
        raise RuntimeError("no mailbox credential: gmail_app_password in secrets.yaml or a Google "
                           "OAuth token from /settings (or mailcode.email unset)")
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(minutes=within_minutes({"mailcode": mc}))

    from . import mail as _mail            # local: mail imports this module (avoid the cycle)
    M = imaplib.IMAP4_SSL(mc.get("imap_host", "imap.gmail.com"))
    try:
        _mail.imap_login(M, addr, pw)
        M.select("INBOX", readonly=True)                # checking for a code must not mark mail as read
        since = (now - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f"(SINCE {since})")
        ids = data[0].split()
        for num in reversed(ids[-40:]):                 # newest first, cap the scan
            typ, d = M.fetch(num, "(BODY.PEEK[])")
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
            if any(part.get_content_type() == "text/calendar" for part in msg.walk()):
                continue                                  # a calendar invite/cancel never carries a form code
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


def fetch_latest_code_any(cfg: dict) -> dict:
    """Newest verification code across EVERY configured mailbox. A code requested while
    applying under the 小号 identity lands in candidate, not the primary box — so check
    both and keep whichever code is newer. A mailbox that errors is skipped (the other may
    still have the code); only if every box fails is the last error raised."""
    best, last_err = {"code": None}, None
    for acct in _accounts(cfg):
        try:
            r = fetch_latest_code(cfg, account=acct)
        except Exception as e:  # noqa: BLE001 — per-box transport errors must not kill the other box
            last_err = e
            continue
        if r.get("code") and (r.get("date") or "") > (best.get("date") or ""):
            best = r
    if best.get("code") is None and last_err is not None:
        raise last_err
    return best
