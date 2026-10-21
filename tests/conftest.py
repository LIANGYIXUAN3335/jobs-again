import pytest

from app import ai, answer_log, confirmed_answers, form_preflight, gcal, interview_ledger, login_vault
from app import config as cfgmod


@pytest.fixture(autouse=True)
def isolated_confirmed_answers(tmp_path, monkeypatch):
    """Never read or write the owner's real answers, calendar copy, form reads, logins, usage log or answer log."""
    monkeypatch.setattr(confirmed_answers, "_root", tmp_path / "answers")
    for module in (form_preflight, interview_ledger, login_vault):
        monkeypatch.setattr(module, "_root", tmp_path / "agent_lab")
    monkeypatch.setattr(ai, "USAGE_LOG", str(tmp_path / "model_usage.jsonl"))
    monkeypatch.setattr(answer_log, "LOG_REL", str(tmp_path / "answers.jsonl"))
    # gcal falls back to this token, and rewrites it when it refreshes.
    monkeypatch.setattr(gcal, "TOKEN_PATH", str(tmp_path / "token_gcal.json"))


@pytest.fixture
def cfg(tmp_path):
    """Real config (keyword maps, filters) with throwaway DB + output paths."""
    c = dict(cfgmod.load_config())
    c["db_path"] = str(tmp_path / "jobscan.db")
    c["user_db_path"] = str(tmp_path / "user_state.db")
    c["output_dirs"] = {"prompts": str(tmp_path / "prompts"),
                        "resumes": str(tmp_path / "resumes")}
    # Tests stub the Anthropic SDK; never let a test shell out to the real claude CLI.
    c["ai"] = {**(c.get("ai") or {}), "backend": "api"}
    # The real settings start caffeinate, send Mac notifications and read the real calendar at every /runner/start.
    c["agent_runner"] = {}
    return c
