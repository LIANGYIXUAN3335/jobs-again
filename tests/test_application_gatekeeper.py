import datetime

import pytest

from app import store
from app.application_gatekeeper import REFUSAL_COOLDOWN_DAYS, record_expired, record_refused, refusal_days
from app.config import norm_key, norm_title


def role(job_id="gh:example:1", url="https://jobs.example.test/role-1", title="Backend Engineer"):
    value = {key: None for key in store.ROLE_COLS}
    value.update(job_id=job_id, ats="greenhouse", company="Example", company_norm=norm_key("Example"),
                 title=title, title_norm=norm_title(title), apply_url=url, weighted_score=80,
                 verdict="consider", first_seen=store.la_today(), last_seen=store.la_today(),
                 location="Remote, United States", is_remote=1)
    return value


def seed(cfg, **changes):
    value = role(**changes)
    with store.connect_jobs(cfg) as conn:
        store.upsert_roles(conn, [value])
    return {"job_id": value["job_id"], "url": value["apply_url"], "status": "new",
            "apply_profile": "casey", "company": value["company"], "title": value["title"]}


def test_an_employer_refusal_closes_the_role_without_calling_it_applied(cfg):
    job = seed(cfg)
    record_refused(cfg, job, "雇主拒收：We couldn't submit your application. You've recently applied to this role.")
    with store.connect_view(cfg) as conn:
        saved = store.get_role(conn, job["job_id"])
    assert saved["status"] == "skip" and saved.get("applied_date") is None
    assert "雇主拒收" in saved["notes"] and "recently applied" in saved["notes"]
    with pytest.raises(ValueError):
        record_refused(cfg, seed(cfg, job_id="gh:example:2", url="https://jobs.example.test/role-2"), "Some other stop.")


@pytest.mark.parametrize("words, days", [
    ("You may submit 2 applications per 30 days; reapply in 6 months.", 180),
    ("Reapply in 6 months. Limit: 2 applications per 30 days.", 180),
    ("A 6-month wait applies.", 180),
    ("Please wait 2 weeks.", 14),
    ("Reapply in 30 days.", 30),
    ("Come back in 5 years.", 365),
    ("We couldn't submit your application We've noticed that you've recently applied to this role, not the right fit.", REFUSAL_COOLDOWN_DAYS),
])
def test_a_refusal_holds_for_the_longest_period_it_names(words, days):
    assert refusal_days(words) == days


TODAY = datetime.date(2026, 11, 6)


@pytest.mark.parametrize("words, days", [
    # A line after the refusal that merely names a period. Two days would put the company back in the queue almost at once.
    ("You recently applied to this position.\nSimilar role posted 2 days ago", REFUSAL_COOLDOWN_DAYS),
    ("Application limit reached.\nBackend Engineer · 3 days a week in office", REFUSAL_COOLDOWN_DAYS),
    ("You have already applied to this job.\nThis role needs 5 years of experience.", REFUSAL_COOLDOWN_DAYS),
    ("You recently applied to this role\nWe have been around for 10 years", REFUSAL_COOLDOWN_DAYS),
    ("You have already applied to this job Name Email Resume Do you have at least 8 years of experience? Submit Application", REFUSAL_COOLDOWN_DAYS),
    ("You applied 2 days ago. You have already applied.", REFUSAL_COOLDOWN_DAYS),
    # A later line that does say how long: it can lengthen the 90 days, and it cannot shorten them.
    ("Application limit reached.\nReapply in 6 months.", 180),
    ("Application limit reached.\nReapply in 30 days.", REFUSAL_COOLDOWN_DAYS),
    # The refusal's own line is the employer speaking.
    ("You recently applied. Please wait 30 days.", 30),
    ("Your application is frozen. You may reapply after 90 days.", 90),
])
def test_only_a_period_about_staying_away_counts(words, days):
    assert refusal_days(words, TODAY) == days


@pytest.mark.parametrize("words, days", [
    ("You may reapply in a year.", 365),
    ("Wait a month before applying again.", 30),
    ("You recently applied. Please wait a few weeks.", REFUSAL_COOLDOWN_DAYS),
    ("You can't reapply for a few months.", REFUSAL_COOLDOWN_DAYS),
    ("Try again in several months.", 180),
    ("You may reapply after January 15, 2028.", (datetime.date(2028, 1, 15) - TODAY).days),
    ("You may reapply after December 2027.", (datetime.date(2028, 1, 1) - TODAY).days),
    ("You can apply again on March 3, 2027.", (datetime.date(2027, 3, 3) - TODAY).days),
    ("You can apply again on March 3.", (datetime.date(2027, 3, 3) - TODAY).days),
    ("Reapply after 2027-02-01.", (datetime.date(2027, 2, 1) - TODAY).days),
    ("Reapply after 2/1/2027.", (datetime.date(2027, 2, 1) - TODAY).days),
    ("You may reapply after 2027.", (datetime.date(2028, 1, 1) - TODAY).days),
    ("You may reapply after January 15, 2099.", 730),
    # A date already past, or one that cannot be, says nothing.
    ("You may reapply after January 15, 2020.", REFUSAL_COOLDOWN_DAYS),
    ("You may reapply after February 31, 2027.", REFUSAL_COOLDOWN_DAYS),
])
def test_a_refusal_holds_for_a_period_in_words_or_a_date_it_names(words, days):
    assert refusal_days(words, TODAY) == days


def _until(cfg, company="Example"):
    with store.connect_view(cfg) as conn:
        return store.manual_cooldowns(conn)[norm_key(company)]


def test_a_shorter_refusal_keeps_the_longer_freeze(cfg):
    with store.connect_view(cfg) as conn:
        kept = store.set_company_cooldown(conn, "Example", days=200, reason="owner set this")
    record_refused(cfg, seed(cfg), "雇主拒收：Reapply in 30 days.")
    assert _until(cfg)["until"] == kept["until"] and _until(cfg)["reason"] == "owner set this"


def test_a_longer_refusal_extends_a_shorter_freeze(cfg):
    with store.connect_view(cfg) as conn:
        kept = store.set_company_cooldown(conn, "Example", days=10, reason="owner set this")
    record_refused(cfg, seed(cfg), "雇主拒收：Reapply in 6 months.")
    assert _until(cfg)["until"] > kept["until"] and _until(cfg)["reason"].startswith("雇主拒收：")


def test_the_manual_cooldown_write_still_lets_the_later_decision_win(cfg):
    with store.connect_view(cfg) as conn:
        store.set_company_cooldown(conn, "Example", days=200)
        store.set_company_cooldown(conn, "Example", days=10, reason="changed my mind")
    assert _until(cfg)["reason"] == "changed my mind"


def test_closure_preserves_notes_identity_resume_and_twin(cfg):
    job = seed(cfg)
    with store.connect_jobs(cfg) as conn:
        store.upsert_roles(conn, [role("li:shadow", "https://linkedin.example.test/jobs/1")])
    with store.connect_view(cfg) as conn:
        store.upsert_state(conn, job["job_id"], apply_profile="casey", notes="Keep this original note.",
                           tailor_status="ready", tailored_resume_path="/tmp/unchanged.pdf")
        store.upsert_state(conn, "li:shadow", duplicate_of=job["job_id"], apply_profile="alex")
    evidence = "This job is no longer available."
    result = record_expired(cfg, job, evidence)
    assert result["status"] == "expired"
    with store.connect_view(cfg) as conn:
        saved = store.get_role(conn, job["job_id"])
        twin = store.get_role(conn, "li:shadow")
        assert saved["notes"].startswith("Keep this original note.\n\n已下架（")
        assert saved["notes"].endswith(evidence)
        assert saved["apply_profile"] == "casey"
        assert saved["tailored_resume_path"] == "/tmp/unchanged.pdf" and saved["tailor_status"] == "ready"
        assert saved["applied_date"] is None and saved["applied_with_resume"] is None
        assert twin["status"] == "new" and twin["apply_profile"] == "alex"
        assert conn.execute("SELECT COUNT(*) FROM applied_companies").fetchone()[0] == 0


@pytest.mark.parametrize("evidence", [
    "This job posting has expired.", "This position is closed.", "This role has been filled.",
    "The job you are looking for is no longer available.",
    "We are no longer accepting applications for this position.", "该职位已经下架",
    "Job not found", "We are no longer accepting applications.", "Applications are closed.",
])
def test_accepts_explicit_employer_closure_wording(cfg, evidence):
    assert record_expired(cfg, seed(cfg), evidence)["status"] == "expired"


@pytest.mark.parametrize("evidence", [
    "404 page not found", "The form did not load", "Sign in to apply", "CAPTCHA required",
    "This job is no longer available, but I am not sure.",
    "This job is no longer available. Sign in to continue.",
    "可能已下架", "无法确认职位已关闭", "This job is not closed.", "",
])
def test_unknown_login_and_generic_errors_do_not_expire(cfg, evidence):
    job = seed(cfg)
    with pytest.raises(ValueError):
        record_expired(cfg, job, evidence)
    with store.connect_view(cfg) as conn:
        assert store.get_role(conn, job["job_id"])["status"] == "new"


@pytest.mark.parametrize("changed", ["applied", "interviewing", "skip", "rejected", "expired"])
def test_never_overwrites_non_new_status(cfg, changed):
    job = seed(cfg)
    with store.connect_view(cfg) as conn:
        store.set_status(conn, job["job_id"], changed)
        store.set_notes(conn, job["job_id"], "Preserve this.")
    with pytest.raises(ValueError):
        record_expired(cfg, job, "This job is no longer available.")
    with store.connect_view(cfg) as conn:
        current = store.get_role(conn, job["job_id"])
        assert current["status"] == changed and current["notes"] == "Preserve this."


@pytest.mark.parametrize("url", [
    "https://other.example.test/role-1", "http://jobs.example.test/role-1",
    "https://jobs.example.test/role-2", "https://jobs.example.test/role-1/other",
    "https://jobs.example.test/role-1?job_id=other", "https://user:secret@jobs.example.test/role-1",
])
def test_mismatched_role_url_does_not_write(cfg, url):
    job = seed(cfg)
    job["url"] = url
    with pytest.raises(ValueError):
        record_expired(cfg, job, "This job is no longer available.")
    with store.connect_view(cfg) as conn:
        assert store.get_role(conn, job["job_id"])["status"] == "new"


@pytest.mark.parametrize("url", [
    "https://jobs.example.test/role-1/application", "https://jobs.example.test/role-1/apply/",
    "https://jobs.example.test:443/role-1?utm_source=test#application",
])
def test_application_suffix_and_tracking_differences_are_same_role(cfg, url):
    job = seed(cfg)
    job["url"] = url
    assert record_expired(cfg, job, "This job is no longer available.")["status"] == "expired"


def test_query_job_identifiers_cannot_change(cfg):
    job = seed(cfg, url="https://jobs.example.test/careers?gh_jid=123")
    job["url"] = "https://jobs.example.test/careers?gh_jid=456"
    with pytest.raises(ValueError):
        record_expired(cfg, job, "This job is no longer available.")


def test_unknown_job_id_creates_no_state(cfg):
    job = seed(cfg)
    job["job_id"] = "missing"
    with pytest.raises(ValueError):
        record_expired(cfg, job, "This job is no longer available.")
    with store.connect_view(cfg) as conn:
        assert conn.execute("SELECT COUNT(*) FROM role_state").fetchone()[0] == 0


def test_expired_is_counted_and_removed_only_from_its_own_queue_entry(cfg):
    cfg = {**cfg, "application_budget": {"enabled": False}, "signature_match": "hide"}
    job = seed(cfg)
    with store.connect_jobs(cfg) as conn:
        store.upsert_roles(conn, [role("gh:example:2", "https://jobs.example.test/role-2")])
    record_expired(cfg, job, "This job is no longer available.")
    with store.connect_view(cfg) as conn:
        review = store.fetch_view(conn, cfg, status="new")
        assert [item["job_id"] for item in review["rows"]] == ["gh:example:2"]
        assert review["counts"]["expired"] == 1 and review["counts"]["applied"] == 0
        expired = store.fetch_view(conn, cfg, status="expired")
        assert [item["job_id"] for item in expired["rows"]] == [job["job_id"]]
        assert len(store.fetch_view(conn, cfg, status="all")["rows"]) == 2


def test_set_status_expired_has_no_application_or_duplicate_cascade(cfg):
    job = seed(cfg)
    with store.connect_jobs(cfg) as conn:
        store.upsert_roles(conn, [role("li:shadow", "https://linkedin.example.test/jobs/1")])
    with store.connect_view(cfg) as conn:
        store.set_status(conn, job["job_id"], "expired")
        saved = store.get_role(conn, job["job_id"])
        assert saved["status"] == "expired" and saved["applied_date"] is None
        assert store.get_role(conn, "li:shadow")["status"] == "new"
        assert conn.execute("SELECT COUNT(*) FROM role_state WHERE duplicate_of IS NOT NULL").fetchone()[0] == 0


@pytest.mark.parametrize("words, days", [
    ("Please reapply in six months.", 180),
    ("You may apply again after twelve months.", 360),
    ("Try again in two weeks.", 14),
    ("Wait one year before applying again.", 365),
    ("2 applications per 30 days; reapply in six months", 180),
    ("A ten-day cooling off period applies.", 10),
    ("You have submitted too many applications.", REFUSAL_COOLDOWN_DAYS),
    ("Our someone-month policy applies.", REFUSAL_COOLDOWN_DAYS),
])
def test_a_refusal_period_can_be_written_in_words(words, days):
    assert refusal_days(words) == days
