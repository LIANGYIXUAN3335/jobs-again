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


def test_url_candidates_reads_digit_leading_workday_req_ids():
    """Autodesk stamps the fiscal year on the FRONT of its req id (26WD99399), which the
    letters-first Workday pattern cannot see at all. web.log 2026-08-09: 78 consecutive
    /api/lookup 404s on one such apply page whose role WAS in the queue with a tailored PDF,
    so the panel never opened and the form got filled by hand.

    The trailing -1 is Workday's repost counter and is never part of the stored job_id
    (wd:autodesk:wd1:Ext:26WD99399), so it must not be captured.
    """
    got = url_candidates(
        "https://autodesk.wd1.myworkdayjobs.com/en-US/Ext/job/Montreal%2C-QC%2C-CAN/"
        "Senior-Software-Developer---Platform---Emerging-Technologies_26WD99399-1/apply/a")
    assert "26WD99399" in got
    assert "26WD99399-1" not in got
    # the letters-first shapes must keep working
    assert "R-284379" in url_candidates(
        "https://mastercard.wd1.myworkdayjobs.com/en-US/CorporateCareers/job/x/"
        "Software-Engineer-II_R-284379/apply/autofillWithResume")
    assert "R170631" in url_candidates(
        "https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/San-Jose/"
        "Software-Development-Engineer_R170631/apply/autofillWithResume")


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


# ---- apply pin -------------------------------------------------------------
# A LinkedIn role's apply_url is the LinkedIn page; clicking Apply there hands you off to the
# employer's own ATS, whose URL carries no id we store. Nothing in url_candidates can bridge
# that, so the pin (set by the ↗ click) is the only join back to li:<id>.

def test_linkedin_url_resolves_to_li_role(cfg):
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [role_row("li:4136621785", company="Netflix", title="SWE 5")])
    view = store.connect_view(cfg)
    cands = url_candidates("https://www.linkedin.com/jobs/view/4136621785/")
    assert "4136621785" in cands
    assert store.lookup_role_by_ids(view, cands)["job_id"] == "li:4136621785"


def test_employer_ats_url_cannot_reach_the_li_role(cfg):
    """The gap the pin exists to close — documented so a future refactor can't 'simplify' it away."""
    jobs = store.connect_jobs(cfg)
    row = role_row("li:4136621785", company="Netflix")
    row["apply_url"] = "https://www.linkedin.com/jobs/view/4136621785/"
    store.upsert_roles(jobs, [row])
    view = store.connect_view(cfg)
    landing = "https://jobs.netflix.com/jobs/790300019012"
    assert store.lookup_role_by_ids(view, url_candidates(landing)) is None
    assert store.lookup_role_by_apply_url(view, [landing]) is None


def test_apply_pin_roundtrip_and_ttl(cfg, monkeypatch):
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [role_row("li:4136621785", company="Netflix")])
    view = store.connect_view(cfg)

    assert store.get_apply_pin(view) is None
    store.set_apply_pin(view, "li:4136621785")
    assert store.get_apply_pin(view) == "li:4136621785"

    # a pin older than the TTL must not resurface — it would attach a stale résumé
    view.execute(
        "UPDATE apply_pins SET pinned_at='2020-01-01T00:00:00+00:00' WHERE job_id=?",
        ("li:4136621785",),
    )
    view.commit()
    assert store.get_apply_pin(view) is None

    # re-pinning the SAME role refreshes it in place rather than stacking duplicates
    store.set_apply_pin(view, "li:4136621785")
    assert view.execute("SELECT COUNT(*) FROM apply_pins").fetchone()[0] == 1
    store.clear_apply_pin(view)
    assert store.get_apply_pin(view) is None


def test_pins_from_several_tabs_all_survive(cfg):
    """The Temporal-on-a-Precisely-form bug (2026-07-31). The pin used to be one global slot,
    so clicking ↗ down a list of tabs left only the last role — and the extension then labelled
    whatever form came next with it. Every ↗ is a separate statement of intent; keep them all,
    carrying the company so the caller can pick the one that fits the page it is on."""
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("li:4437046992", company="Precisely"),
        role_row("gh:temporaltechnologies:5183565007", company="Temporal"),
    ])
    view = store.connect_view(cfg)

    store.set_apply_pin(view, "gh:temporaltechnologies:5183565007")
    store.set_apply_pin(view, "li:4437046992")

    pins = store.get_apply_pins(view)
    assert [p["job_id"] for p in pins] == [
        "li:4437046992", "gh:temporaltechnologies:5183565007"]      # newest first
    # the company travels with the pin — matching it to a page must not need a join back
    assert {p["company_norm"] for p in pins} == {"precisely", "temporal"}

    # releasing the one you actually applied to leaves the other tab's pin alone
    store.clear_apply_pin(view, "li:4437046992")
    assert [p["job_id"] for p in store.get_apply_pins(view)] == [
        "gh:temporaltechnologies:5183565007"]


def test_stale_pins_are_pruned_on_write(cfg):
    """A pin past the TTL must never reach the caller, even if the reader forgets to check."""
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [role_row("li:1", company="Precisely"),
                              role_row("li:2", company="Temporal")])
    view = store.connect_view(cfg)

    store.set_apply_pin(view, "li:1")
    view.execute("UPDATE apply_pins SET pinned_at='2020-01-01T00:00:00+00:00'")
    view.commit()
    store.set_apply_pin(view, "li:2")            # any write prunes

    assert [p["job_id"] for p in store.get_apply_pins(view)] == ["li:2"]
    assert view.execute("SELECT COUNT(*) FROM apply_pins").fetchone()[0] == 1


def test_pin_endpoints(cfg, monkeypatch):
    """Exercise the real routes, but against the throwaway DBs — app.web binds `cfg` at import
    time, so without this monkeypatch the test would pin a role in the REAL user_state.db."""
    from fastapi.testclient import TestClient
    from app import web as webmod

    monkeypatch.setattr(webmod, "cfg", cfg)
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [role_row("li:4136621785", company="Netflix", title="SWE 5")])
    client = TestClient(webmod.app)

    assert client.get("/api/pinned").status_code == 404          # nothing pinned yet
    assert client.post("/api/pin", json={"job_id": "nope:1"}).status_code == 404

    assert client.post("/api/pin", json={"job_id": "li:4136621785"}).status_code == 200
    body = client.get("/api/pinned").json()
    assert body["job_id"] == "li:4136621785"
    assert body["from_pin"] is True          # the panel must be able to say "not URL-matched"
    assert body["company"] == "Netflix"

    client.delete("/api/pin")
    assert client.get("/api/pinned").status_code == 404


def test_pins_endpoint_returns_every_live_pin(cfg, monkeypatch):
    """/api/pins hands the extension the whole live set, because only the extension can see
    the page and decide which one belongs to it. A server-side pick is what put a Temporal
    role on a Precisely form. Deleting one pin must not disturb the rest."""
    from fastapi.testclient import TestClient
    from app import web as webmod

    monkeypatch.setattr(webmod, "cfg", cfg)
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("li:4437046992", company="Precisely", title="Associate SWE"),
        role_row("gh:temporaltechnologies:5183565007", company="Temporal", title="SWE II"),
    ])
    client = TestClient(webmod.app)

    assert client.get("/api/pins").json()["pins"] == []
    client.post("/api/pin", json={"job_id": "gh:temporaltechnologies:5183565007"})
    client.post("/api/pin", json={"job_id": "li:4437046992"})

    pins = client.get("/api/pins").json()["pins"]
    assert [p["company"] for p in pins] == ["Precisely", "Temporal"]     # newest first
    assert all(p["from_pin"] is True for p in pins)

    # the frame that claimed Precisely releases only Precisely
    assert client.delete("/api/pin", params={"job_id": "li:4437046992"}).status_code == 200
    assert [p["company"] for p in client.get("/api/pins").json()["pins"]] == ["Temporal"]


def test_cooldown_endpoints_round_trip(cfg, monkeypatch):
    """「这家不让投了」(owner 2026-09-22): POST hides the company from review at once, GET lists
    config + manual merged, DELETE clears a manual one, refuses a config one (409) and 404s
    on nothing. Against the throwaway DBs — app.web binds `cfg` at import time."""
    from fastapi.testclient import TestClient
    from app import web as webmod

    cfg = {**cfg, "application_budget": {"enabled": False},
           "cooldowns": {"Cfgco": {"until": "2099-01-01", "reason": "in yaml"}}}
    monkeypatch.setattr(webmod, "cfg", cfg)
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [role_row("gh:a:1", company="Acme"), role_row("gh:a:2", company="Acme"),
                              role_row("gh:b:1", company="Bravo")])
    client = TestClient(webmod.app)

    r = client.post("/api/companies/cooldown", json={"company": "Acme", "days": 60, "reason": "one at a time"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "manual" and body["hidden_roles"] == 2 and body["reason"] == "one at a time"
    rows = client.get("/api/companies/cooldowns").json()["rows"]
    assert [(x["company"], x["source"]) for x in rows] == [("Acme", "manual"), ("Cfgco", "config")]

    view = store.connect_view(cfg)
    assert {x["company"] for x in store.fetch_view(view, cfg, status="new")["rows"]} == {"Bravo"}

    assert client.post("/api/companies/cooldown", json={"company": "Acme", "days": 0}).status_code == 422
    assert client.post("/api/companies/cooldown", json={"company": "Acme", "until": "garbage"}).status_code == 422
    assert client.delete("/api/companies/cooldown/Cfgco").status_code == 409     # lives in config.yaml
    assert client.delete("/api/companies/cooldown/Acme").status_code == 200
    assert client.delete("/api/companies/cooldown/Acme").status_code == 404
    assert {x["company"] for x in store.fetch_view(view, cfg, status="new")["rows"]} == {"Acme", "Bravo"}


def test_cooldown_from_the_panel_skips_the_role_and_shows_in_lookup(cfg, monkeypatch):
    """Owner 2026-09-25: the application page is where she learns a company won't take more
    applications, so the panel sets the cooldown itself. It may also mark THIS role skip
    (never an applied one), and the lookup payload reports the active cooldown so the panel
    shows it first the next time any posting of that company opens."""
    from fastapi.testclient import TestClient
    from app import web as webmod

    cfg = {**cfg, "application_budget": {"enabled": False}, "cooldowns": {}}
    monkeypatch.setattr(webmod, "cfg", cfg)
    jobs = store.connect_jobs(cfg)
    a = role_row("gh:acme:1", company="Acme"); a["apply_url"] = "https://job-boards.greenhouse.io/acme/jobs/1"
    b = role_row("gh:acme:2", company="Acme"); b["apply_url"] = "https://job-boards.greenhouse.io/acme/jobs/2"
    store.upsert_roles(jobs, [a, b])
    view = store.connect_view(cfg)
    store.set_status(view, "gh:acme:2", "applied", "base")
    client = TestClient(webmod.app)

    assert client.get("/api/lookup", params={"url": a["apply_url"]}).json()["cooldown"] is None
    r = client.post("/api/companies/cooldown", json={"company": "Acme", "days": 90, "reason": "limit", "skip_job_id": "gh:acme:1"}).json()
    assert r["ok"] and r["skipped_role"] is True
    assert store.get_role(view, "gh:acme:1")["status"] == "skip"
    cd = client.get("/api/lookup", params={"url": b["apply_url"]}).json()["cooldown"]
    assert cd["until"] == r["until"] and cd["source"] == "manual" and 88 <= cd["days_left"] <= 90 and cd["reason"] == "limit"
    # an applied role is never flipped to skip by the panel's cooldown
    r2 = client.post("/api/companies/cooldown", json={"company": "Acme", "days": 30, "skip_job_id": "gh:acme:2"}).json()
    assert r2["skipped_role"] is False and store.get_role(view, "gh:acme:2")["status"] == "applied"
