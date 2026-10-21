"""Application drafts must use the same per-role identity as the form and PDF."""
import copy

import pytest

from app import ai


@pytest.fixture
def identity_fixture(monkeypatch, tmp_path):
    cfg = {
        "profile": {
            "first_name": "Alex", "last_name": "Example", "preferred_name": "Alex",
            "legal_first_name": "Alex", "email": "sample-5803d105@example.com",
            "work_authorization_answer": "Authorized; no sponsorship needed.",
            "legal_facts": ["Confirmed legal fact."],
        },
        "apply_profiles": {
            "default": "alex", "companies": {"caseycompany": "casey"},
            "profiles": {
                "casey": {"display_name": "Casey Example", "first_name": "Casey",
                           "preferred_name": "Casey", "email": "sample-5fbfd918@example.com"},
                "alex": {"display_name": "Alex Example", "first_name": "Alex",
                           "preferred_name": "Alex", "email": "sample-5803d105@example.com"},
            },
        },
        "ai": {"quality_max_tries": 1},
    }
    base = {"name": "Alex Example", "education": [], "experience": [], "skills_master": {}}
    monkeypatch.setattr(ai.tailoring, "load_base", lambda _: base)
    monkeypatch.setattr(ai.tailoring, "resume_yaml_path", lambda *args: tmp_path / "absent.yaml")
    return cfg, base


def role(card=None, company_norm="example"):
    return {"job_id": "ab:example:123", "company": "Example", "company_norm": company_norm,
            "title": "Software Engineer", "apply_profile": card}


def test_casey_and_alex_facts_keep_email_name_pair_and_legal_name(identity_fixture):
    cfg, base = identity_fixture
    before_cfg, before_base = copy.deepcopy(cfg), copy.deepcopy(base)
    casey = ai._facts(cfg, role("casey"))
    alex = ai._facts(cfg, role("alex"))

    assert casey.splitlines()[0] == "Name: Casey Example"
    assert "Application email: sample-5fbfd918@example.com" in casey
    assert "Preferred name: Casey Example" in casey
    assert "Legal name: Alex Example" in casey
    assert "sample-5803d105@example.com" not in casey
    assert alex.splitlines()[0] == "Name: Alex Example"
    assert "Application email: sample-5803d105@example.com" in alex
    assert "sample-5fbfd918@example.com" not in alex
    assert "Confirmed legal fact." in casey and "Authorized; no sponsorship needed." in casey
    assert cfg == before_cfg and base == before_base


def test_role_override_company_card_and_default_share_existing_precedence(identity_fixture):
    cfg, _ = identity_fixture
    assert ai._facts(cfg, role(company_norm="caseycompany")).startswith("Name: Casey Example\n")
    assert ai._facts(cfg, role("alex", "caseycompany")).startswith("Name: Alex Example\n")
    assert ai._facts(cfg, role()).startswith("Name: Alex Example\n")


def test_no_card_keeps_another_workspace_identity(identity_fixture):
    cfg, base = identity_fixture
    cfg["apply_profiles"] = {}
    cfg["profile"] = {"first_name": "Other", "last_name": "Candidate", "email": "other@example.test"}
    base["name"] = "Other Candidate"
    facts = ai._facts(cfg, role())
    assert facts.startswith("Name: Other Candidate\nApplication email: other@example.test\n")
    assert "Casey" not in facts and "Alex" not in facts


def test_answer_request_system_and_facts_use_the_same_role_card(identity_fixture, monkeypatch):
    cfg, _ = identity_fixture
    requests = []
    monkeypatch.setattr(ai, "_ready", lambda _: True)
    monkeypatch.setattr(ai, "_lessons", lambda: "")
    monkeypatch.setattr(ai.answer_log, "log", lambda *a, **kw: None)

    def complete(_cfg, **kwargs):
        requests.append(kwargs)
        return '{"0": "Casey Example"}'

    monkeypatch.setattr(ai, "complete", complete)
    result = ai.answer_questions(cfg, role("casey"), ["Name"])
    assert result == {"Name": "Casey Example"}
    assert "candidate, Casey Example." in requests[0]["system"]
    assert "candidate, Alex Example." not in requests[0]["system"]
    assert "Name: Casey Example\nApplication email: sample-5fbfd918@example.com" in requests[0]["user"]


def test_cover_letter_facts_keep_the_role_identity(identity_fixture, monkeypatch):
    cfg, _ = identity_fixture
    requests = []
    monkeypatch.setattr(ai, "_ready", lambda _: True)
    monkeypatch.setattr(ai, "_lessons", lambda: "")
    monkeypatch.setattr(ai.answer_log, "log", lambda *a, **kw: None)
    monkeypatch.setattr(ai, "_critique", lambda *a: {"pass": True, "issues": [], "fix": ""})

    def complete(_cfg, **kwargs):
        requests.append(kwargs)
        return "I build reliable software."

    monkeypatch.setattr(ai, "complete", complete)
    assert ai.cover_letter(cfg, role("casey")) == "I build reliable software."
    assert "Alex Example" not in requests[0]["system"]
    assert "Name: Casey Example\nApplication email: sample-5fbfd918@example.com" in requests[0]["user"]
