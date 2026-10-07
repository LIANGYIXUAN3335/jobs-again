"""Per-workspace Google authorization — one click on /settings, no app password.

Why this exists (owner 2026-09-23): a second candidate's workspace (config.<name>.yaml) has
its own mailbox and calendar. The owner's mail runs on a Gmail app password in the shared
secrets.yaml and her calendar on a token made by tools/gcal_auth.py — neither works for
another person's Google account without them handing over a password. So the workspace
authorizes Google itself: the browser the candidate is logged into goes through Google's
consent screen, and the refreshable token lands under THAT workspace's data dir
(data/<name>/secrets/google_token.json; the owner's default would be data/secrets/).

One token covers both halves:
  * Gmail over IMAP/SMTP via SASL XOAUTH2 — mail.imap_login / mail.smtp_login accept a
    mailcode.OAuthSecret wherever a password string works today.
  * Google Calendar read-only — gcal._service tries this token before the legacy one.

The consent must finish in the browser profile the candidate clicks from (that is where his
Google session lives), so the flow runs IN-PROCESS in the web server: start_flow → Google →
GET /oauth/google/callback → finish_flow. Not a subprocess like the health page's
重新授权日历 button, which would open the machine's default browser instead.

Secrets discipline: nothing here logs a token or the client secret. status() exposes only the
account email, granted scopes, expiry and file paths.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import config as cfgmod

MAIL_SCOPE = "https://mail.google.com/"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
SCOPES = [
    MAIL_SCOPE,                                          # IMAP + SMTP via XOAUTH2 (restricted scope; fine for Test users)
    CALENDAR_SCOPE,                                      # free/busy for scheduling replies (never writes)
    "https://www.googleapis.com/auth/userinfo.email",    # to learn WHICH account was authorized
    "openid",
]
DEFAULT_CLIENT = "/path/to/.config/jobscan-secrets/gcal_client.json"   # same "Desktop app" client as gcal
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Consent flows waiting for Google to call back: {state: (Flow, started_epoch)}. The state is
# the CSRF token in the redirect URL; an unknown or stale one is refused in finish_flow.
_PENDING: dict[str, tuple[object, float]] = {}
_PENDING_TTL_S = 600          # ten minutes is plenty to pick an account and click Allow
_lock = threading.Lock()


# ── paths ─────────────────────────────────────────────────────────────────────────────────

def client_secret_path(cfg: dict) -> Path:
    """The OAuth client JSON (Google Cloud Console → Credentials → 'Desktop app'). Config
    google.client_secret, else the file gcal already uses. One client serves every workspace —
    it identifies the APP, not the person; the person is whoever consents."""
    g = cfg.get("google") or {}
    return cfgmod.resolve_path(g.get("client_secret") or DEFAULT_CLIENT)


def token_path(cfg: dict) -> Path:
    """Where this workspace's token lives. Config google.token, else <dir of user_db_path>/
    secrets/google_token.json — keyed off user_db_path because that is the one path every
    workspace config already overrides, so a new workspace gets its own token dir for free."""
    g = cfg.get("google") or {}
    if g.get("token"):
        return cfgmod.resolve_path(g["token"])
    return cfgmod.user_db_path(cfg).parent / "secrets" / "google_token.json"


def expected_account(cfg: dict) -> str | None:
    """The mailbox this workspace is configured for (mailcode.email, else profile.email)."""
    mc = cfg.get("mailcode") or {}
    return mc.get("email") or (cfg.get("profile") or {}).get("email") or None


def _account_mismatch(account: str | None, expected: str | None) -> bool:
    """True when a token must NOT serve this workspace: the workspace names a mailbox and the
    token's account is someone else's — or unknown. Unknown counts as a mismatch on purpose:
    a token we cannot attribute to the right person is exactly the wrong-Chrome-profile case
    (the OWNER's full-scope Gmail+Calendar token landing in data/<name>/secrets/, and the
    candidate's scheduling replies quoting HER free/busy). No expected account → nothing to
    compare against, so no check."""
    if not expected:
        return False
    return not account or account.strip().lower() != expected.strip().lower()


# ── token file ────────────────────────────────────────────────────────────────────────────

def _read_token(cfg: dict) -> dict | None:
    """Parsed token JSON, or None when missing/unreadable/garbage. Never raises: every caller
    (mailcode.available on each extension lookup, gcal, the settings page) must degrade to
    'not connected'."""
    tp = token_path(cfg)
    try:
        raw = json.loads(tp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _save(cfg: dict, creds, account: str | None) -> None:
    """Write creds to token_path with the GRANTED scopes and the account email. Parent dir
    0o700, file 0o600, atomic replace — it holds a refresh token, i.e. standing access to
    someone's mailbox."""
    data = json.loads(creds.to_json())
    # Google's granular consent lets the person untick a scope. Store what was actually
    # granted (not what we asked for): status() then tells the truth about mail/calendar, and
    # a later refresh never asks Google for more than it gave (that raises RefreshError).
    granted = list(getattr(creds, "granted_scopes", None) or creds.scopes or [])
    data["scopes"] = granted
    if account:
        data["account"] = account
    tp = token_path(cfg)
    if not tp.parent.exists():
        tp.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(tp.parent, 0o700)
    tmp = tp.with_name(tp.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data))
    os.chmod(tmp, 0o600)
    os.replace(tmp, tp)


# ── the consent flow ──────────────────────────────────────────────────────────────────────

def _purge_pending(now: float | None = None) -> None:
    now = now or time.time()
    for k in [k for k, (_f, t0) in _PENDING.items() if now - t0 > _PENDING_TTL_S]:
        _PENDING.pop(k, None)


def start_flow(cfg: dict, redirect_uri: str) -> str:
    """Begin the consent flow; returns the Google URL the browser must open. `redirect_uri`
    is this web server's /oauth/google/callback — a 'Desktop app' client accepts any
    loopback http://127.0.0.1:<port>/… without pre-registration. Raises FileNotFoundError
    when the client JSON is missing (the route turns that into a 503)."""
    client = client_secret_path(cfg)
    if not client.exists():
        raise FileNotFoundError(str(client))
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_secrets_file(str(client), SCOPES, redirect_uri=redirect_uri)
    # offline + consent → Google always issues a refresh token (a re-consent without `consent`
    # returns only an access token and the mailbox would go dark in an hour).
    url, state = flow.authorization_url(access_type="offline", prompt="consent",
                                        include_granted_scopes="true")
    with _lock:
        _purge_pending()
        _PENDING[state] = (flow, time.time())
    return url


def _fetch_email(creds) -> str | None:
    """The authorized account's email via the userinfo endpoint (needs userinfo.email/openid).
    Never raises. None is not neutral though: when the workspace names a mailbox, finish_flow
    refuses a token it cannot attribute (see _account_mismatch) — only a workspace with no
    expected account keeps an anonymous token, and the settings page says so."""
    try:
        from google.auth.transport.requests import AuthorizedSession
        r = AuthorizedSession(creds).get(USERINFO_URL, timeout=15)
        if r.status_code == 200:
            return (r.json().get("email") or "").strip() or None
    except Exception:  # noqa: BLE001 — see docstring
        pass
    return None


def _revoke_remote(tok: str | None) -> None:
    """Best-effort revoke at Google. Offline is fine: the grant can always be removed at
    myaccount.google.com/permissions, and the caller drops/never writes the file regardless."""
    if not tok:
        return
    try:
        import requests
        requests.post(REVOKE_URL, params={"token": tok},
                      headers={"content-type": "application/x-www-form-urlencoded"}, timeout=10)
    except Exception:  # noqa: BLE001 — see docstring
        pass


def finish_flow(cfg: dict, state: str, code: str) -> dict:
    """Second half: Google redirected back with ?state=&code=. Exchanges the code, learns the
    account email, saves the token for this workspace, returns status(cfg).

    Refuses (ValueError, nothing written, grant revoked) when the authorized account is not the
    one this workspace is configured for, or could not be determined. Rendering the mismatch
    AFTER saving — what the callback page used to do — is too late: the file already held a
    stranger's refresh token and every consumer (mailcode, gcal) would have used it."""
    with _lock:
        _purge_pending()
        entry = _PENDING.pop(state or "", None)
    if entry is None:
        raise ValueError("unknown or expired state")
    flow, _started = entry
    # oauthlib raises a bare Warning("Scope has changed") when Google returns a different scope
    # set than requested — which include_granted_scopes and granular consent both make normal.
    # Relax it; _save records the granted set instead.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow.fetch_token(code=code)
    creds = flow.credentials
    account = _fetch_email(creds)
    expected = expected_account(cfg)
    if _account_mismatch(account, expected):
        _revoke_remote(getattr(creds, "refresh_token", None) or getattr(creds, "token", None))
        got = account or "（未知：userinfo 没返回邮箱）"
        raise ValueError(f"授权的 Google 账号是 {got}，但这个工作台配置的邮箱是 {expected} —— 多半是这个 "
                         f"Chrome profile 里登着别人的账号。这次的 token 没有保存（已向 Google 撤销）；"
                         f"在 Chrome 右上角切到 {expected} 后再授权一次。")
    _save(cfg, creds, account)
    return status(cfg)


# ── using the token ───────────────────────────────────────────────────────────────────────

def credentials(cfg: dict, scopes: list[str] | None = None):
    """google.oauth2 Credentials for this workspace, refreshed (and saved back) when expired.
    None when there is no token, the file is broken, `scopes` asks for something not granted,
    or the refresh fails — never raises into callers."""
    raw = _read_token(cfg)
    if not raw or not raw.get("refresh_token"):
        return None
    # Defense in depth against a hand-copied file: finish_flow never writes a foreign token,
    # but a token file whose `account` is not this workspace's mailbox is still "not connected".
    if _account_mismatch(raw.get("account"), expected_account(cfg)):
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_info(raw)      # scopes = the granted set _save wrote
        if scopes and not set(scopes) <= set(creds.scopes or []):
            return None
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
            _save(cfg, creds, raw.get("account"))
        return creds if creds.valid else None
    except Exception:  # noqa: BLE001 — revoked/garbage token or no network → "not connected"
        return None


def access_token(cfg: dict) -> str | None:
    """A live bearer token (refreshing if needed), or None."""
    creds = credentials(cfg)
    return creds.token if creds is not None and creds.valid else None


def xoauth2_string(user: str, token: str) -> str:
    """The SASL XOAUTH2 initial client response (before base64), per Google's spec."""
    return f"user={user}\x01auth=Bearer {token}\x01\x01"


# ── introspection ─────────────────────────────────────────────────────────────────────────

def status(cfg: dict) -> dict:
    """What the settings page and mailcode.available need — read from the token FILE only
    (no network, no google imports), because mailcode.available runs on every extension
    lookup."""
    tp = token_path(cfg)
    raw = _read_token(cfg) or {}
    scopes = raw.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    scopes = list(scopes)
    account = raw.get("account") or None
    expected = expected_account(cfg)
    authorized = bool(raw.get("refresh_token"))
    # A token for someone else (or nobody we can name) is on disk but must not light anything
    # up: mailcode.available keys off mail_scope and gcal off credentials(), so both go False
    # here. `account_blocked` is the settings page's reason line.
    blocked = authorized and _account_mismatch(account, expected)
    usable = authorized and not blocked
    return {
        "authorized": authorized,
        "account": account,
        "scopes": scopes,
        "expiry": raw.get("expiry"),
        "token_path": str(tp),
        "client_present": client_secret_path(cfg).exists(),
        "mail_scope": usable and MAIL_SCOPE in scopes,
        "calendar_scope": usable and CALENDAR_SCOPE in scopes,
        "expected_account": expected,
        "account_matches": (account.lower() == expected.lower()) if (account and expected) else None,
        "account_blocked": blocked,
    }


def revoke(cfg: dict) -> dict:
    """Forget the token: best-effort revoke at Google, then delete the file. Returns status."""
    raw = _read_token(cfg) or {}
    _revoke_remote(raw.get("refresh_token") or raw.get("token"))
    try:
        token_path(cfg).unlink()
    except FileNotFoundError:
        pass
    return status(cfg)
