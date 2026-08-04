import shutil
from pathlib import Path

import pytest
import yaml

from app import store, tailoring
from tests.test_store import role_row

TYPST = shutil.which("typst") or Path("/opt/homebrew/bin/typst").exists()


def _seed_role(cfg, job_id="gh:stripe:123", company="Stripe",
               title="Backend Engineer, Payments", jd="Build a distributed ledger."):
    jobs = store.connect_jobs(cfg)
    r = role_row(job_id, company=company, title=title)
    r["jd_text"] = jd
    store.upsert_roles(jobs, [r])
    jobs.close()
    return store.connect_view(cfg)


def test_suggest_angle():
    assert tailoring.suggest_angle({}, {"title": "Backend Engineer, Payments",
                                        "jd_text": "ledger", "company": "Stripe"}) == "payments"
    assert tailoring.suggest_angle({}, {"title": "Backend Engineer, Storage",
                                        "jd_text": "sharding", "company": "MongoDB"}) == "backend-distributed"


def test_write_prompt_contains_jd_base_and_schema(cfg):
    conn = _seed_role(cfg)
    p = tailoring.write_prompt(cfg, conn, "gh:stripe:123", angle="payments")
    assert p.exists()
    text = p.read_text()
    assert "distributed ledger" in text          # the JD
    assert "GMV Max" in text                      # base resume bullets included
    assert 'job_id: "gh:stripe:123"' in text      # schema pre-fills the id
    assert "do NOT invent" in text.lower() or "never fabricate" in text.lower()
    role = store.get_role(conn, "gh:stripe:123")
    assert role["tailor_status"] == "queued"
    assert role["prompt_path"] == str(p)


def test_validate_rejects_bad_shapes():
    with pytest.raises(tailoring.TailorError):
        tailoring.validate_tailored({"experience": []})            # empty
    with pytest.raises(tailoring.TailorError):
        tailoring.validate_tailored({"job_id": "x", "experience": [{"company": "A"}]})  # no bullets
    tailoring.validate_tailored({"job_id": "x", "experience": [{"company": "A", "bullets": ["ok"]}]})


def test_merge_keeps_facts_from_base_only(cfg):
    base = tailoring.load_base(cfg)
    tailored = {
        "job_id": "gh:stripe:123",
        "summary": "Backend-leaning engineer.",
        "experience": [
            {"company": "TikTok", "bullets": ["Reworded backend bullet."]},
            # Meta intentionally omitted → must fall back to master bullets
        ],
    }
    merged = tailoring.merge(base, tailored)
    assert merged["summary"] == "Backend-leaning engineer."
    tiktok = next(j for j in merged["experience"] if j["company"] == "TikTok")
    assert tiktok["bullets"] == ["Reworded backend bullet."]
    assert tiktok["dates"] == "April 2025 - Present"          # date comes from base
    meta = next(j for j in merged["experience"] if j["company"] == "Meta")
    assert len(meta["bullets"]) > 1                            # fell back to master
    # a tailored file cannot inject a fake employer
    assert {j["company"] for j in merged["experience"]} == {
        j["company"] for j in base["experience"]}


@pytest.mark.skipif(not TYPST, reason="typst not installed")
def test_ingest_renders_pdf(cfg, tmp_path):
    cfg = {**cfg, "output_dirs": {"prompts": str(tmp_path / "p"), "resumes": str(tmp_path / "r")}}
    conn = _seed_role(cfg)
    yf = tailoring.resume_yaml_path(cfg, "Stripe", "gh:stripe:123")
    yf.write_text(yaml.safe_dump({
        "job_id": "gh:stripe:123",
        "summary": "Backend engineer moving toward distributed systems.",
        "skills": [{"label": "Languages", "items": ["Go", "Java", "Python"]}],
        "experience": [{"company": "TikTok", "bullets": ["Built Go/internal API flows."]}],
        "gaps": ["JD wants Kafka ownership; has integration-level exposure."],
    }))
    res = tailoring.ingest_file(cfg, conn, yf)
    pdf = Path(res["pdf"])
    assert pdf.exists() and pdf.stat().st_size > 1000
    assert pdf.read_bytes()[:4] == b"%PDF"
    assert store.get_role(conn, "gh:stripe:123")["tailor_status"] == "ready"
    assert res["gaps"]


def test_facts_ground_answers_in_the_tailored_resume(cfg, tmp_path):
    """✨ button (2026-08-01): answers must speak the same angle as the submitted PDF, so
    _facts must carry the tailored YAML's summary AND bullets when one exists for the role."""
    import yaml as _yaml
    from app import ai as aimod, tailoring

    role = {"job_id": "li:4437046992", "company": "Precisely", "title": "Associate SWE"}
    yp = tailoring.resume_yaml_path(cfg, role["company"], role["job_id"])
    yp.write_text(_yaml.safe_dump({
        "summary": "Full-stack engineer who ships guardrails end to end.",
        "angle": "AI-first product engineering",
        "experience": [{"company": "TikTok",
                        "bullets": ["Cut invalid submissions ~30% via state-machine guardrails"]}],
    }), encoding="utf-8")

    facts = aimod._facts(cfg, role)
    assert "ships guardrails end to end" in facts
    assert "Cut invalid submissions ~30%" in facts
    assert "AI-first product engineering" in facts

    # no tailored YAML → facts still build (grounding is best-effort, never a hard dependency)
    role2 = {"job_id": "gh:none:1", "company": "NoSuchCo", "title": "SWE"}
    assert "Name:" in aimod._facts(cfg, role2)
