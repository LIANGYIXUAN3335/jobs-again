"""Drafter → reviewer second pass: when it fires, and what it must never say."""
from dataclasses import dataclass, field

import pytest

from app import tailoring


@dataclass
class FakeScore:
    overall: int = 70
    verdict: str = "improve"
    skills_coverage: int = 60
    title_alignment: int = 80
    achievements: int = 70
    parseability: int = 100
    summary_quality: int = 100
    summary_flags: list = field(default_factory=list)
    matched_hard: list = field(default_factory=lambda: ["python"])
    missing_hard: list = field(default_factory=list)
    matched_soft: list = field(default_factory=list)
    missing_soft: list = field(default_factory=list)
    coverable: list = field(default_factory=list)
    true_gaps: list = field(default_factory=list)
    family: str = "eng"


class FakeConn:
    pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    cfg = {"output_dirs": {"prompts": str(prompts), "resumes": str(tmp_path)},
           "tailor_review": {"enabled": True, "below_score": 85}}

    role = {"company": "Acme", "title": "Backend Engineer",
            "jd_text": "We need Python and Kubernetes.", "location": "SF",
            "apply_url": "https://acme.example/jobs/1"}
    monkeypatch.setattr(tailoring.store, "get_role", lambda conn, jid: role)
    monkeypatch.setattr(tailoring, "load_base",
                        lambda c: {"name": "Casey", "jobs": [{"company": "TikTok"}]})
    monkeypatch.setattr(tailoring.cfgmod, "load_company_notes", lambda c: {})
    monkeypatch.setattr(tailoring, "prompt_path",
                        lambda c, company, jid: prompts / f"{company}_{jid}.md")
    return cfg, role


def _write(env, sc, tailored=None):
    cfg, _ = env
    return tailoring.write_review_prompt(
        cfg, FakeConn(), "gh:acme:1", tailored or {"summary": "did things"}, sc)


# ---------------------------------------------------------------- when it fires

def test_low_score_triggers_a_review(env):
    assert _write(env, FakeScore(overall=70)) is not None


def test_high_clean_score_does_not(env):
    assert _write(env, FakeScore(overall=92, coverable=[], summary_flags=[])) is None


def test_summary_flags_trigger_even_at_a_high_score(env):
    assert _write(env, FakeScore(overall=95, summary_flags=["puffery"])) is not None


def test_leftover_coverable_keyword_triggers_even_at_a_high_score(env):
    """A keyword that is TRUE for her and still missing is free points — always review."""
    assert _write(env, FakeScore(overall=95, coverable=["kubernetes"])) is not None


def test_disabled_in_config_writes_nothing(env):
    cfg, _ = env
    cfg["tailor_review"]["enabled"] = False
    assert _write(env, FakeScore(overall=10)) is None


def test_below_score_threshold_is_respected(env):
    cfg, _ = env
    cfg["tailor_review"]["below_score"] = 50
    assert _write(env, FakeScore(overall=70, coverable=[], summary_flags=[])) is None


# ---------------------------------------------------------------- content

def test_review_file_lands_next_to_the_tailor_prompt(env):
    p = _write(env, FakeScore())
    assert p.name.endswith("_review.md") and p.exists()


def test_measured_numbers_are_in_the_prompt(env):
    txt = _write(env, FakeScore(overall=71, skills_coverage=62)).read_text()
    assert "overall **71**" in txt and "skills_coverage 62" in txt


def test_true_gaps_carry_an_explicit_never_claim_instruction(env):
    txt = _write(env, FakeScore(true_gaps=["rust", "kafka"])).read_text()
    assert "rust, kafka" in txt
    assert "Never propose adding these" in txt


def test_coverable_and_true_gaps_are_not_conflated(env):
    txt = _write(env, FakeScore(coverable=["python"], true_gaps=["rust"])).read_text()
    cov = txt.index("**Coverable**")
    gap = txt.index("**True gaps**")
    assert txt.index("python") > cov and txt.index("python") < gap
    assert txt.index("rust") > gap


def test_jd_is_fenced_as_untrusted(env):
    txt = _write(env, FakeScore()).read_text()
    assert "untrusted third-party data" in txt


def test_reviewer_is_told_not_to_rewrite(env):
    txt = _write(env, FakeScore()).read_text()
    assert "Critique, do not rewrite" in txt
    assert "Do not emit a whole new YAML" in txt


def test_relevance_weighted_cutting_rule_is_present(env):
    txt = _write(env, FakeScore()).read_text()
    assert "Never cut by recency" in txt


def test_draft_and_base_are_both_inlined(env):
    txt = _write(env, FakeScore(), tailored={"summary": "UNIQUE_DRAFT_MARKER"}).read_text()
    assert "UNIQUE_DRAFT_MARKER" in txt and "Casey" in txt


# ---------------------------------------------------------------- robustness

def test_a_reviewer_failure_never_breaks_the_ingest(env, monkeypatch):
    """ingest_file wraps this in try/except — verify the contract holds here too."""
    cfg, _ = env
    monkeypatch.setattr(tailoring.store, "get_role", lambda conn, jid: None)
    assert tailoring.write_review_prompt(cfg, FakeConn(), "x", {}, FakeScore()) is None
