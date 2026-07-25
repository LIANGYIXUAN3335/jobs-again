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


def test_lookup_falls_back_to_apply_url_for_captured_roles(cfg):
    """Tesla/Apple/any-careers-site URLs carry no ATS job id — the stored apply_url is the
    join key. This is what lets the fill panel appear on captured roles' apply pages."""
    from app import store
    from tests.test_store import role_row
    conn = store.connect_view(cfg)
    url = "https://www.tesla.com/careers/search/job/software-engineer-backend-ai-tooling-241547"
    conn.execute("INSERT INTO jobs.roles (" + ", ".join(store.ROLE_COLS) + ") VALUES ("
                 + ", ".join(f":{c}" for c in store.ROLE_COLS) + ")",
                 role_row(job_id="manual:tesla:ai-tooling", company="Tesla",
                          title="Software Engineer, Backend, AI Tooling"))
    conn.execute("UPDATE jobs.roles SET apply_url=? WHERE job_id='manual:tesla:ai-tooling'", (url,))
    conn.commit()
    # exact, trailing-slash, query-string, and deeper-step variants must all resolve
    for u in (url, url + "/", url + "?source=linkedin&foo=1", url + "/apply"):
        hit = store.lookup_role_by_apply_url(conn, [u])
        assert hit and hit["job_id"] == "manual:tesla:ai-tooling", u
    # the LIST page is a prefix of every job URL — it must never resolve to one
    assert store.lookup_role_by_apply_url(conn, ["https://www.tesla.com/careers/search/"]) is None
    assert store.lookup_role_by_apply_url(conn, ["https://www.tesla.com/careers/search"]) is None
    # an unrelated job page must NOT match either
    assert store.lookup_role_by_apply_url(
        conn, ["https://www.tesla.com/careers/search/job/mechanical-design-engineer-999999"]) is None


def test_apple_req_id_joins_details_and_apply_urls(cfg):
    """Apple's apply flow (/app/en-us/apply/<id>) and details page (/en-us/details/<id>/slug)
    share only the req id; captured roles' job_ids are hashes, so the id must be found in the
    stored apply_url."""
    from app import store
    from app.web import url_candidates
    from tests.test_store import role_row
    assert "200673880-0836" in url_candidates("https://jobs.apple.com/app/en-us/apply/200673880-0836")
    assert "200673880-0836" in url_candidates(
        "https://jobs.apple.com/en-us/details/200673880-0836/senior-software-engineer?team=SFTWR")
    conn = store.connect_view(cfg)
    conn.execute("INSERT INTO jobs.roles (" + ", ".join(store.ROLE_COLS) + ") VALUES ("
                 + ", ".join(f":{c}" for c in store.ROLE_COLS) + ")",
                 role_row(job_id="cap:aabbccdd11223344", company="Apple",
                          title="Senior Software Engineer, Apple Data Platform"))
    conn.execute("UPDATE jobs.roles SET apply_url=? WHERE job_id='cap:aabbccdd11223344'",
                 ("https://jobs.apple.com/en-us/details/200673880-0836/senior-software-engineer-apple-data-platform?team=SFTWR",))
    conn.commit()
    hit = store.lookup_role_by_ids(conn, url_candidates(
        "https://jobs.apple.com/app/en-us/apply/200673880-0836"))
    assert hit and hit["job_id"] == "cap:aabbccdd11223344"


def test_outbound_pdf_name_strips_internal_bookkeeping():
    """The recruiter must never see our match score in the filename."""
    from app.web import outbound_pdf_name
    assert outbound_pdf_name(
        "/x/Casey Example - Apple - Apple Data Platform [bb44fbf66595bca5] - 88.pdf"
    ) == "Casey Example - Apple - Apple Data Platform.pdf"
    assert outbound_pdf_name("/x/Casey Example - Stripe - Money [6176758] - 80.pdf") \
        == "Casey Example - Stripe - Money.pdf"
    assert outbound_pdf_name("/x/Already Clean.pdf") == "Already Clean.pdf"


def test_identity_query_params_survive_canonicalization():
    """Deel's careers site keys every job on ?ats_id=<uuid> with an IDENTICAL path — stripping
    the query (the old behavior) made every Deel job canonicalize to the same URL and every
    capture collide into one role."""
    from app.manual import canonical_capture_url as c
    a = c("https://www.deel.com/careers/job/?ats_id=6fc6f9f1-78fb-4e96-9f11-08159e8b612b")
    b = c("https://www.deel.com/careers/job/?ats_id=6fc6f9f1-78fb-4e96-9f11-08159e8b612b&utm_source=li&ref=x")
    other = c("https://www.deel.com/careers/job/?ats_id=00000000-0000-0000-0000-000000000000")
    assert "ats_id=6fc6f9f1" in a
    assert a == b                        # view params stripped, identity kept
    assert a != other                    # different jobs stay different


def test_uuid_candidates_match_apply_url_query_params(cfg):
    """jobs.deel.com/deel/job-details/<uuid>/application carries the uuid in the PATH while the
    stored apply_url carries it after '=' — UUID-length ids match anywhere in the URL."""
    from app import store
    from app.web import url_candidates
    from tests.test_store import role_row
    conn = store.connect_view(cfg)
    conn.execute("INSERT INTO jobs.roles (" + ", ".join(store.ROLE_COLS) + ") VALUES ("
                 + ", ".join(f":{c}" for c in store.ROLE_COLS) + ")",
                 role_row(job_id="cap:deadbeef00112233", company="Deel",
                          title="Backend Engineer Node.js + AWS"))
    conn.execute("UPDATE jobs.roles SET apply_url=? WHERE job_id='cap:deadbeef00112233'",
                 ("https://www.deel.com/careers/job/?ats_id=6fc6f9f1-78fb-4e96-9f11-08159e8b612b",))
    conn.commit()
    hit = store.lookup_role_by_ids(conn, url_candidates(
        "https://jobs.deel.com/deel/job-details/6fc6f9f1-78fb-4e96-9f11-08159e8b612b/application?em"))
    assert hit and hit["job_id"] == "cap:deadbeef00112233"


def test_resume_pdf_route_is_actually_registered():
    """Regression for a self-inflicted outage: a helper was inserted between @app.get and
    api_resume_pdf, the decorator landed on the helper, and EVERY resume fetch (extension
    attach on all ATSes, the 📄 links, /apply queue) returned 422. An unknown job_id must
    give OUR 404 — a 422 'missing query param' means the route is bound to the wrong function."""
    from fastapi.testclient import TestClient
    from app.web import app
    r = TestClient(app).get("/api/roles/no:such:role/resume.pdf")
    assert r.status_code == 404
    assert "unknown job_id" in r.text
