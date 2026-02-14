from app import store
from app.web import url_candidates
from tests.test_store import role_row


def test_url_candidates_extracts_ids():
    assert "6042172" in url_candidates("https://stripe.com/jobs/search?gh_jid=6042172")
    assert "7775008" in url_candidates("https://job-boards.greenhouse.io/embed/job_app?for=x&token=7775008")
    assert "8463063002" in url_candidates("https://job-boards.greenhouse.io/databricks/jobs/8463063002")
    u = "ed704306-6585-4a76-ba8c-d23565ae6b9c"
    assert u in url_candidates(f"https://jobs.ashbyhq.com/snowflake/{u}")
    assert url_candidates("https://example.com/careers") == []


def test_lookup_matches_by_raw_id_and_prefers_tailored(cfg):
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("gh:stripe:6042172", company="Stripe", title="Backend Engineer"),
        role_row("ab:snowflake:ed704306-6585-4a76-ba8c-d23565ae6b9c", company="Snowflake"),
    ])
    view = store.connect_view(cfg)

    r = store.lookup_role_by_ids(view, url_candidates("https://stripe.com/x?gh_jid=6042172"))
    assert r and r["company"] == "Stripe"
    assert store.lookup_role_by_ids(view, ["999999"]) is None

    # when two rows share a candidate id, the tailored one wins
    store.set_tailor(view, "gh:stripe:6042172", tailor_status="ready",
                     tailored_resume_path="/tmp/x.pdf")
    r2 = store.lookup_role_by_ids(view, ["6042172", "6042172"])
    assert r2["tailored_resume_path"] == "/tmp/x.pdf"
