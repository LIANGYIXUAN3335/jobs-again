"""Per-workspace Google authorization (app/google_auth.py) and the OAuth plumbing through
mailcode / mail / gcal. No network anywhere: fake IMAP/SMTP/creds objects, token files in tmp.
"""
import base64
import json
import pathlib
import smtplib
import stat

import pytest

from app import config as cfgmod
from app import gcal, google_auth, mail, mailcode

CAL = google_auth.CALENDAR_SCOPE
MAIL = google_auth.MAIL_SCOPE


def _ws_cfg(tmp_path):
    """A derived-workspace-shaped config: own user_db_path, no app-password key, token file
    under tmp so the real machine's data/<name>/secrets never leaks into a test."""
    return {
        "user_db_path": str(tmp_path / "ws" / "user_state.db"),
        "mailcode": {"enabled": True, "email": "cand@example.com"},
        "profile": {"email": "cand@example.com"},
        "google": {"token": str(tmp_path / "ws" / "secrets" / "google_token.json")},
        cfgmod.EXTENDS_KEY: "config.yaml",
    }


def _write_token(path: pathlib.Path, scopes, account="cand@example.com"):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"token": "at", "refresh_token": "rt", "client_id": "c", "client_secret": "s",
            "scopes": scopes, "expiry": "2099-01-01T00:00:00Z"}
    if account:                                            # None → no `account` key, like a legacy/hand-made file
        data["account"] = account
    path.write_text(json.dumps(data))


# ── paths / strings ───────────────────────────────────────────────────────────────────────

def test_token_path_default_derives_from_user_db_path():
    assert google_auth.token_path({"user_db_path": "data/robin/user_state.db"}) == \
        cfgmod.ROOT / "data" / "robin" / "secrets" / "google_token.json"
    assert google_auth.token_path({"user_db_path": "data/user_state.db"}) == \
        cfgmod.ROOT / "data" / "secrets" / "google_token.json"
    # explicit override wins; absolute paths allowed
    assert google_auth.token_path({"user_db_path": "x", "google": {"token": "/tmp/t.json"}}) == \
        pathlib.Path("/tmp/t.json")


def test_xoauth2_string_format():
    assert google_auth.xoauth2_string("a@b.c", "TOK") == "user=a@b.c\x01auth=Bearer TOK\x01\x01"


# ── status / credentials ──────────────────────────────────────────────────────────────────

def test_status_missing_token(tmp_path):
    st = google_auth.status(_ws_cfg(tmp_path))
    assert st["authorized"] is False and st["account"] is None and st["scopes"] == []
    assert st["mail_scope"] is False and st["calendar_scope"] is False
    assert st["expected_account"] == "cand@example.com" and st["account_matches"] is None
    assert st["token_path"].endswith("google_token.json")


def test_status_reads_granted_scopes_and_account_match(tmp_path):
    cfg = _ws_cfg(tmp_path)
    _write_token(google_auth.token_path(cfg), [MAIL, CAL], account="Cand@Example.com")
    st = google_auth.status(cfg)
    assert st["authorized"] and st["mail_scope"] and st["calendar_scope"]
    assert st["account_matches"] is True                   # case-insensitive
    _write_token(google_auth.token_path(cfg), [CAL], account="cand@example.com")
    st = google_auth.status(cfg)
    assert st["mail_scope"] is False and st["calendar_scope"] is True   # granted set, not requested set
    # Someone else's token: scopes on disk no longer matter — nothing lights up (see
    # test_foreign_token_file_is_not_connected for the full consumer chain).
    _write_token(google_auth.token_path(cfg), [CAL], account="sample-a8074080@example.com")
    st = google_auth.status(cfg)
    assert st["account_matches"] is False and st["account_blocked"] is True
    assert st["mail_scope"] is False and st["calendar_scope"] is False


def test_credentials_none_on_missing_or_garbage(tmp_path):
    cfg = _ws_cfg(tmp_path)
    assert google_auth.credentials(cfg) is None
    tp = google_auth.token_path(cfg)
    tp.parent.mkdir(parents=True)
    tp.write_text("{not json")
    assert google_auth.credentials(cfg) is None
    assert google_auth.status(cfg)["authorized"] is False
    tp.write_text(json.dumps({"token": "x"}))              # no refresh_token → useless
    assert google_auth.credentials(cfg) is None
    assert google_auth.access_token(cfg) is None


def test_finish_flow_rejects_unknown_state(tmp_path):
    with pytest.raises(ValueError, match="unknown or expired state"):
        google_auth.finish_flow(_ws_cfg(tmp_path), "nope", "code")


# ── finish_flow account check ─────────────────────────────────────────────────────────────

class _FlowCreds:
    """What flow.credentials hands back after fetch_token: full scopes, a refresh token."""
    scopes = granted_scopes = [MAIL, CAL, "openid"]
    token, refresh_token = "at", "rt-fresh"

    def to_json(self):
        return json.dumps({"token": self.token, "refresh_token": self.refresh_token,
                           "client_id": "c", "client_secret": "s", "scopes": self.scopes})


class _FakeFlow:
    def __init__(self):
        self.credentials = _FlowCreds()

    def fetch_token(self, code):
        self.code = code


def _arm_flow(monkeypatch, userinfo_email):
    """Register a pending state and stub the two network calls: userinfo → `userinfo_email`,
    revoke → recorded. Returns (state, revoked_tokens)."""
    revoked = []
    monkeypatch.setattr(google_auth, "_fetch_email", lambda creds: userinfo_email)
    monkeypatch.setattr(google_auth, "_revoke_remote", lambda tok: revoked.append(tok))
    monkeypatch.setitem(google_auth._PENDING, "st1", (_FakeFlow(), 9e12))   # far-future start → never purged
    return "st1", revoked


def test_finish_flow_refuses_wrong_account_and_revokes(monkeypatch, tmp_path):
    """The wrong-Chrome-profile case: the OWNER's account consented inside the candidate's
    workspace. Nothing may be written — that file would be her full Gmail+Calendar refresh
    token under data/<name>/secrets/, and his scheduling replies would quote her free/busy."""
    cfg = _ws_cfg(tmp_path)
    state, revoked = _arm_flow(monkeypatch, "sample-377dfc9c@example.com")
    with pytest.raises(ValueError) as ei:
        google_auth.finish_flow(cfg, state, "code")
    msg = str(ei.value)
    assert "sample-377dfc9c@example.com" in msg and "cand@example.com" in msg      # names both accounts
    assert not google_auth.token_path(cfg).exists()
    assert revoked == ["rt-fresh"]                                      # grant handed back to Google
    st = google_auth.status(cfg)
    assert st["authorized"] is False and st["mail_scope"] is False and st["calendar_scope"] is False
    assert mailcode.available(cfg) is False and google_auth.credentials(cfg) is None
    assert state not in google_auth._PENDING                            # one-shot state, consumed


def test_finish_flow_refuses_unknown_account(monkeypatch, tmp_path):
    """userinfo failed → we cannot say whose token this is → same refusal (a token we cannot
    attribute must not be trusted when the workspace names a mailbox)."""
    cfg = _ws_cfg(tmp_path)
    state, revoked = _arm_flow(monkeypatch, None)
    with pytest.raises(ValueError, match="cand@example.com"):
        google_auth.finish_flow(cfg, state, "code")
    assert not google_auth.token_path(cfg).exists()
    assert revoked == ["rt-fresh"]


def test_finish_flow_saves_matching_account_case_insensitive(monkeypatch, tmp_path):
    cfg = _ws_cfg(tmp_path)
    state, revoked = _arm_flow(monkeypatch, "Cand@Example.COM")
    st = google_auth.finish_flow(cfg, state, "code")
    assert revoked == []
    assert st["authorized"] and st["mail_scope"] and st["calendar_scope"]
    assert st["account"] == "Cand@Example.COM" and st["account_matches"] is True
    assert st["account_blocked"] is False
    assert google_auth.token_path(cfg).exists()
    assert mailcode.available(cfg) is True


def test_finish_flow_without_expected_account_keeps_anonymous_token(monkeypatch, tmp_path):
    """No mailcode.email / profile.email → nothing to compare against → the old behaviour."""
    cfg = _ws_cfg(tmp_path)
    cfg["mailcode"] = {"enabled": True}
    cfg["profile"] = {}
    state, revoked = _arm_flow(monkeypatch, None)
    st = google_auth.finish_flow(cfg, state, "code")
    assert revoked == [] and st["authorized"] and st["mail_scope"]
    assert st["account"] is None and st["account_matches"] is None and st["account_blocked"] is False


# ── defense in depth: a hand-copied foreign token is "not connected" ──────────────────────

def test_foreign_token_file_is_not_connected(tmp_path, monkeypatch):
    """Someone copies the owner's token into data/<name>/secrets/: status/credentials/mailcode/
    gcal must all treat it as absent, whatever scopes it carries."""
    cfg = _ws_cfg(tmp_path)
    _write_token(google_auth.token_path(cfg), [MAIL, CAL], account="sample-377dfc9c@example.com")
    st = google_auth.status(cfg)
    assert st["authorized"] is True                       # the file IS there — the page must say why it is ignored
    assert st["account_matches"] is False and st["account_blocked"] is True
    assert st["mail_scope"] is False and st["calendar_scope"] is False
    assert google_auth.credentials(cfg) is None and google_auth.access_token(cfg) is None
    assert mailcode.available(cfg) is False and mailcode._accounts(cfg) == []
    _mc, _addr, pw = mailcode._cfg(cfg)
    assert pw == ""                                        # never an OAuthSecret for a stranger's token
    monkeypatch.setattr(gcal, "TOKEN_PATH", str(tmp_path / "missing_legacy.json"))
    assert gcal._service(cfg) is None                      # not the calendar client either


def test_token_without_account_is_not_connected_when_workspace_names_mailbox(tmp_path):
    cfg = _ws_cfg(tmp_path)
    _write_token(google_auth.token_path(cfg), [MAIL, CAL], account=None)
    st = google_auth.status(cfg)
    assert st["authorized"] is True and st["account_blocked"] is True
    assert st["account_matches"] is None                   # unknown, not "wrong" — the page words it differently
    assert st["mail_scope"] is False and st["calendar_scope"] is False
    assert google_auth.credentials(cfg) is None and mailcode.available(cfg) is False


def test_matching_token_file_stays_connected(tmp_path):
    """The positive control for the two tests above: same file, right account → everything on."""
    cfg = _ws_cfg(tmp_path)
    _write_token(google_auth.token_path(cfg), [MAIL, CAL], account="CAND@example.com")
    st = google_auth.status(cfg)
    assert st["account_blocked"] is False and st["mail_scope"] and st["calendar_scope"]
    creds = google_auth.credentials(cfg)
    assert creds is not None and creds.valid and google_auth.access_token(cfg) == "at"
    assert mailcode.available(cfg) is True


class _SaveCreds:
    scopes = [MAIL, CAL, "openid"]
    granted_scopes = [CAL, "openid"]                       # the person unticked Gmail on the consent page

    def to_json(self):
        return json.dumps({"token": "t", "refresh_token": "r", "client_id": "c",
                           "client_secret": "s", "scopes": self.scopes})


def test_save_records_granted_scopes_with_tight_permissions(tmp_path):
    cfg = _ws_cfg(tmp_path)
    google_auth._save(cfg, _SaveCreds(), "cand@example.com")
    tp = google_auth.token_path(cfg)
    assert stat.S_IMODE(tp.stat().st_mode) == 0o600
    assert stat.S_IMODE(tp.parent.stat().st_mode) == 0o700
    st = google_auth.status(cfg)
    assert st["calendar_scope"] is True and st["mail_scope"] is False   # granted, not requested
    assert st["account"] == "cand@example.com" and st["account_matches"] is True


# ── login dispatch ────────────────────────────────────────────────────────────────────────

class _FakeIMAP:
    def __init__(self):
        self.calls = []

    def login(self, addr, pw):
        self.calls.append(("login", addr, pw))

    def authenticate(self, mech, authobject):
        self.calls.append(("authenticate", mech, authobject(b"")))


class _FakeSMTP:
    def __init__(self, code=235):
        self.calls, self.code = [], code

    def ehlo(self):
        self.calls.append(("ehlo",))

    def login(self, addr, pw):
        self.calls.append(("login", addr, pw))

    def docmd(self, cmd, arg=""):
        self.calls.append(("docmd", cmd, arg))
        return self.code, b"ok"


def test_imap_login_dispatch(monkeypatch, tmp_path):
    cfg = _ws_cfg(tmp_path)
    monkeypatch.setattr(google_auth, "access_token", lambda c: "TOK")
    M = _FakeIMAP()
    mail.imap_login(M, "cand@example.com", mailcode.OAuthSecret(cfg, "cand@example.com"))
    assert M.calls == [("authenticate", "XOAUTH2", b"user=cand@example.com\x01auth=Bearer TOK\x01\x01")]
    M2 = _FakeIMAP()
    mail.imap_login(M2, "sample-13a740fc@example.com", "app-pass")             # plain password → the old path, untouched
    assert M2.calls == [("login", "sample-13a740fc@example.com", "app-pass")]


def test_smtp_login_dispatch(monkeypatch, tmp_path):
    cfg = _ws_cfg(tmp_path)
    monkeypatch.setattr(google_auth, "access_token", lambda c: "TOK")
    s = _FakeSMTP()
    mail.smtp_login(s, "cand@example.com", mailcode.OAuthSecret(cfg, "cand@example.com"))
    want = base64.b64encode(b"user=cand@example.com\x01auth=Bearer TOK\x01\x01").decode()
    assert s.calls == [("ehlo",), ("docmd", "AUTH", "XOAUTH2 " + want)]
    s2 = _FakeSMTP()
    mail.smtp_login(s2, "sample-13a740fc@example.com", "app-pass")
    assert s2.calls == [("login", "sample-13a740fc@example.com", "app-pass")]
    with pytest.raises(smtplib.SMTPAuthenticationError):
        mail.smtp_login(_FakeSMTP(code=535), "cand@example.com", mailcode.OAuthSecret(cfg, "cand@example.com"))


def test_oauthsecret_is_truthy_never_leaks_and_fails_loud(tmp_path):
    sec = mailcode.OAuthSecret(_ws_cfg(tmp_path), "cand@example.com")
    assert bool(sec) is True and "cand@example.com" in repr(sec)
    with pytest.raises(RuntimeError, match="/settings"):       # no token file → clear message
        sec.token()


# ── mailcode.available across workspaces ──────────────────────────────────────────────────

def test_mailcode_available_workspace_without_password_or_token(tmp_path):
    cfg = _ws_cfg(tmp_path)
    assert mailcode.available(cfg) is False
    _mc, addr, pw = mailcode._cfg(cfg)
    assert addr == "cand@example.com" and pw == ""
    assert mailcode._accounts(cfg) == []


def test_mailcode_available_via_oauth_token(tmp_path):
    cfg = _ws_cfg(tmp_path)
    _write_token(google_auth.token_path(cfg), [MAIL, CAL])
    _mc, addr, pw = mailcode._cfg(cfg)
    assert isinstance(pw, mailcode.OAuthSecret) and pw.addr == "cand@example.com"
    assert mailcode.available(cfg) is True
    assert [a for _m, a, _p in mailcode._accounts(cfg)] == ["cand@example.com"]
    _write_token(google_auth.token_path(cfg), [CAL])       # calendar-only token → mailbox stays off
    assert mailcode.available(cfg) is False
    cfg["mailcode"]["enabled"] = False                     # switch honoured even with a full token
    _write_token(google_auth.token_path(cfg), [MAIL, CAL])
    assert mailcode.available(cfg) is False


def test_robin_workspace_is_quiet_without_token(tmp_path):
    """The real second workspace inherits config.yaml, and secrets.yaml (one shared file)
    holds the OWNER's app password — it must not light up his mailbox with her password."""
    c = cfgmod.load_config("config.robin.yaml")
    assert c.get(cfgmod.EXTENDS_KEY) == "config.yaml"
    assert google_auth.token_path(c) == cfgmod.ROOT / "data" / "robin" / "secrets" / "google_token.json"
    c["google"] = {"token": str(tmp_path / "none.json")}   # ignore whatever the machine has
    assert mailcode.available(c) is False
    assert mailcode._accounts(c) == []


def test_owner_config_keeps_app_password_path():
    """No EXTENDS_KEY → the shared gmail_app_password is hers and stays a plain str (whatever
    the machine has in secrets.yaml, possibly '')."""
    c = cfgmod.load_config("config.yaml")
    assert cfgmod.EXTENDS_KEY not in c
    _mc, _addr, pw = mailcode._cfg(c)
    assert isinstance(pw, str)


# ── gcal prefers the workspace token ──────────────────────────────────────────────────────

class _FakeCreds:
    valid = True
    scopes = [CAL]
    token = "x"


def test_gcal_service_prefers_workspace_token(monkeypatch):
    import googleapiclient.discovery as disc
    fake, seen = _FakeCreds(), {}
    monkeypatch.setattr(google_auth, "credentials", lambda cfg, scopes=None: fake)
    monkeypatch.setattr(disc, "build",
                        lambda name, ver, credentials=None, **kw: seen.update(name=name, creds=credentials) or "SVC")
    assert gcal._service({"user_db_path": "x"}) == "SVC"
    assert seen == {"name": "calendar", "creds": fake}


def test_gcal_service_skips_workspace_token_without_calendar_scope(monkeypatch, tmp_path):
    import googleapiclient.discovery as disc
    fake = _FakeCreds()
    fake.scopes = [MAIL]
    monkeypatch.setattr(google_auth, "credentials", lambda cfg, scopes=None: fake)
    monkeypatch.setattr(disc, "build", lambda *a, **kw: "SVC")
    monkeypatch.setattr(gcal, "TOKEN_PATH", str(tmp_path / "missing_legacy.json"))
    assert gcal._service({"user_db_path": "x"}) is None
