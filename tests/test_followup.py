"""Follow-ups: who surfaces, when, and the ceilings that stop it becoming spam."""
import datetime

from app import followup, store
from tests.test_store import role_row


def _seed(cfg, *, job_id="gh:x:1", score=80.0, days_ago=30, **state):
    conn = store.connect_view(cfg)
    conn.execute("INSERT OR REPLACE INTO jobs.roles (" + ", ".join(store.ROLE_COLS) + ") VALUES ("
                 + ", ".join(f":{c}" for c in store.ROLE_COLS) + ")",
                 role_row(job_id=job_id, score=score))
    conn.commit()
    when = (datetime.date.fromisoformat(store.la_today())
            - datetime.timedelta(days=days_ago)).isoformat()
    store.upsert_state(conn, job_id, status="applied", applied_date=when, **state)
    return conn


def test_a_fresh_application_is_not_chased(cfg):
    conn = _seed(cfg, days_ago=3)
    assert followup.quiet(cfg, conn) == []


def test_a_quiet_application_surfaces(cfg):
    conn = _seed(cfg, days_ago=30)
    rows = followup.quiet(cfg, conn)
    assert [r["job_id"] for r in rows] == ["gh:x:1"]
    assert rows[0]["days_quiet"] == 30


def test_an_answered_application_is_never_chased(cfg):
    conn = _seed(cfg, days_ago=30)
    store.record_signal(conn, "gh:x:1", stage="responded")
    assert followup.quiet(cfg, conn) == []


def test_the_second_nudge_waits_for_the_gap_and_the_third_never_comes(cfg):
    conn = _seed(cfg, days_ago=60)
    store.followup_add(conn, "gh:x:1", channel="email", to_addr=None, subject="s", body="b")
    # drafted today, so the gap hasn't elapsed
    assert followup.quiet(cfg, conn) == []

    conn.execute("UPDATE followups SET drafted_at=? WHERE job_id='gh:x:1' AND seq=1",
                 ((datetime.date.fromisoformat(store.la_today())
                   - datetime.timedelta(days=20)).isoformat(),))
    conn.commit()
    assert len(followup.quiet(cfg, conn)) == 1              # gap elapsed → second nudge is due

    store.followup_add(conn, "gh:x:1", channel="email", to_addr=None, subject="s", body="b")
    conn.execute("UPDATE followups SET drafted_at='2020-01-01' WHERE job_id='gh:x:1'")
    conn.commit()
    assert followup.quiet(cfg, conn) == []                  # ceiling reached, however long it's been


def test_ranked_by_score_and_capped(cfg):
    cfg = {**cfg, "followup": {**followup.DEFAULTS, "top": 2}}
    conn = None
    for i, score in enumerate([60.0, 95.0, 80.0, 70.0]):
        conn = _seed(cfg, job_id=f"gh:x:{i}", score=score, days_ago=30)
    rows = followup.quiet(cfg, conn)
    assert [r["weighted_score"] for r in rows] == [95.0, 80.0]


def test_min_score_filter(cfg):
    cfg = {**cfg, "followup": {**followup.DEFAULTS, "min_score": 75}}
    conn = _seed(cfg, job_id="gh:x:lo", score=60.0, days_ago=30)
    conn = _seed(cfg, job_id="gh:x:hi", score=90.0, days_ago=30)
    assert [r["job_id"] for r in followup.quiet(cfg, conn)] == ["gh:x:hi"]


def test_marketing_addresses_are_not_contacts(cfg):
    """sample-50533ce0@example.com is a mailing list. Resolving it would look like success and reach nobody."""
    for addr in ["sample-50533ce0@example.com", "sample-f5955c7a@example.com", "sample-8c044424@example.com",
                 "sample-f532d4b9@example.com", "sample-449e2cb7@example.com", "sample-ec57cd76@example.com"]:
        assert followup._NOT_A_PERSON.search(addr), addr
    for addr in ["sample-4ebc23ee@example.com", "sample-a213a9c0@example.com", "sample-09b4ccdf@example.com"]:
        assert not followup._NOT_A_PERSON.search(addr), addr


def test_contact_resolution_prefers_the_triage_queue(cfg):
    conn = store.connect_view(cfg)
    store.queue_add(conn, {
        "msg_id": "<1@x>", "uid": "1", "from_addr": "Ann <sample-c4047c3b@example.com>",
        "from_name": "Ann", "reply_to": "sample-c4047c3b@example.com", "subject": "Your application",
        "snippet": "", "category": "recruiter", "asks_availability": 0, "repliable": 1,
        "draft": "", "references_hdr": "", "in_reply_to": "<1@x>", "status": "pending",
        "found_at": "2026-07-01", "sent_at": None})
    c = followup.resolve_contact(cfg, conn, "Stripe")
    assert c and c["addr"] == "sample-c4047c3b@example.com" and "triage queue" in c["source"]


def test_no_contact_is_a_valid_answer(cfg, monkeypatch):
    """None means 'send it through the portal', not 'give up' — and it must never invent an
    address. The mailbox is stubbed: no test in this suite touches the real inbox."""
    monkeypatch.setattr(followup.mail, "search_recent", lambda *a, **k: [])
    conn = store.connect_view(cfg)
    assert followup.resolve_contact(cfg, conn, "Nobody Inc") is None


def test_mailbox_fallback_ignores_mail_that_is_not_about_the_application(cfg, monkeypatch):
    """The company's newsletter mentions the company. That is not evidence of a recruiter."""
    monkeypatch.setattr(followup.mail, "search_recent", lambda *a, **k: [
        {"uid": "1", "from": "Nvidia News <sample-50533ce0@example.com>", "subject": "Nvidia GTC keynote"},
        {"uid": "2", "from": "Nvidia Events <sample-3c22c977@example.com>", "subject": "Nvidia webinar"},
    ])
    monkeypatch.setattr(followup.mail, "get_message",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not fetch")))
    conn = store.connect_view(cfg)
    assert followup.resolve_contact(cfg, conn, "Nvidia") is None
