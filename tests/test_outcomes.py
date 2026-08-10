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


# --- company-level attribution ---------------------------------------------------------------
#
# Every subject line below is a real one out of data/mail_label_scan.json. These are the cases
# the old writer got wrong: it committed a signal only when the company had exactly one open
# application, so at Nvidia (39), Mastercard (38), Anthropic (19) and Airbnb (12) it wrote
# nothing at all and the funnel reported a response rate that was a property of the plumbing.

from app import outcomes                                            # noqa: E402
from app import config as cfgmod                                    # noqa: E402


def _msg(date, subject, sender, *, from_me=False):
    return {"date": date, "subject": subject, "from": sender, "from_me": from_me}


def _thread(company, msgs, *, tier="auto", sender=""):
    return {"company": company, "tier": tier, "from": sender,
            "messages": [dict(m) for m in msgs]}


def _roles(company, n, applied="2026-07-19", score=80.0):
    """`n` open applications at one company, in outcomes._open_roles' shape."""
    return {cfgmod.norm_key(company): [
        {"job_id": f"gh:{cfgmod.norm_key(company)}:{i}", "company": company,
         "company_norm": cfgmod.norm_key(company), "title": f"Software Engineer {i}",
         "weighted_score": score, "status": "applied", "applied_date": applied,
         "stage": None, "outcome": None, "first_response_at": None} for i in range(n)]}


def _propose(company, msgs, roles, *, threads=None):
    """The whole pipeline on one company: classify -> signals -> proposals."""
    ts = threads or [_thread(company, msgs)]
    sigs = outcomes.company_signals(ts)
    flat = []
    for m in msgs:
        sig = outcomes.signal_of(m["subject"])
        if outcomes._RANK.get(sig, -1) < outcomes._REAL and outcomes._BOOKED.search(m["subject"]):
            sig = "interview"
        if sig in (None, "applied") or m.get("from_me"):
            continue
        hits = outcomes._match(company, "", m["subject"], roles)
        flat.append({**m, "signal": sig, "company": company, "hits": hits})
    return outcomes.attribute(flat, sigs, roles)


def test_twelve_open_applications_no_longer_mean_zero_signals():
    """Airbnb: 12 applications, one CodeSignal invite that names none of them. The old rule
    called this ambiguous and wrote nothing; the company-level fact is not ambiguous at all."""
    props = _propose("Airbnb", [_msg(
        "2026-07-21", "Next Step: Complete the Airbnb Industry Coding Assessment on CodeSignal",
        "CodeSignal <sample-f8d5ac48@example.com>")], _roles("Airbnb", 12))
    assert len(props) == 1
    p = props[0]
    assert p["scope"] == "company" and p["confidence"] == "medium"
    assert p["n_candidates"] == 12 and p["verdict"] == "attributable"
    assert outcomes.committable(p)


def test_a_single_open_application_is_still_role_level_and_certain():
    props = _propose("Commure", [_msg(
        "2026-08-03", "Commure Interview Confirmation - Software Engineer, RCM Product",
        "Jenna Fonck <sample-46989f91@example.com>")], _roles("Commure", 1, applied="2026-08-01"))
    assert props[0]["scope"] == "role" and props[0]["confidence"] == "high"
    assert props[0]["n_candidates"] == 1


def test_an_ats_autoack_that_says_next_steps_is_not_a_response():
    """Cisco's `sample-5cc464b5@example.com` sends "Your Cisco Journey Begins Here – Next Steps
    for Your Application" to everyone who clicks apply. It matches every interview regex there
    is and it means nothing."""
    sigs = outcomes.company_signals([_thread("Cisco", [_msg(
        "2026-08-01", "Your Cisco Journey Begins Here – Next Steps for YourApplication",
        "Cisco <sample-5cc464b5@example.com>")])])
    assert sigs["cisco"]["real"] is None


def test_an_assessment_vendor_counts_even_though_it_is_a_robot():
    """CodeSignal/HackerRank always mail from no-reply. A coding test is still a real screen."""
    sigs = outcomes.company_signals([_thread("Snowflake", [_msg(
        "2026-07-28", "Your HackerRank (AppEx) Snowflake Back-End Software Engineer Streamlit "
                      "Hiring Test Invitation", "Snowflake Hiring Team <sample-cedcd2c2@example.com>")])])
    assert sigs["snowflake"]["real"] == "2026-07-28"


def test_a_calendar_invite_is_an_interview_even_with_no_interview_word():
    """"Invitation from an unknown sender: Pinterest Chat | Alex & Crystal" carries no stage
    keyword at all and is the most decisive message in the whole thread."""
    sigs = outcomes.company_signals([_thread("Pinterest", [_msg(
        "2026-07-28", "Invitation from an unknown sender: Pinterest Chat | Alex & Crystal "
                      "@ Wed Jul 29, 2026", '"Crystal Acevedo" <sample-0a3df850@example.com>')])])
    assert sigs["pinterest"]["real"] == "2026-07-28"
    assert sigs["pinterest"]["evidence"]["2026-07-28"]["kind"] == "interview"


def test_a_thank_you_for_applying_is_never_a_response():
    sigs = outcomes.company_signals([_thread("Nuro", [
        _msg("2026-08-01", "Thank you for applying to Nuro", "sample-210b2f5f@example.com")])])
    assert sigs["nuro"]["real"] is None


def test_only_the_auto_tier_is_trusted():
    """A `review` thread is one mail_label refused to attribute; guessing there puts another
    company's interview on this row."""
    assert outcomes.company_signals([_thread("Whoever", [
        _msg("2026-08-01", "Interview Availability Request", "sample-fb98d44a@example.com")], tier="review")]) == {}


def test_a_recruiter_who_wrote_first_is_not_a_response_to_the_application():
    """Microsoft's reply landed 07-28, after her 07-24 application — but a recruiter InMail
    ("New Senior Software Engineer opportunity at Microsoft") opened the thread on 07-17.
    Crediting the machine for that conversation is the easiest lie in this dataset."""
    msgs = [_msg("2026-07-17", "New Senior Software Engineer opportunity at Microsoft",
                 '"Gayatri J." <sample-8ad1c0e9@example.com>'),
            _msg("2026-07-28", "Next Steps with Microsoft",
                 "Stephanie Jocom <sample-c1276010@example.com>")]
    props = _propose("Microsoft", msgs, _roles("Microsoft", 9, applied="2026-07-24"))
    assert props and all(p["verdict"] == "pre-existing" for p in props)
    assert not any(outcomes.committable(p) for p in props)


def test_a_pipeline_that_was_already_open_is_not_a_conversion():
    """Anthropic invited her to the Fellow program on 07-17; she then filed 19 applications
    there starting 07-20."""
    props = _propose("Anthropic", [_msg(
        "2026-07-17", "Anthropic invitation to interview for Fellow program",
        "sample-210b2f5f@example.com")], _roles("Anthropic", 19, applied="2026-07-20"))
    assert props[0]["verdict"] == "pre-existing"
    assert props[0]["confidence"] == "low"


def test_an_explicit_invite_counts_even_from_a_noreply_ats():
    """Greenhouse mails real invitations from no-reply@; dropping them would have hidden the
    fact that the Anthropic pipeline predates all 19 Anthropic applications."""
    sigs = outcomes.company_signals([_thread("Anthropic", [_msg(
        "2026-07-17", "Anthropic invitation to interview for Fellow program",
        "sample-210b2f5f@example.com")])])
    assert sigs["anthropic"]["real"] == "2026-07-17"


def test_a_referral_is_reported_beside_the_verdict_not_instead_of_it():
    """Google's thread opens "Invitation from Avery Wei to apply to Google" — a referral. It
    seeds the conversation without being a reply, and the interview a week later is still
    something the application caused."""
    msgs = [_msg("2026-07-20", "Invitation from Avery Wei to apply to Google",
                 "Google Recruiting <sample-adc0782f@example.com>"),
            _msg("2026-07-27", "Appointment booked: Google Chat w/ Cwon (Alex Example) @ Tue "
                               "Jul 28, 2026 2pm", "Cwon Terrell <sample-e3f5fc30@example.com>")]
    props = _propose("Google", msgs, _roles("Google", 5, applied="2026-07-20"))
    assert props[0]["referral_at"] == "2026-07-20"
    assert props[0]["verdict"] == "attributable"


def test_rejections_stay_distinguishable_from_positive_responses():
    rej = outcomes.signal_of("Motive | Following up on your application - we regret to inform you")
    assert rej == "rejection"
    assert outcomes._COMMIT_AS["rejection"] == ("", "rejected")
    assert outcomes._COMMIT_AS["interview"] == ("interview", "")


def test_the_guess_is_the_freshest_application_already_in_when_the_mail_landed():
    """Deterministic on purpose: a backfill that re-ran and moved the signal to a different
    requisition would be worse than not running it."""
    rows = [{"job_id": "gh:x:old", "applied_date": "2026-07-01", "weighted_score": 90},
            {"job_id": "gh:x:new", "applied_date": "2026-07-20", "weighted_score": 60},
            {"job_id": "gh:x:later", "applied_date": "2026-08-01", "weighted_score": 99}]
    assert outcomes._best_role(rows, "2026-07-25")["job_id"] == "gh:x:new"
    assert outcomes._best_role(list(reversed(rows)), "2026-07-25")["job_id"] == "gh:x:new"


def test_the_long_window_behind_the_short_one_keeps_the_ordering_honest():
    """launchd runs `--days 14`, far too narrow to see the 07-17 InMail. Folding mail_label's
    cache in behind the fresh scan is what stops a pre-existing pipeline from graduating into
    "the machine did this" the moment the outreach scrolls out of the window."""
    old = outcomes.company_signals([_thread("Microsoft", [_msg(
        "2026-07-17", "New Senior Software Engineer opportunity at Microsoft",
        '"Gayatri J." <sample-8ad1c0e9@example.com>')])])
    fresh = outcomes.company_signals([_thread("Microsoft", [_msg(
        "2026-07-28", "Next Steps with Microsoft", "Stephanie Jocom <sample-df1f4a88@example.com>")])])
    assert fresh["microsoft"]["contact"] == "2026-07-28"
    assert outcomes.merge_signals(old, fresh)["microsoft"]["contact"] == "2026-07-17"


# --- what reaches the DB ----------------------------------------------------------------------

def test_a_company_level_signal_records_the_response_and_says_it_is_a_guess(cfg):
    conn = _applied(cfg)
    p = {"signal": "assessment", "stage": "screen", "outcome": "", "date": "2026-07-21",
         "subject": "Next Step: Complete the Airbnb Industry Coding Assessment on CodeSignal",
         "scope": "company", "verdict": "attributable", "n_candidates": 12}
    outcomes.apply_proposal(cfg, conn, p, "gh:x:1")
    row = conn.execute("SELECT * FROM role_state WHERE job_id='gh:x:1'").fetchone()
    assert row["first_response_at"] == "2026-07-21"
    assert row["response_confidence"] == "company"          # honest about the ambiguity
    assert "1 of 12 open reqs" in row["outcome_note"]


def test_inbound_contact_can_never_become_a_response(cfg):
    """The rule that keeps Microsoft and Anthropic out of the conversion count, enforced at the
    one place that writes."""
    conn = _applied(cfg)
    store.record_signal(conn, "gh:x:1", stage="interview", outcome="rejected",
                        note="recruiter InMail", when="2026-07-17", inbound=True)
    row = conn.execute("SELECT * FROM role_state WHERE job_id='gh:x:1'").fetchone()
    assert row["first_response_at"] is None
    assert row["stage"] is None and row["outcome"] is None
    assert row["status"] == "applied"                       # not closed, not advanced
    assert row["last_contact_at"] == "2026-07-17"
    assert "recruiter InMail" in row["outcome_note"]


def test_confidence_only_ever_improves(cfg):
    conn = _applied(cfg)
    store.record_signal(conn, "gh:x:1", stage="screen", when="2026-07-21", confidence="company")
    store.record_signal(conn, "gh:x:1", stage="interview", when="2026-07-28", confidence="role")
    row = conn.execute("SELECT * FROM role_state WHERE job_id='gh:x:1'").fetchone()
    assert row["response_confidence"] == "role"
    store.record_signal(conn, "gh:x:1", stage="onsite", when="2026-08-01", confidence="company")
    row = conn.execute("SELECT * FROM role_state WHERE job_id='gh:x:1'").fetchone()
    assert row["response_confidence"] == "role"             # a later guess never un-sharpens it


def test_an_unrelated_write_does_not_wipe_response_confidence(cfg):
    """upsert_state uses INSERT OR REPLACE — a column missing from _STATE_COLS is silently
    nulled on the next status change. Same regression as first_response_at, one column later."""
    conn = _applied(cfg)
    store.record_signal(conn, "gh:x:1", stage="screen", confidence="company")
    store.set_notes(conn, "gh:x:1", "unrelated")
    row = conn.execute("SELECT * FROM role_state WHERE job_id='gh:x:1'").fetchone()
    assert row["response_confidence"] == "company"


def test_the_migration_adds_the_new_columns_to_an_old_db(cfg):
    conn = store.connect_user(cfg)
    conn.executescript("DROP TABLE role_state;"
                       "CREATE TABLE role_state (job_id TEXT PRIMARY KEY, status TEXT);"
                       "DROP TABLE interviews;"
                       "CREATE TABLE interviews (marker_id TEXT PRIMARY KEY, company TEXT);")
    conn.execute("INSERT INTO role_state (job_id, status) VALUES ('gh:x:1','applied')")
    conn.commit()
    conn.close()

    conn = store.connect_user(cfg)
    assert "response_confidence" in {r["name"] for r in conn.execute("PRAGMA table_info(role_state)")}
    assert "source" in {r["name"] for r in conn.execute("PRAGMA table_info(interviews)")}
    assert conn.execute("SELECT status FROM role_state WHERE job_id='gh:x:1'").fetchone()[0] == "applied"


def test_the_same_note_is_not_appended_twice(cfg):
    """The outcomes job runs at 11:00 and 16:00 over the same 14 days; without this the note
    grows one identical line per run and stops being readable."""
    conn = _applied(cfg)
    for _ in range(3):
        store.record_signal(conn, "gh:x:1", outcome="rejected",
                            note="rejection: Update from SeatGeek", when="2026-08-07")
    note = conn.execute("SELECT outcome_note FROM role_state WHERE job_id='gh:x:1'").fetchone()[0]
    assert note.count("Update from SeatGeek") == 1
