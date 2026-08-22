"""A validation failure must not leave the system claiming success.

Before 2026-08-10 `ingest.py` printed `✗` on a TailorError and moved on, never reaching
`store.set_tailor(...)`. The role therefore kept `tailor_status='ready'` pointing at the PDF
the OLD pipeline produced — the one the pilot audit found fabricated tools and metrics in.
/apply, the extension résumé lookup and the résumé chip all key off `ready`, so the stricter
validator neither fixed nor flagged 1,485 résumés; it declined to touch them while the UI kept
offering them. Measured on the real corpus: 1,465 roles are `ready` behind a YAML that the
credibility rules reject.
"""
import yaml

from app import ingest as ingestmod
from app import store, tailoring
from tests.test_store import role_row


def _seed(cfg, job_id="gh:stripe:500", company="Stripe"):
    jobs = store.connect_jobs(cfg)
    r = role_row(job_id, company=company, title="Backend Engineer")
    r["jd_text"] = "Build distributed services."
    store.upsert_roles(jobs, [r])
    jobs.close()
    conn = store.connect_view(cfg)
    store.set_tailor(conn, job_id, tailor_status="ready",
                     tailored_resume_path="/tmp/old-pipeline.pdf")
    return conn


def _write(cfg, company, job_id, bullets):
    p = tailoring.resume_yaml_path(cfg, company, job_id)
    p.write_text(yaml.safe_dump({
        "job_id": job_id,
        "summary": "Backend engineer with real shipped systems across advertising platforms.",
        "experience": [{"company": "TikTok", "bullets": bullets}]}), encoding="utf-8")
    return p


def test_a_failing_resume_stops_being_offered(cfg):
    conn = _seed(cfg)
    _write(cfg, "Stripe", "gh:stripe:500",
           ["Provisioned the GMV Max services with Terraform and Azure."])
    assert store.get_role(conn, "gh:stripe:500")["tailor_status"] == "ready"
    conn.close()

    ingestmod._scan_once(cfg, {}, force=True)

    conn = store.connect_view(cfg)
    role = store.get_role(conn, "gh:stripe:500")
    assert role["tailor_status"] == "invalid", "a rejected résumé must not stay `ready`"
    # the files themselves are never touched — only the recommendation is withdrawn
    assert tailoring.resume_yaml_path(cfg, "Stripe", "gh:stripe:500").exists()
    assert role["tailored_resume_path"] == "/tmp/old-pipeline.pdf"


def test_dry_run_reports_without_touching_the_db(cfg):
    """The blast-radius preview: she must be able to see what a stricter validator would do
    before it does it."""
    conn = _seed(cfg, "gh:stripe:501")
    _write(cfg, "Stripe", "gh:stripe:501", ["Shipped a Rust ledger doing 4.2M TPS."])
    conn.close()

    tally: dict = {}
    ingestmod._scan_once(cfg, {}, force=True, dry_run=True, tally=tally)
    assert tally["failed"] == 1

    conn = store.connect_view(cfg)
    assert store.get_role(conn, "gh:stripe:501")["tailor_status"] == "ready"


def test_scan_once_still_returns_an_int(cfg):
    """app/web.py's /api/ingest does `n = _scan_once(...)` and returns it as a count."""
    _seed(cfg, "gh:stripe:502")
    assert isinstance(ingestmod._scan_once(cfg, {}, force=True), int)
