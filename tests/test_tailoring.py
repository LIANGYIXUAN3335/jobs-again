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


def test_suggest_angle_ai_terms_are_word_bounded():
    """'rag' as a substring hits leverage/coverage/paragraph. Measured over 4,883 real JDs it
    was a trigger on 1,032 of 1,323 ai-platform routings and ~440 roles reached ai-platform for
    no other reason — a résumé framed for a role the company isn't hiring for."""
    jd = ("Build data migration tooling, leveraging our storage layer for coverage across "
          "shards. See the paragraph below on average latency.")
    assert tailoring.suggest_angle({}, {"title": "Software Engineer", "jd_text": jd,
                                        "company": "MongoDB"}) == "backend-distributed"
    # one genuine AI term + 'leveraging' must still NOT be enough (that was the live bug)
    jd2 = jd + " You will work alongside our LLM inference team."
    assert tailoring.suggest_angle({}, {"title": "Software Engineer", "jd_text": jd2,
                                        "company": "MongoDB"}) == "backend-distributed"
    # two real model-layer terms still route to ai-platform
    jd3 = "Serve LLM inference and build RAG pipelines over our vector index."
    assert tailoring.suggest_angle({}, {"title": "Software Engineer", "jd_text": jd3,
                                        "company": "Acme"}) == "ai-platform"


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
    tailoring.validate_tailored(_ok())


_SUMMARY = "Backend engineer with real shipped systems across advertising and AI platforms."


def _ok(bullets=("ok",), skills=None):
    t = {"job_id": "x", "summary": _SUMMARY,
         "experience": [{"company": "A", "bullets": list(bullets)}]}
    if skills is not None:
        t["skills"] = skills
    return t


def test_validate_rejects_placeholder_residue():
    """Schema example text must never render into a submittable PDF (the 半成品 guard)."""
    with pytest.raises(tailoring.TailorError, match="placeholder|too short"):
        tailoring.validate_tailored(_ok(["..."]))                      # truncation marker
    with pytest.raises(tailoring.TailorError, match="placeholder"):
        tailoring.validate_tailored(_ok(
            ["Strong verb + REAL system (e.g. GMV Max) + what you built + the metric."]))
    with pytest.raises(tailoring.TailorError, match="too short"):
        tailoring.validate_tailored({"job_id": "x", "summary": "",
                                     "experience": [{"company": "A", "bullets": ["ok"]}]})
    t = _ok()
    t["summary"] = "2-3 sentence professional summary tailored to THIS role."
    with pytest.raises(tailoring.TailorError, match="placeholder"):
        tailoring.validate_tailored(t)
    with pytest.raises(tailoring.TailorError, match="placeholder"):
        tailoring.validate_tailored(_ok(skills=[{"label": "G", "items": ["..."]}]))


@pytest.mark.parametrize("bullet", [
    # 1. the original hand-picked example — passed only thanks to its Oxford comma
    "Established API contracts across services, employing Golang, Terraform, "
    "Kubernetes, Docker, and Kafka to enhance reliability.",
    # 2. THE SAME FIVE TOOLS, sentence just ends on one. Scored 4 before 2026-08-10 → passed.
    "Established API contracts across services, employing Golang, Terraform, "
    "Kubernetes, Docker and Kafka.",
    # 3. six tools as hyphenated compounds — scored ZERO before 2026-08-10. This is not a
    #    hypothetical form: it is the base résumé's own house style ("Docker-based
    #    microservices" :59, "Kafka-backed streaming" :60) and the style the prompt encourages,
    #    so a model imitating her voice produced bullets the counter could not see at all.
    "Kafka-backed pipelines on Docker-based microservices with Kubernetes-managed rollouts, "
    "Redis-cached reads, MySQL-backed storage and Java-based workers.",
])
def test_validate_rejects_tool_zoo_bullet(bullet):
    """The #1 stuffing signal: a bullet tail-loaded with 5+ tool names. All three forms below
    name 5-6 tools; before the tokenisation fix only the first was detected."""
    with pytest.raises(tailoring.TailorError, match="tool"):
        tailoring.validate_tailored(_ok([bullet]))


def test_tool_zoo_boundary_is_at_four():
    """4 is the cap, so 4 passes and 5 rejects — including in the punctuation forms above."""
    assert tailoring._bullet_tool_count("Built flows on MySQL, Redis, Kafka and Docker.") == 4
    assert tailoring._bullet_tool_count(
        "Built Kafka-backed flows on MySQL, Redis and Docker-based workers.") == 4
    tailoring.validate_tailored(_ok(["Built flows on MySQL, Redis, Kafka and Docker."]))
    with pytest.raises(tailoring.TailorError, match="tool"):
        tailoring.validate_tailored(
            _ok(["Built flows on MySQL, Redis, Kafka, Docker and Kubernetes."]))
    # 3 tools with a mechanical role is fine (the Haoxiong MySQL/Cassandra/MongoDB story)
    tailoring.validate_tailored(_ok(
        ["Built product management on MySQL, Cassandra, and MongoDB with replication."]))
    # her own base bullet must not trip the guard it was written before
    tailoring.validate_tailored(_ok([
        "Engineered backend services for the Meta Business Suite AI agent onboarding platform, "
        "deploying Docker-based microservices behind the React onboarding surfaces."]))


def test_validate_rejects_soft_skill_items_and_group_overflow():
    with pytest.raises(tailoring.TailorError, match="soft skill"):
        tailoring.validate_tailored(_ok(skills=[
            {"label": "Practices", "items": ["problem-solving"]}]))
    with pytest.raises(tailoring.TailorError, match="skill groups"):
        tailoring.validate_tailored(_ok(skills=[
            {"label": f"G{i}", "items": ["Kafka"]} for i in range(5)]))
    tailoring.validate_tailored(_ok(skills=[
        {"label": f"G{i}", "items": ["Kafka"]} for i in range(4)]))


def test_validate_bullet_caps_use_base_dates(cfg):
    base = tailoring.load_base(cfg)
    seven = [f"story {i}" for i in range(7)]
    with pytest.raises(tailoring.TailorError, match="cap"):
        tailoring.validate_tailored(
            {"job_id": "x", "summary": _SUMMARY,
             "experience": [{"company": "TikTok", "bullets": seven}]}, base)
    # Haoxiong ended May 2022 → old role, capped at 3
    with pytest.raises(tailoring.TailorError, match="cap"):
        tailoring.validate_tailored(
            {"job_id": "x", "summary": _SUMMARY, "experience": [
                {"company": "Shanghai Haoxiong Education Technology Co., Ltd",
                 "bullets": ["a", "b", "c", "d"]}]}, base)
    tailoring.validate_tailored(
        {"job_id": "x", "summary": _SUMMARY, "experience": [
            {"company": "TikTok", "bullets": ["a", "b", "c", "d", "e", "f"]},
            {"company": "Shanghai Haoxiong Education Technology Co., Ltd",
             "bullets": ["a", "b", "c"]}]}, base)


# ---------- provenance: no claim without a source in resume_base.yaml ----------

def _tailored(bullets_by_company, summary=_SUMMARY, skills=None):
    t = {"job_id": "x", "summary": summary,
         "experience": [{"company": c, "bullets": list(b)} for c, b in bullets_by_company.items()]}
    if skills is not None:
        t["skills"] = skills
    return t


_HAOXIONG = "Shanghai Haoxiong Education Technology Co., Ltd"


def test_fabricated_resume_is_rejected(cfg):
    """THE test. Before 2026-08-10 this exact résumé passed validation and rendered into a PDF
    verbatim — merge() copies tailored bullets straight through, and nothing compared them to
    the base. Every claim below is invented: the tools, the metrics, the team, the patents."""
    base = tailoring.load_base(cfg)
    fabricated = _tailored({
        "TikTok": [
            "Architected a globally-replicated Rust ledger handling 4.2M transactions per second.",
            "Led a team of 12 engineers to deliver a multi-region Spanner migration.",
        ],
        "Meta": ["Fine-tuned a 70B-parameter foundation model that raised conversion 41%."],
    })
    with pytest.raises(tailoring.TailorError) as exc:
        tailoring.validate_tailored(fabricated, base)
    msg = str(exc.value)
    for token in ("rust", "spanner", "4.2", "12", "70", "41"):
        assert token in msg.lower(), f"{token!r} not named in the rejection: {msg}"


def test_provenance_names_the_offending_token_and_the_supported_values(cfg):
    """A rejection nobody can debug is a rejection that gets bypassed."""
    base = tailoring.load_base(cfg)
    v = tailoring.provenance_violations(
        _tailored({"Meta": ["Cut onboarding-flow failures by 77% in production."]}), base)
    assert len(v) == 1
    assert "77" in v[0] and "Meta" in v[0]
    assert "90" in v[0]                      # tells her which numbers Meta's bullets DO support


def test_provenance_allows_honest_rephrasing(cfg):
    """Rewording is the whole point of tailoring. Rounding, unit changes and restating a real
    metric in a different sentence are NOT fabrication — a validator that rejects honest
    rewrites gets switched off, and then it protects nothing."""
    base = tailoring.load_base(cfg)
    ok = _tailored({
        # same numbers, completely rewritten sentences
        "TikTok": ["Cut invalid submissions ~30% by making a multi-step advertiser flow "
                   "deterministic across 8+ scenarios."],
        # 1.2% rounded down to 1%, and "3 months" restated as "a quarter"
        "Meta": ["Launched Click to Messenger, worth roughly 1% incremental revenue in its "
                 "first quarter, with over 90% test coverage behind it."],
        # 500 -> "500+", 85% restated
        _HAOXIONG: ["Ran product data for 500+ SKUs on MySQL and Cassandra, held to 85% "
                    "unit-test coverage."],
    }, summary="Backend engineer with 6+ years shipping advertiser and onboarding systems at "
               "TikTok and Meta, where a 30% drop in invalid submissions came from making "
               "state transitions deterministic.")
    assert tailoring.provenance_violations(ok, base) == []
    tailoring.validate_tailored(ok, base)


def test_provenance_allows_unit_conversion(cfg):
    """'2 hours daily' (Gemini's real bullet) restated as '120 minutes' is the same claim."""
    base = tailoring.load_base(cfg)
    assert tailoring.provenance_violations(
        _tailored({"Gemini Express Corp": ["Removed 120 minutes of manual processing a day "
                                           "with a Python automation service."]}), base) == []


def test_provenance_rejects_a_number_that_is_merely_close(cfg):
    """The tolerance must not be a band. An early draft allowed ±5% plus unit factors and it
    swallowed 447 real instances of the retracted 'campaign efficiency by 2%' (2 ≈ 30/12) and
    285 of a wholly invented '50% fewer onboarding failures'."""
    base = tailoring.load_base(cfg)
    for bad in ("Improved advertiser campaign efficiency by 2%.",
                "Cut invalid submissions by 35% across advertiser journeys.",
                "Drove throughput up 25% across the pipeline."):
        assert tailoring.provenance_violations(_tailored({"TikTok": [bad]}), base), bad


def test_provenance_rejects_imported_technologies_everywhere(cfg):
    """Terraform / Azure / FastAPI / C++ were cut from the base for lack of bullet evidence
    (resume_base.yaml:33-35). They must not come back in a bullet, the summary, or skills.
    PyTorch was re-added to the pool on 2026-08-16, so it is deliberately NOT in this list —
    see test_provenance_allows_a_pool_only_technology below."""
    base = tailoring.load_base(cfg)
    assert tailoring.provenance_violations(
        _tailored({"TikTok": ["Provisioned the GMV Max services with Terraform."]}), base)
    assert tailoring.provenance_violations(
        _tailored({"TikTok": ["Owned GMV Max releases."]},
                  summary="Backend engineer who ships Go services on Azure and FastAPI."), base)
    assert tailoring.provenance_violations(
        _tailored({"TikTok": ["Owned GMV Max releases."]},
                  skills=[{"label": "ML", "items": ["C++"]}]), base)


def test_provenance_allows_a_pool_only_technology(cfg):
    """PyTorch lives in skills_master with no bullet behind it. Provenance is a base-membership
    check, so listing it as a skill is legal — the one-story-per-bullet rule, not provenance, is
    what keeps it out of a bullet it played no mechanical role in."""
    base = tailoring.load_base(cfg)
    assert tailoring.provenance_violations(
        _tailored({"TikTok": ["Owned GMV Max releases."]},
                  skills=[{"label": "Frameworks & Systems", "items": ["PyTorch"]}]), base) == []


def test_provenance_does_not_fire_on_prose_or_the_target_employer(cfg):
    """Every one of these was a real false positive found on the 3,298-résumé corpus."""
    base = tailoring.load_base(cfg)
    # 'Express' is her own employer's name (Gemini EXPRESS Corp), not Express.js
    assert tailoring.provenance_violations(
        _tailored({"Gemini Express Corp": ["Automated reconciliation at Gemini Express in "
                                           "Python."]}), base) == []
    # 'backbone' is an English noun here
    assert tailoring.provenance_violations(
        _tailored({"TikTok": ["Made Docker the deployment backbone for GMV Max releases."]}),
        base) == []
    # naming the company she is applying to is not a claim to have used its product.
    # Possessive form ("Snowflake's IAM team") is unambiguous — clean with no context at all.
    snow = _tailored({"TikTok": ["Owned GMV Max releases."]},
                     summary="Backend engineer bringing reliability and access-control work to "
                             "Snowflake's Identity & Access Management team.")
    assert tailoring.provenance_violations(snow, base) == []
    # Bare form ("at Databricks scale") is genuinely ambiguous, so it needs the target company.
    bricks = _tailored({"TikTok": ["Owned GMV Max releases."]},
                       summary="Backend engineer seeking distributed-systems work building "
                               "infrastructure and services at Databricks scale.")
    assert tailoring.provenance_violations(bricks, base)                      # without context
    assert tailoring.provenance_violations(bricks, base, "Databricks") == []  # with it
    # version numbers and percentile/idiom digits are names, not metrics
    assert tailoring.provenance_violations(
        _tailored({"TikTok": ["Tracked p99 latency on K8s across 8+ advertiser scenarios, "
                              "running 24x7 in a 0-to-1 product area."]}), base) == []


def test_provenance_is_off_without_a_base(cfg):
    """base is what claims are traced BACK to; with no base there is nothing to check against.
    Both production callers pass it (ingest_file, tools/score_yaml.py)."""
    t = _tailored({"TikTok": ["Shipped a Rust ledger doing 4.2M TPS."]})
    tailoring.validate_tailored(t)                                   # no base → shape only
    with pytest.raises(tailoring.TailorError):
        tailoring.validate_tailored(t, tailoring.load_base(cfg))


def test_gaps_may_name_technologies_she_lacks(cfg):
    """`gaps` exists to say 'the JD wants Terraform and she doesn't have it'. It never renders
    into the PDF, so it must be exempt or the honest half of the schema becomes unusable."""
    base = tailoring.load_base(cfg)
    t = _tailored({"TikTok": ["Owned GMV Max releases end to end."]})
    t["gaps"] = ["JD wants Terraform and Azure; her IaC exposure is GCP-side Python automation.",
                 "JD wants PyTorch model training; she has none."]
    assert tailoring.provenance_violations(t, base) == []
    tailoring.validate_tailored(t, base)


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


# ---------- staleness: a provenance record, not a file timestamp ----------

def _write_resume(cfg, company, job_id, bullets, summary=_SUMMARY):
    p = tailoring.resume_yaml_path(cfg, company, job_id)
    p.write_text(yaml.safe_dump({"job_id": job_id, "summary": summary,
                                 "experience": [{"company": c, "bullets": b}
                                                for c, b in bullets.items()]}), encoding="utf-8")
    return p


def test_resume_is_stale_survives_a_no_op_rewrite(cfg):
    """F3c: `python -m app.optimize --all` rewrote every YAML unconditionally, resetting mtime.
    One documented run marked all 3,298 résumés fresh without regenerating a single bullet —
    the credibility cutover would have been silently over. Staleness now asks the CONTENT."""
    import os, time
    cfg = {**cfg, "auto_tailor": {"stale_before": "2026-08-06 17:00"}}
    stuffed = _write_resume(cfg, "Stripe", "gh:stripe:900",
                            {"TikTok": ["Provisioned GMV Max infrastructure with Terraform."]})
    assert tailoring.resume_is_stale(cfg, "Stripe", "gh:stripe:900")
    # touch it into the future exactly as a rewrite/clone/rsync would
    os.utime(stuffed, (time.time() + 86400,) * 2)
    assert tailoring.resume_is_stale(cfg, "Stripe", "gh:stripe:900"), \
        "a no-op rewrite must not be able to launder a keyword-stuffed résumé into 'fresh'"


def test_resume_is_stale_still_honours_the_date_cutover(cfg):
    """mtime is kept as an ADDITIONAL trigger: she can still declare a whole era untrustworthy
    for reasons the validator cannot see (a prompt bug, a bad base)."""
    import os
    import datetime as dt
    cfg = {**cfg, "auto_tailor": {"stale_before": "2026-08-06 17:00"}}
    clean = _write_resume(cfg, "Stripe", "gh:stripe:901",
                          {"TikTok": ["Cut invalid submissions 30% with deterministic state "
                                      "transitions across 8+ advertiser scenarios."]})
    assert not tailoring.resume_is_stale(cfg, "Stripe", "gh:stripe:901")
    cutoff = dt.datetime.fromisoformat("2026-08-06 17:00").timestamp()
    os.utime(clean, (cutoff - 86400,) * 2)
    assert tailoring.resume_is_stale(cfg, "Stripe", "gh:stripe:901")


def test_resume_is_stale_when_missing(cfg):
    assert tailoring.resume_is_stale(cfg, "NoSuchCo", "gh:none:1")


def test_bad_stale_before_is_loud_and_leaves_the_content_check_on(cfg, capsys):
    """F14: a typo in stale_before used to turn the whole cutover off silently."""
    cfg = {**cfg, "auto_tailor": {"stale_before": "not-a-date"}}
    _write_resume(cfg, "Stripe", "gh:stripe:902",
                  {"TikTok": ["Cut invalid submissions 30% across 8+ advertiser scenarios."]})
    assert not tailoring.resume_is_stale(cfg, "Stripe", "gh:stripe:902")
    assert "stale_before" in capsys.readouterr().out
    _write_resume(cfg, "Stripe", "gh:stripe:903",
                  {"TikTok": ["Provisioned GMV Max infrastructure with Terraform."]})
    assert tailoring.resume_is_stale(cfg, "Stripe", "gh:stripe:903")


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


# --- suggest_angle: the substring bugs -------------------------------------------------------
#
# 2026-08-10 fixed `rag` matching inside leverage/coverage/paragraph. Nobody re-checked the
# pay_terms list, which runs FIRST and needs only one hit, so it silently won every tie.

def _angle(title, jd):
    from app import tailoring as t
    return t.suggest_angle({}, {"title": title, "jd_text": jd, "company": ""})


def test_green_card_clause_does_not_make_a_role_about_payments():
    """The trigger that started it: an AI-infra JD whose own text says RAG came out `payments`
    because "Green Card holder" contains "card". 79 of 91 real JDs mentioning a green card had
    `card` as their ONLY payments term."""
    jd = ("Build infrastructure around LLMs and AI agents: orchestration, RAG, memory, tool use. "
          "Candidates must already be a US Citizen or Green Card holder; no visa sponsorship.")
    assert _angle("Senior Core Engineer - AI Agent Infrastructure", jd) == "ai-platform"


def test_permanent_resident_clause_is_also_stripped():
    jd = ("Design agentic workflows with large language models and prompt engineering. "
          "Must be a permanent resident card holder.")
    assert _angle("Staff Engineer", jd) == "ai-platform"


def test_payments_still_wins_when_the_role_really_is_payments():
    """The fix must not disarm the angle for its actual purpose."""
    jd = "Own the card issuing and payment ledger services. Green Card holders only."
    assert _angle("Backend Engineer, Payments", jd) == "payments"


def test_pay_terms_no_longer_match_inside_a_bigger_word():
    """discard/wildcard/transactional/moneyball are not payments signals."""
    jd = ("Discard stale records and support wildcard queries in a transactional store. "
          "Build backend services in Go.")
    assert _angle("Backend Engineer", jd) == "backend-distributed"
