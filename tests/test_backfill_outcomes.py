"""Replaying the mailbox into the DB. The dangerous half of this tool is not the classifier —
that is tested in test_outcomes.py — it is deciding WHICH company a message belongs to, and then
writing. The first cut of the tool used substring matching and invented four responses out of
ten from companies she had never heard from; those four are the first tests here.

Every subject line, sender and company name below is real, out of data/mail_label_scan.json and
the reply_queue table.
"""
import json
import sqlite3

import pytest

from app import config as cfgmod, store
from tests.test_store import role_row
from tools import backfill_outcomes as bf


def _msg(date, subject, sender, *, from_me=False):
    return {"date": date, "subject": subject, "from": sender, "from_me": from_me}


def _thread(company, msgs, *, tier="auto", sender=""):
    return {"company": company, "tier": tier, "from": sender,
            "messages": [dict(m) for m in msgs]}


def _by_co(*names):
    """companies applied to, in outcomes._open_roles' shape."""
    out = {}
    for name, n in names:
        co = cfgmod.norm_key(name)
        out[co] = [{"job_id": f"gh:{co}:{i}", "company": name, "company_norm": co,
                    "title": f"Software Engineer {i}", "weighted_score": 80.0,
                    "status": "applied", "applied_date": "2026-07-20", "stage": None,
                    "outcome": None, "first_response_at": None} for i in range(n)]
    return out


# --- whose reply is it -----------------------------------------------------------------------

@pytest.mark.parametrize("thread_company, applied_company", [
    ("Intelliprogroup", "Intel"),            # a staffing agency, not the chipmaker
    ("EvolutionaryScale", "Scale AI"),       # "…aryscaleai" contains "scaleai"
    ("Alex AI", "xAI"),                      # "alexai" contains "xai"
    ("Jack", "Jack & Jill"),                 # an autoapply newsletter, not the employer
])
def test_a_company_whose_name_merely_contains_another_never_claims_its_reply(
        thread_company, applied_company):
    """All four are real (2026-08-10) and all four produced a fabricated "response" the owner
    would have read as proof the machine was working."""
    idx = bf.company_index(_by_co((applied_company, 7)))
    assert bf.roles_for(thread_company, "Next steps", idx) == []


def test_the_company_the_labeller_named_does_resolve():
    idx = bf.company_index(_by_co(("Pinterest", 9)))
    hits = bf.roles_for("Pinterest", "Pinterest Chat | Alex & Crystal", idx)
    assert len(hits) == 9


def test_a_subject_that_names_one_of_her_titles_narrows_it_to_that_requisition():
    """Mastercard mails "An update on your application for Software Engineer II at Mastercard" —
    the one shape of ambiguous-company mail that is not ambiguous at all."""
    by_co = _by_co(("Mastercard", 3))
    by_co["mastercard"][1]["title"] = "Software Engineer II"
    hits = bf.roles_for("Mastercard", "An update on your application for Software Engineer II "
                                      "at Mastercard", bf.company_index(by_co))
    assert [h["title"] for h in hits] == ["Software Engineer II"]


def test_a_reply_queue_row_resolves_by_domain_not_by_the_raw_address():
    """Matching the gazetteer against "sample-a955c521@example.com" filed a Lucid thread under
    LinkedIn once already (mail_label, 2026-08-03). Here it would file a Microsoft InMail under
    her LinkedIn applications."""
    by_co = _by_co(("LinkedIn", 2), ("Microsoft", 9))
    idx, gaz = bf.company_index(by_co), bf.gazetteer(by_co)
    co = bf.resolve_company('"Gayatri J." <sample-a955c521@example.com>',
                            "New Senior Software Engineer opportunity at Microsoft", idx, gaz)
    assert co == "microsoft"          # read off the subject; the relay domain names nothing


def test_a_real_employer_domain_wins():
    by_co = _by_co(("Commure", 7))
    co = bf.resolve_company("Jenna Fonck <sample-46989f91@example.com>",
                            "Commure Interview Confirmation", bf.company_index(by_co),
                            bf.gazetteer(by_co))
    assert co == "commure"


def test_an_all_lowercase_common_word_is_not_a_company():
    """mail_label's rule (2026-08-03: "interview loop" invented a company called Loop)."""
    assert bf._case_ok("Loop", "Loop") is True
    assert bf._case_ok("loop", "Loop") is False
    assert bf._case_ok("fal", "fal") is True          # a name that IS lowercase is exempt


def test_an_unresolvable_sender_is_skipped_rather_than_guessed():
    by_co = _by_co(("Gem", 4))
    assert bf.resolve_company("Team at Visage <sample-cd100810@example.com>",
                              "Follow-Up: Exciting Opportunity at Ford",
                              bf.company_index(by_co), bf.gazetteer(by_co)) is None


# --- the databases ---------------------------------------------------------------------------

@pytest.fixture
def repo(cfg, tmp_path, monkeypatch):
    """A small real world: 12 Airbnb applications, 1 at Commure, an Anthropic pipeline that was
    already open, and a mail cache saying so."""
    conn = store.connect_view(cfg)
    specs = ([("gh:airbnb:%d" % i, "Airbnb", "2026-07-19") for i in range(12)]
             + [("gh:commure:1", "Commure", "2026-08-01")]
             + [("gh:anthropic:%d" % i, "Anthropic", "2026-07-20") for i in range(19)])
    for job_id, company, applied in specs:
        conn.execute("INSERT INTO jobs.roles (" + ", ".join(store.ROLE_COLS) + ") VALUES ("
                     + ", ".join(f":{c}" for c in store.ROLE_COLS) + ")",
                     role_row(job_id=job_id, company=company))
        store.upsert_state(conn, job_id, status="applied", applied_date=applied)
    conn.commit()
    conn.close()

    cache = tmp_path / "scan.json"
    cache.write_text(json.dumps({"threads": [
        _thread("Airbnb", [_msg("2026-07-21",
                                "Next Step: Complete the Airbnb Industry Coding Assessment on "
                                "CodeSignal", "CodeSignal <sample-f8d5ac48@example.com>")]),
        _thread("Commure", [_msg("2026-08-03",
                                 "Commure Interview Confirmation - Software Engineer, RCM Product",
                                 "Jenna Fonck <sample-46989f91@example.com>")]),
        _thread("Anthropic", [_msg("2026-07-17",
                                   "Anthropic invitation to interview for Fellow program",
                                   "sample-210b2f5f@example.com")]),
        _thread("Cisco", [_msg("2026-08-01",
                               "Your Cisco Journey Begins Here – Next Steps for YourApplication",
                               "Cisco <sample-5cc464b5@example.com>")]),
    ]}), encoding="utf-8")
    cfg["mail_label"] = {"cache": str(cache)}
    monkeypatch.setattr(bf, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: cfg)
    return cfg


def test_the_plan_records_the_two_companies_that_answered_and_quarantines_the_third(repo):
    conn = bf.connect_ro(repo)
    res = bf.plan(repo, conn)
    conn.close()
    assert sorted(w["company"] for w in res["writes"]) == ["Airbnb", "Commure"]
    assert {w["scope"] for w in res["writes"]} == {"company", "role"}
    assert [p["company"] for p in res["proposals"] if p["verdict"] == "pre-existing"] == ["Anthropic"]


def test_dry_run_is_the_default_and_writes_nothing(repo, capsys):
    before = _snapshot(repo)
    assert bf.main([]) == 0
    assert _snapshot(repo) == before
    assert "nothing written (dry run" in capsys.readouterr().out


def test_apply_records_the_company_level_response_on_one_of_the_twelve(repo):
    bf.main(["--apply"])
    conn = store.connect_view(repo)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM role_state WHERE first_response_at IS NOT NULL ORDER BY job_id")]
    conn.close()
    airbnb = [r for r in rows if r["job_id"].startswith("gh:airbnb:")]
    assert len(airbnb) == 1                                  # one row carries the company's reply
    assert airbnb[0]["first_response_at"] == "2026-07-21"
    assert airbnb[0]["response_confidence"] == "company"     # …and says so
    commure = [r for r in rows if r["job_id"] == "gh:commure:1"][0]
    assert commure["response_confidence"] == "role"
    assert commure["stage"] == "interview" and commure["status"] == "interviewing"


def test_a_pre_existing_pipeline_is_left_out_of_the_response_count(repo):
    bf.main(["--apply"])
    conn = store.connect_view(repo)
    n = conn.execute("SELECT COUNT(*) FROM role_state WHERE job_id LIKE 'gh:anthropic:%'"
                     "  AND first_response_at IS NOT NULL").fetchone()[0]
    conn.close()
    assert n == 0


def test_apply_snapshots_the_user_db_before_writing(repo, tmp_path):
    bf.main(["--apply"])
    baks = list((tmp_path / "backups").rglob("user_state.db.bak-*"))
    assert len(baks) == 1
    snap = sqlite3.connect(f"file:{baks[0]}?mode=ro", uri=True)
    try:                                                     # a real DB, not a truncated copy
        assert snap.execute("SELECT COUNT(*) FROM role_state").fetchone()[0] == 32
    finally:
        snap.close()


def test_running_it_twice_changes_nothing_the_second_time(repo):
    bf.main(["--apply"])
    first = _snapshot(repo)
    bf.main(["--apply"])
    assert _snapshot(repo) == first


def test_nothing_is_ever_deleted(repo):
    before = _counts(repo)
    bf.main(["--apply"])
    after = _counts(repo)
    assert all(after[t] >= before[t] for t in before)


# --- the interview half ------------------------------------------------------------------------

def test_an_interview_gets_indexed_with_interview_ingests_own_marker(repo):
    bf.main(["--apply"])
    conn = store.connect_view(repo)
    row = conn.execute("SELECT * FROM interviews WHERE company='Commure'").fetchone()
    conn.close()
    assert row["marker_id"] == "2026-08-03-commure"          # interview_ingest._derived_id
    assert row["source"] == "mailbox"                        # visibly inferred, not a 复盘
    assert row["job_id"] == "gh:commure:1"                   # exactly one candidate -> linked
    assert row["sources"] == "{}"                            # no file on disk backs it


def test_a_company_already_in_the_review_index_is_not_indexed_twice(repo):
    """The mailbox calls it Lucid, her 复盘 calls it Lucid Motors, and the 07-28 invite and the
    07-30 review are the same interview."""
    conn = store.connect_view(repo)
    store.interview_upsert(conn, marker_id="2026-08-04-commure-hm", company="Commure Inc",
                           happened_on="2026-08-04", round_type="HM", sources="{}")
    conn.close()
    conn = bf.connect_ro(repo)
    res = bf.plan(repo, conn)
    conn.close()
    assert "Commure" not in [i["company"] for i in res["interviews"]]


def test_a_review_written_later_takes_the_row_over(repo):
    bf.main(["--apply"])
    conn = store.connect_view(repo)
    store.interview_upsert(conn, marker_id="2026-08-03-commure", company="Commure",
                           happened_on="2026-08-03", round_type="Recruiter Screen",
                           sources='{"Interview Action Items.md": "Commure · 2026-08-03"}')
    row = conn.execute("SELECT * FROM interviews WHERE marker_id='2026-08-03-commure'").fetchone()
    conn.close()
    assert row["source"] == "review-md"
    assert row["round_type"] == "Recruiter Screen"
    assert row["job_id"] == "gh:commure:1"                   # the link survives


def test_a_calendar_subject_becomes_a_readable_round_name():
    assert bf._round_label("Invitation from an unknown sender: Pinterest Chat | Alex & Crystal "
                           "@ Wed Jul 29, 2026 11am - 11:20am (PDT) (sample-6b7f1a71@example.com)") \
        == "Pinterest Chat | Alex & Crystal"


# --- helpers -----------------------------------------------------------------------------------

_TABLES = ("role_state", "interviews", "interview_items", "reply_queue", "followups")


def _counts(cfg):
    conn = bf.connect_ro(cfg)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _TABLES}
    finally:
        conn.close()


def _snapshot(cfg):
    conn = bf.connect_ro(cfg)
    try:
        return (sorted(tuple(r) for r in conn.execute(
                    "SELECT job_id, status, stage, outcome, first_response_at,"
                    " response_confidence, outcome_note FROM role_state")),
                sorted(tuple(r) for r in conn.execute(
                    "SELECT marker_id, company, happened_on, round_type, job_id, source"
                    "  FROM interviews")))
    finally:
        conn.close()


def test_responses_recorded_before_the_column_existed_are_labelled_role_level(repo):
    """The old writer only ever committed a signal when the company had exactly one open
    application, so its rows are role-level by construction. Left NULL they would read as
    "responses we know nothing about"."""
    conn = store.connect_view(repo)
    store.upsert_state(conn, "gh:airbnb:5", first_response_at="2026-07-25",
                       response_confidence=None)
    conn.close()
    bf.main(["--apply"])
    conn = bf.connect_ro(repo)
    row = conn.execute("SELECT response_confidence FROM role_state"
                       " WHERE job_id='gh:airbnb:5'").fetchone()
    n_null = conn.execute("SELECT COUNT(*) FROM role_state WHERE first_response_at IS NOT NULL"
                          "   AND response_confidence IS NULL").fetchone()[0]
    conn.close()
    assert row["response_confidence"] == "role"
    assert n_null == 0
