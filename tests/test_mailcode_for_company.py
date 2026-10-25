"""A code only goes into a form when it clearly came from that company, once."""
import datetime

import pytest

from app import mailcode

NOW = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(mailcode.cfgmod, "ROOT", tmp_path)
    monkeypatch.setattr(mailcode.cfgmod, "workspace_name", lambda _cfg: "test")
    monkeypatch.setattr(mailcode, "_accounts", lambda _cfg: [("box", "a", "b")])
    return {}


def mail(subject="Security code for your application to Acme Labs", sender="sample-210b2f5f@example.com", code="dW9hkRYX", mail_id="m1"):
    return {"id": mail_id, "arrived": NOW, "subject": subject, "from": sender, "code": code}


def test_this_companys_code_is_returned_and_used_once(cfg):
    scan = lambda account, after: [mail()]
    assert mailcode.fetch_code_for(cfg, "Acme Labs", NOW, scan=scan)["code"] == "dW9hkRYX"
    assert mailcode.fetch_code_for(cfg, "Acme Labs", NOW, scan=scan) == {"code": None, "matches": 0}


def test_another_companys_code_never_counts(cfg):
    scan = lambda account, after: [mail(subject="Security code for your application to Beta")]
    assert mailcode.fetch_code_for(cfg, "Acme Labs", NOW, scan=scan) == {"code": None, "matches": 0}


def test_two_different_codes_stop_instead_of_guessing(cfg):
    scan = lambda account, after: [mail(code="AAA111"), mail(code="BBB222", mail_id="m2")]
    assert mailcode.fetch_code_for(cfg, "Acme Labs", NOW, scan=scan) == {"code": None, "matches": 2}


def test_the_sender_can_be_what_names_the_company(cfg):
    scan = lambda account, after: [mail(subject="Your one-time code", sender="Acme Labs <careers@acme.example>")]
    assert mailcode.fetch_code_for(cfg, "Acme Labs", NOW, scan=scan)["code"] == "dW9hkRYX"


@pytest.mark.parametrize("company, subject, sender", [
    ("Exa", "Your exam code", "no-reply@example.com"),
    ("Exa", "Security code for your application to Examplify", "sample-210b2f5f@example.com"),
    ("Exa", "Your one-time code", "Exact Sciences <jobs@exactsciences.example>"),
    ("Fal", "Your fall hiring code", "sample-210b2f5f@example.com"),
    ("Fal", "Your one-time code", "Falcon Labs <jobs@falconlabs.example>"),
    ("Fal", "Security code: this is not a false alarm", "sample-210b2f5f@example.com"),
    ("Acme Labs", "Security code for your application to Acme Laboratories", "sample-210b2f5f@example.com"),
    ("Acme Labs", "Security code for your application to Pacme Labs", "sample-210b2f5f@example.com"),
])
def test_a_name_inside_another_word_never_counts(cfg, company, subject, sender):
    scan = lambda account, after: [mail(subject=subject, sender=sender)]
    assert mailcode.fetch_code_for(cfg, company, NOW, scan=scan) == {"code": None, "matches": 0}


@pytest.mark.parametrize("company, subject, sender", [
    ("Exa", "Security code for your application to Exa", "sample-210b2f5f@example.com"),
    ("Exa", "Your one-time code", "Exa <sample-be7dfb1a@example.com>"),
    ("EXA", "Your one-time code", "sample-3bdbc659@example.com"),
    ("Fal", "Security code for your application to fal", "sample-210b2f5f@example.com"),
    ("Fal", "Your one-time code", "sample-6a1a2e24@example.com"),
    ("Acme Labs", "Security code for your application to Acme Labs", "sample-210b2f5f@example.com"),
    ("Acme Labs", "Your one-time code", "Acme Labs <careers@acme.example>"),
    ("Acme Labs", "Your one-time code", "careers@acme-labs.example"),
    ("Acme Labs", "Your one-time code", "careers@acmelabs.example"),
    ("Scale AI", "Your one-time code", "sample-7ea932bf@example.com"),
    ("AT&T", "Your code from AT&T", "sample-210b2f5f@example.com"),
])
def test_the_name_standing_alone_counts(cfg, company, subject, sender):
    scan = lambda account, after: [mail(subject=subject, sender=sender)]
    assert mailcode.fetch_code_for(cfg, company, NOW, scan=scan)["code"] == "dW9hkRYX"


@pytest.mark.parametrize("company", ["Ro", "ro", "R.O.", "X", "  "])
@pytest.mark.parametrize("subject, sender", [
    ("Your one-time code", "Microsoft <sample-0ffab23b@example.com>"),
    ("Security code for your application to Ro", "sample-210b2f5f@example.com"),
    ("Your one-time code", "Ro <sample-6e4ac22b@example.com>"),
])
def test_a_name_under_three_characters_never_matches_anything(cfg, company, subject, sender):
    scan = lambda account, after: [mail(subject=subject, sender=sender)]
    assert mailcode.fetch_code_for(cfg, company, NOW, scan=scan) == {"code": None, "matches": 0}
    assert not mailcode._used_path(cfg).exists()


def test_the_name_cannot_straddle_the_subject_and_the_sender(cfg):
    scan = lambda account, after: [mail(subject="Your code from Acme", sender="Labs <jobs@beta.example>")]
    assert mailcode.fetch_code_for(cfg, "Acme Labs", NOW, scan=scan) == {"code": None, "matches": 0}


class TestTheRoute:
    """/api/email-code answers the extension on this machine, and nothing else."""

    @pytest.fixture
    def route(self, monkeypatch):
        from fastapi.testclient import TestClient
        from app import web
        calls = []
        # Stubbed before any request: nothing here may touch a real mailbox or its credential.
        monkeypatch.setattr(web, "cfg", {})
        monkeypatch.setattr(web.mailmod, "available", lambda _cfg: True)
        monkeypatch.setattr(web.mailmod, "fetch_code_for", lambda _cfg, company, after: calls.append(company) or {"code": "dW9hkRYX"})
        monkeypatch.setattr(web.mailmod, "fetch_latest_code_any", lambda _cfg: calls.append(None) or {"code": "dW9hkRYX"})
        return TestClient(web.app), calls

    @pytest.mark.parametrize("host", ["127.0.0.1:8787", "localhost:8787", "127.0.0.1:8788"])
    def test_the_extension_gets_an_answer(self, route, host):
        client, calls = route
        reply = client.get("/api/email-code", params={"company": "Acme Labs"},
                           headers={"Host": host, "Origin": "chrome-extension://" + "a" * 32, "Sec-Fetch-Site": "none"})
        assert reply.status_code == 200 and reply.json()["code"] == "dW9hkRYX" and calls == ["Acme Labs"]

    @pytest.mark.parametrize("params", [{}, {"company": "Acme Labs"}])
    @pytest.mark.parametrize("host", ["evil.example", "evil.example:8787", "127.0.0.1.evil.example:8787", "localhost.evil.example"])
    def test_a_rebound_page_on_another_host_gets_nothing(self, route, params, host):
        # DNS rebinding: the page is same-origin to the browser and a GET sends no Origin.
        client, calls = route
        reply = client.get("/api/email-code", params=params, headers={"Host": host, "Sec-Fetch-Site": "same-origin"})
        assert reply.status_code == 403 and "dW9hkRYX" not in reply.text and calls == []

    @pytest.mark.parametrize("headers", [{"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}])
    def test_the_older_guards_still_hold_on_a_local_host(self, route, headers):
        client, calls = route
        reply = client.get("/api/email-code", headers={"Host": "127.0.0.1:8787", **headers})
        assert reply.status_code == 403 and calls == []


def test_nothing_but_a_hash_is_stored(cfg):
    mailcode.fetch_code_for(cfg, "Acme Labs", NOW, scan=lambda account, after: [mail()])
    stored = mailcode._used_path(cfg).read_text()
    assert "dW9hkRYX" not in stored and "m1" not in stored


def test_a_mailbox_that_fails_does_not_hide_the_other(cfg, monkeypatch):
    monkeypatch.setattr(mailcode, "_accounts", lambda _cfg: ["bad", "good"])

    def scan(account, after):
        if account == "bad":
            raise OSError("down")
        return [mail()]
    assert mailcode.fetch_code_for(cfg, "Acme Labs", NOW, scan=scan)["code"] == "dW9hkRYX"


@pytest.mark.parametrize("config, minutes", [({"mailcode": {"within_minutes": 30}}, 30), ({"mailcode": {"within_minutes": "45"}}, 45), ({"mailcode": {}}, 15), ({}, 15),
                                             ({"mailcode": {"within_minutes": 0}}, 15), ({"mailcode": {"within_minutes": -5}}, 15), ({"mailcode": {"within_minutes": "soon"}}, 15)])
def test_the_window_comes_from_config_and_15_is_only_the_default(config, minutes):
    assert mailcode.within_minutes(config) == minutes


@pytest.mark.parametrize("config, minutes", [({"mailcode": {"within_minutes": 30}}, 30), ({}, 15)])
def test_the_route_looks_back_as_far_as_config_says(monkeypatch, config, minutes):
    from fastapi.testclient import TestClient
    from app import web
    seen = []
    monkeypatch.setattr(web, "cfg", config)
    monkeypatch.setattr(web.mailmod, "available", lambda _cfg: True)
    monkeypatch.setattr(web.mailmod, "fetch_code_for", lambda _cfg, company, after: seen.append(after) or {"code": "dW9hkRYX"})
    reply = TestClient(web.app).get("/api/email-code", params={"company": "Acme Labs"},
                                    headers={"Host": "127.0.0.1:8787", "Origin": "chrome-extension://" + "a" * 32})
    assert reply.status_code == 200
    window = (datetime.datetime.now(datetime.timezone.utc) - seen[0]).total_seconds() / 60
    assert minutes - 1 < window < minutes + 1


def test_the_newest_code_search_uses_the_same_window(monkeypatch):
    class Box:
        def __init__(self, _host): pass
        def select(self, *_a, **_k): pass
        def search(self, *_a): return "OK", [b""]
        def logout(self): pass
    cutoffs = []
    from app import mail as mailer
    monkeypatch.setattr(mailcode.imaplib, "IMAP4_SSL", Box)
    monkeypatch.setattr(mailer, "imap_login", lambda *_: None)
    real = mailcode.within_minutes
    monkeypatch.setattr(mailcode, "within_minutes", lambda c: cutoffs.append(real(c)) or real(c))
    assert mailcode.fetch_latest_code({}, account=({"within_minutes": 30}, "a@b.c", "pw")) == {"code": None}
    assert cutoffs == [30]
