"""Outcome tracking: the schema migration, the monotonicity rules, and the funnel's refusal to
report a rate it can't support."""
import datetime

from app import funnel, store
from tests.test_store import role_row


def _applied(cfg, *, job_id="gh:x:1", score=80.0, days_ago=30):
    conn = store.connect_view(cfg)
    conn.execute("INSERT INTO jobs.roles (" + ", ".join(store.ROLE_COLS) + ") VALUES ("
                 + ", ".join(f":{c}" for c in store.ROLE_COLS) + ")",
                 role_row(job_id=job_id, score=score))
    conn.commit()
    when = (datetime.date.fromisoformat(store.la_today())
            - datetime.timedelta(days=days_ago)).isoformat()
    store.upsert_state(conn, job_id, status="applied", applied_date=when)
    return conn


def test_migration_adds_columns_to_an_existing_table(cfg):
    """An old user_state.db predates these columns; connect_user must ALTER them in without
    touching a single existing status."""
    conn = store.connect_user(cfg)
    conn.executescript("DROP TABLE role_state;"
                       "CREATE TABLE role_state (job_id TEXT PRIMARY KEY, status TEXT,"
                       " status_updated_at TEXT, applied_date TEXT, applied_with_resume TEXT,"
                       " notes TEXT, tailor_status TEXT, prompt_path TEXT,"
                       " tailored_resume_path TEXT);")
    conn.execute("INSERT INTO role_state (job_id, status) VALUES ('gh:x:1','applied')")
    conn.commit()
    conn.close()

    conn = store.connect_user(cfg)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(role_state)")}
    assert {"stage", "outcome", "outcome_note", "first_response_at", "last_contact_at"} <= cols
    assert conn.execute("SELECT status FROM role_state WHERE job_id='gh:x:1'").fetchone()[0] == "applied"


def test_an_unrelated_write_does_not_wipe_the_outcome_columns(cfg):
    """upsert_state uses INSERT OR REPLACE, so a column missing from _STATE_COLS would be
    silently nulled on the next status change. This is that regression."""
    conn = _applied(cfg)
    store.record_signal(conn, "gh:x:1", stage="interview", note="phone screen")
    store.set_notes(conn, "gh:x:1", "some unrelated note")
    row = conn.execute("SELECT * FROM role_state WHERE job_id='gh:x:1'").fetchone()
    assert row["stage"] == "interview"
    assert row["first_response_at"]


def test_stage_only_ever_advances(cfg):
    conn = _applied(cfg)
    store.record_signal(conn, "gh:x:1", stage="onsite")
    store.record_signal(conn, "gh:x:1", stage="responded")     # a later, lesser signal
    assert conn.execute("SELECT stage FROM role_state WHERE job_id='gh:x:1'").fetchone()[0] == "onsite"


def test_first_response_is_written_once(cfg):
    conn = _applied(cfg)
    store.record_signal(conn, "gh:x:1", stage="responded", when="2026-07-01")
    store.record_signal(conn, "gh:x:1", stage="interview", when="2026-07-20")
    row = conn.execute("SELECT * FROM role_state WHERE job_id='gh:x:1'").fetchone()
    assert row["first_response_at"] == "2026-07-01"    # the FIRST reply, not the latest
    assert row["last_contact_at"] == "2026-07-20"      # which does move


def test_notes_append_rather_than_overwrite(cfg):
    conn = _applied(cfg)
    store.record_signal(conn, "gh:x:1", note="recruiter call", when="2026-07-01")
    store.record_signal(conn, "gh:x:1", note="rejected", when="2026-07-09")
    note = conn.execute("SELECT outcome_note FROM role_state WHERE job_id='gh:x:1'").fetchone()[0]
    assert "recruiter call" in note and "rejected" in note


def test_rejection_closes_the_row_and_counts_as_a_response(cfg):
    conn = _applied(cfg)
    store.record_signal(conn, "gh:x:1", outcome="rejected")
    row = conn.execute("SELECT * FROM role_state WHERE job_id='gh:x:1'").fetchone()
    assert row["status"] == "rejected" and row["outcome"] == "rejected"
    assert row["first_response_at"]              # a rejection IS a reply, and the funnel counts it


def test_funnel_holds_out_applications_too_young_to_judge(cfg):
    conn = _applied(cfg, job_id="gh:x:1", days_ago=2)
    rep = funnel.report(cfg, conn, ["score"])
    assert rep["overall"]["applied"] == 1
    assert rep["overall"]["mature"] == 0          # 2 days of silence means nothing
    assert rep["overall"]["response_rate"] is None


def test_funnel_refuses_to_call_a_trend_on_a_tiny_sample(cfg):
    conn = _applied(cfg, job_id="gh:x:1", days_ago=60)
    store.record_signal(conn, "gh:x:1", stage="interview")
    rep = funnel.report(cfg, conn, ["score"])
    assert rep["overall"]["responded"] == 1
    assert rep["signal"]["verdict"] == "not enough data"
    assert rep["by"]["score"][0]["enough"] is False


def test_match_score_is_read_back_off_the_pdf_filename():
    assert funnel._match_score("Casey Example - Stripe - Money [6176758] - 80.pdf") == 80
    assert funnel._match_score("no score here.pdf") is None
    assert funnel._match_score(None) is None
