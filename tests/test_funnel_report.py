"""The funnel report's one job is to not flatter the system. These are the cases where a naive
reading would have — the ATS auto-ack that says "next steps", the recruiter who wrote first, the
interview that happened before she ever applied there.

Every subject line below is a real one out of data/mail_label_scan.json.
"""
import sqlite3

import pytest

from app import store
from tests.test_store import role_row
from tools import funnel_report as fr


def _thread(company, msgs, *, tier="auto", sender=""):
    return {"thrid": company + str(len(msgs)), "company": company, "tier": tier,
            "from": sender, "messages": [dict(m) for m in msgs]}


def _msg(date, subject, sender, *, from_me=False):
    return {"date": date, "subject": subject, "from": sender, "from_me": from_me}


# --- what counts as a reply -----------------------------------------------------------------

def test_an_ats_autoack_that_says_next_steps_is_not_a_reply():
    """Cisco's `sample-5cc464b5@example.com` sends "Your Cisco Journey Begins Here – Next Steps
    for Your Application" to everyone who clicks apply. It matches every interview regex there
    is, and it means nothing."""
    sigs = fr.company_signals([_thread("Cisco", [
        _msg("2026-08-01", "Your Cisco Journey Begins Here – Next Steps for YourApplication",
             "Cisco <sample-5cc464b5@example.com>")])])
    assert sigs["cisco"]["real"] is None


def test_an_assessment_vendor_counts_even_though_it_is_a_robot():
    """CodeSignal/HackerRank always mail from no-reply. A coding test is still a real screen."""
    sigs = fr.company_signals([_thread("Airbnb", [
        _msg("2026-07-21", "Next Step: Complete the Airbnb Industry Coding Assessment on CodeSignal",
             "CodeSignal <sample-f8d5ac48@example.com>")])])
    assert sigs["airbnb"]["real"] == "2026-07-21"


def test_a_calendar_invite_is_an_interview_even_with_no_interview_word():
    """"Invitation from an unknown sender: Pinterest Chat | Alex & Crystal" carries no stage
    keyword at all and is the most decisive message in the whole thread."""
    sigs = fr.company_signals([_thread("Pinterest", [
        _msg("2026-07-28", "Invitation from an unknown sender: Pinterest Chat | Alex & Crystal "
                           "@ Wed Jul 29, 2026", '"Crystal Acevedo" <sample-0a3df850@example.com>')])])
    assert sigs["pinterest"]["real"] == "2026-07-28"
    assert sigs["pinterest"]["evidence"]["2026-07-28"]["kind"] == "interview"


def test_an_explicit_interview_invite_counts_even_from_a_noreply_ats():
    """Greenhouse mails real invitations from no-reply@. "Anthropic invitation to interview for
    Fellow program" is not an auto-ack, and dropping it would have hidden the fact that the
    Anthropic pipeline predates all 19 Anthropic applications."""
    sigs = fr.company_signals([_thread("Anthropic", [
        _msg("2026-07-17", "Anthropic invitation to interview for Fellow program",
             "sample-210b2f5f@example.com")])])
    assert sigs["anthropic"]["real"] == "2026-07-17"


def test_a_thank_you_for_applying_is_never_a_reply():
    sigs = fr.company_signals([_thread("Nuro", [
        _msg("2026-08-01", "Thank you for applying to Nuro", "sample-210b2f5f@example.com"),
        _msg("2026-08-02", "Thank you for applying to Nuro", "sample-210b2f5f@example.com")])])
    assert sigs["nuro"]["real"] is None


def test_only_the_auto_tier_is_trusted():
    """A `review` thread is one mail_label refused to attribute; guessing puts another
    company's interview on this row."""
    sigs = fr.company_signals([_thread("Whoever", [
        _msg("2026-08-01", "Interview Availability Request", "sample-fb98d44a@example.com")], tier="review")])
    assert sigs == {}


def test_her_own_replies_never_count_as_a_response():
    sigs = fr.company_signals([_thread("Nuro", [
        _msg("2026-08-04", "Re: phone screen invitation", "sample-09daf18d@example.com", from_me=True)])])
    assert sigs["nuro"]["real"] is None


# --- attribution ----------------------------------------------------------------------------

def _apps(*specs):
    return [{"co": co, "company": co.title(), "applied_date": d, "source": "greenhouse",
             "first_response_at": None, "weighted_score": 70.0, "match": 90,
             "applied_with_resume": "tailored", "age_days": 3, "seniority": "mid",
             "job_id": f"gh:{co}:1"} for co, d in specs]


def test_a_reply_after_applying_is_attributable():
    cos = fr.verdicts(_apps(("commure", "2026-08-01")), {"commure": {
        "real": "2026-08-03", "contact": "2026-08-03", "evidence": {}}})
    assert cos["commure"]["verdict"] == "attributable"
    assert cos["commure"]["lag_days"] == 2


def test_a_pipeline_that_was_already_open_is_not_a_conversion():
    """Anthropic invited her to the Fellow program on 07-17; she applied to 19 Anthropic reqs
    starting 07-20. Crediting the machine for that invite is the easiest lie in this dataset."""
    cos = fr.verdicts(_apps(("anthropic", "2026-07-20")), {"anthropic": {
        "real": "2026-07-17", "contact": "2026-07-17", "evidence": {}}})
    assert cos["anthropic"]["verdict"] == "pre-existing"


def test_a_recruiter_who_wrote_first_is_not_a_conversion():
    """Microsoft's reply landed 07-28, after her 07-24 application — but a recruiter InMail
    ("New Senior Software Engineer opportunity at Microsoft") opened the thread on 07-17."""
    cos = fr.verdicts(_apps(("microsoft", "2026-07-24")), {"microsoft": {
        "real": "2026-07-28", "contact": "2026-07-17", "evidence": {}}})
    assert cos["microsoft"]["verdict"] == "pre-existing"


def test_silence_is_a_no_response_not_a_rejection():
    cos = fr.verdicts(_apps(("nvidia", "2026-07-20")), {})
    assert cos["nvidia"]["verdict"] == "no-response"
    assert cos["nvidia"]["lag_days"] is None


def test_outreach_is_read_off_the_subject_not_the_sender():
    """"New Senior Software Engineer opportunity at Microsoft" comes from a LinkedIn relay
    address; the subject is what identifies it as a recruiter opening a conversation."""
    sigs = fr.company_signals([_thread("Microsoft", [
        _msg("2026-07-17", "New Senior Software Engineer opportunity at Microsoft",
             '"Gayatri J." <sample-8ad1c0e9@example.com>')])])
    assert sigs["microsoft"]["contact"] == "2026-07-17"
    assert sigs["microsoft"]["real"] is None          # outreach is contact, not a stage


# --- slicing --------------------------------------------------------------------------------

def test_rates_are_company_level_not_application_level():
    """Twelve applications to one company that replies once is 1/1, not 1/12. A recruiter email
    names a company; it never names which of nine reqs."""
    apps = _apps(*[("airbnb", "2026-07-19")] * 12, ("nvidia", "2026-07-20"))
    cos = fr.verdicts(apps, {"airbnb": {"real": "2026-07-21", "contact": None, "evidence": {}}})
    rows = fr.slice_by(apps, cos, lambda a: a["source"])
    assert rows[0]["apps"] == 13
    assert rows[0]["companies"] == 2
    assert rows[0]["responded"] == 1
    assert rows[0]["rate"] == pytest.approx(50.0)


def test_age_days_prefers_posted_at_and_survives_a_missing_stamp():
    assert fr._age_days({"applied_date": "2026-08-01", "posted_at": "2026-07-30",
                         "first_seen": "2026-06-01"}) == 2
    assert fr._age_days({"applied_date": "2026-08-01", "posted_at": None,
                         "first_seen": "2026-07-25"}) == 7
    assert fr._age_days({"applied_date": "2026-08-01", "posted_at": None, "first_seen": None}) is None


def test_bands_are_half_open_so_nothing_lands_in_two_buckets():
    assert fr._band(59, [60, 70, 80]) == "<60"
    assert fr._band(60, [60, 70, 80]) == "60–70"
    assert fr._band(80, [60, 70, 80]) == "80+"
    assert fr._band(None, [60]) == "unknown"


# --- the DBs --------------------------------------------------------------------------------

@pytest.fixture
def dbs(cfg):
    """A tiny real pair of databases: one company applied to, one interview logged."""
    conn = store.connect_view(cfg)
    conn.execute("INSERT INTO jobs.roles (" + ", ".join(store.ROLE_COLS) + ") VALUES ("
                 + ", ".join(f":{c}" for c in store.ROLE_COLS) + ")",
                 role_row(job_id="gh:commure:1", company="Commure", first_seen="2026-07-29"))
    store.upsert_state(conn, "gh:commure:1", status="applied", applied_date="2026-08-01")
    conn.execute("INSERT INTO interviews (marker_id, company, happened_on, round_type)"
                 " VALUES ('a','Commure','2026-08-04','Recruiter Screen'),"
                 "        ('b','Amazon','2026-06','Phone Screen')")
    conn.commit()
    conn.close()
    return cfg


def test_the_report_never_takes_a_write_lock(dbs):
    conn = fr.connect_ro(dbs)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE role_state SET status='skip'")
    conn.close()


def test_applications_load_with_source_and_age(dbs):
    conn = fr.connect_ro(dbs)
    apps = fr.load_applications(conn)
    conn.close()
    assert len(apps) == 1
    assert apps[0]["source"] == "greenhouse"
    assert apps[0]["co"] == "commure"
    assert apps[0]["age_days"] == 3


def test_an_interview_at_a_company_never_applied_to_is_labelled_not_applied(dbs):
    conn = fr.connect_ro(dbs)
    apps = fr.load_applications(conn)
    cos = fr.verdicts(apps, {"commure": {"real": "2026-08-03", "contact": None, "evidence": {}}})
    ivs = fr.interview_attribution(conn, cos)
    conn.close()
    by = {i["company"]: i for i in ivs}
    assert by["Amazon"]["how"] == "not-applied"
    assert by["Commure"]["how"] == "post-apply"


def test_a_month_only_interview_date_is_compared_at_month_end(dbs):
    """`interviews.happened_on` is often just "2026-06". Padding it to the 28th is the
    conservative choice: it can only ever move an interview LATER, never invent a conversion."""
    conn = fr.connect_ro(dbs)
    cos = fr.verdicts(fr.load_applications(conn), {})
    ivs = {i["company"]: i for i in fr.interview_attribution(conn, cos)}
    conn.close()
    assert ivs["Amazon"]["how"] == "not-applied"
