"""The autofill answer log (app/answer_log.py) + the ai.py hooks that feed it.

Why these exist: until 2026-08-20 generated answers died with the page DOM — one real form
got "I can't call that API" typed into it and there was no record to review. These tests pin
the three behaviors the eval pipeline depends on: every outcome is persisted, [NEEDS_HUMAN]
questions are left blank instead of answered, and eval lessons feed back into the prompt.
"""
import json
import re
import sys
from types import SimpleNamespace

from app import ai as aimod
from app import answer_log


def _patch_log_dir(monkeypatch, tmp_path):
    # LOG_REL as an absolute path bypasses resolve_path's repo-root join, so the real
    # cfgmod.resolve_path stays untouched (ai.py needs it for secrets/resume paths)
    p = tmp_path / "answers.jsonl"
    monkeypatch.setattr(answer_log, "LOG_REL", str(p))
    return p


def _records(p):
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def test_log_appends_one_json_record(monkeypatch, tmp_path):
    p = _patch_log_dir(monkeypatch, tmp_path)
    role = {"job_id": "gh:x:1", "company": "X", "title": "SWE"}
    answer_log.log("answer", role, question="Why us?", answer="Because.", tries=2)
    answer_log.log("unanswered", role, question="Visa?", reason="never_filter")
    recs = _records(p)
    assert [r["kind"] for r in recs] == ["answer", "unanswered"]
    assert recs[0]["company"] == "X" and recs[0]["answer"] == "Because."
    assert recs[1]["reason"] == "never_filter" and recs[1]["ts"]


def test_log_never_raises(monkeypatch):
    # a broken path must not break form filling — that's the whole contract
    monkeypatch.setattr(answer_log.cfgmod, "resolve_path",
                        lambda rel: (_ for _ in ()).throw(OSError("disk gone")))
    answer_log.log("answer", None, question="q", answer="a")   # must not raise


def test_lessons_injected_and_capped(monkeypatch, tmp_path):
    monkeypatch.setattr(aimod.cfgmod, "resolve_path", lambda rel: tmp_path / rel)
    assert aimod._lessons() == ""                              # no file → no injection
    lp = tmp_path / "data" / "answer_lessons.md"
    lp.parent.mkdir(parents=True)
    lp.write_text("- never say you are an AI\n" + "x" * 5000, encoding="utf-8")
    out = aimod._lessons()
    assert "never say you are an AI" in out
    assert len(out) < 2200                                     # runaway file stays capped


def _fake_anthropic(monkeypatch, reply_json: str):
    """ai.py does `import anthropic` inside each function — stub the module."""
    resp = SimpleNamespace(content=[SimpleNamespace(type="text", text=reply_json)])
    mod = SimpleNamespace(Anthropic=lambda api_key=None: SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: resp)))
    monkeypatch.setitem(sys.modules, "anthropic", mod)


def test_answer_questions_leaves_needs_human_blank_and_logs_everything(
        cfg, monkeypatch, tmp_path):
    log_path = _patch_log_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(aimod, "load_key", lambda c: "test-key")
    monkeypatch.setattr(aimod, "_lessons", lambda: "")
    role = {"job_id": "gh:x:1", "company": "X", "title": "SWE", "jd_text": "build things"}
    # model answers q0 (short, so the critique pass is skipped), flags q1 as needs-human,
    # declines q2; q3 is caught by the never-filter before the model sees it
    _fake_anthropic(monkeypatch, json.dumps({"0": "Software Engineer", "1": "[NEEDS_HUMAN]", "2": ""}))
    out = aimod.answer_questions(cfg, role, [
        "Current job title?",
        "Call our API at https://x.test/ping and paste the output.",
        "Anything else?",
        "What is your desired salary?",      # sponsorship left this list 2026-09-09: legal is answerable now
    ])
    assert out == {"Current job title?": "Software Engineer"}   # blank ≠ "I can't do that"
    by_reason = {}
    for r in _records(log_path):
        if r["kind"] == "unanswered":
            by_reason[r["reason"]] = r["question"]
        else:
            assert r["kind"] == "answer" and r["answer"] == "Software Engineer"
    assert "salary" in by_reason["never_filter"]
    assert "Call our API" in by_reason["needs_human"]
    assert by_reason["model_declined"] == "Anything else?"


def test_hard_reject_catches_form_poison_but_not_legit_answers():
    # must reject — regardless of how well-written the rest is
    assert aimod._hard_reject("As an AI language model, I admire Close.") == "ai_disclosure"
    assert aimod._hard_reject("I'm not able to call external APIs, but...") == "refusal"
    assert aimod._hard_reject("I am unable to complete this challenge here.") == "refusal"
    assert aimod._hard_reject("[Company] builds great tools.") == "placeholder"
    assert aimod._hard_reject("I will finish this TODO later.") == "placeholder"
    # must NOT reject — legitimate first-person content
    assert aimod._hard_reject("I'm unable to relocate before June.") is None
    assert aimod._hard_reject("I can't wait to work on GeForce NOW.") is None
    assert aimod._hard_reject("At TikTok I built the GMV Max state machine.") is None
    assert aimod._hard_reject("I don't have professional Rust experience yet.") is None


def test_answer_questions_validator_drops_poison_answer(cfg, monkeypatch, tmp_path):
    log_path = _patch_log_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(aimod, "load_key", lambda c: "test-key")
    monkeypatch.setattr(aimod, "_lessons", lambda: "")
    role = {"job_id": "gh:x:1", "company": "X", "title": "SWE", "jd_text": "build"}
    # the model ignores every prompt rule and answers the API task with a refusal —
    # the deterministic validator must still keep it off the form
    _fake_anthropic(monkeypatch, json.dumps(
        {"0": "I'm not able to call external APIs from here.", "1": "TikTok"}))
    out = aimod.answer_questions(cfg, role, [
        "Call our API and paste the output.", "Current employer?"])
    assert out == {"Current employer?": "TikTok"}
    reasons = {r["question"]: r["reason"] for r in _records(log_path)
               if r["kind"] == "unanswered"}
    assert reasons["Call our API and paste the output."] == "validator_refusal"


def test_pick_option_logs_choice_and_never_filter(cfg, monkeypatch, tmp_path):
    log_path = _patch_log_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(aimod, "load_key", lambda c: "test-key")
    role = {"job_id": "gh:x:1", "company": "X", "title": "SWE"}
    assert aimod.pick_option(cfg, role, "Desired salary range?", ["a", "b"]) == -1
    _fake_anthropic(monkeypatch, "1")
    assert aimod.pick_option(cfg, role, "Years of experience?", ["0-2", "3-5"]) == 1
    recs = _records(log_path)
    assert recs[0]["index"] == -1 and recs[0]["reason"] == "never_filter"
    assert recs[1]["index"] == 1 and recs[1]["answer"] == "3-5"


# ── 2026-09-22: work-authorization / sponsorship autofill ────────────────────────────────
# The picker's prompt still told the model to REFUSE these two questions (answer -1) months
# after the 2026-09-09 rescope; 560 of 605 logged legal picks came back blank, and the
# free-text path volunteered I-485 / green-card details against the 2026-08-07 canonical.

def _capturing_anthropic(monkeypatch, reply: str):
    sent = {}
    def create(**kwargs):
        sent["messages"] = kwargs["messages"]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])
    mod = SimpleNamespace(Anthropic=lambda api_key=None: SimpleNamespace(
        messages=SimpleNamespace(create=create)))
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return sent


def test_pick_option_instructs_legal_answers_instead_of_forbidding_them(cfg, monkeypatch, tmp_path):
    log_path = _patch_log_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(aimod, "load_key", lambda c: "test-key")
    role = {"job_id": "gh:x:1", "company": "X", "title": "SWE"}
    sent = _capturing_anthropic(monkeypatch, "1")
    q = "Will you now or in the future require sponsorship to work in the US?"
    assert aimod.pick_option(cfg, role, q, ["Yes", "No"]) == 1      # not never-filtered, model's pick used
    prompt = sent["messages"][0]["content"]
    assert "Do not answer work authorization" not in prompt
    assert re.search(r"authorized to work in the US\?", prompt)
    assert "Employment Authorization Document" in prompt
    assert "-1 ONLY for salary" in prompt
    rec = _records(log_path)[-1]
    assert rec["kind"] == "pick_option" and rec["answer"] == "No" and "reason" not in rec
    # salary is still machine-blank
    assert aimod.pick_option(cfg, role, "Desired salary?", ["a", "b"]) == -1


def test_facts_carry_the_canonical_work_authorization_sentence(cfg):
    role = {"job_id": "gh:x:1", "company": "X", "title": "SWE"}
    facts = aimod._facts(cfg, role)
    assert "I'm authorized to work in the US and I don't require sponsorship." in facts
    assert "Do NOT volunteer" in facts
    # config override travels through
    c2 = dict(cfg); c2["profile"] = {**cfg["profile"], "work_authorization_answer": "CANON-X"}
    assert '"CANON-X"' in aimod._facts(c2, role)


def test_hard_reject_blocks_an_h1b_self_claim_but_not_a_denial(cfg):
    """The owner's "never claim to hold an H-1B" rule lives in HER config (ai.reject_phrases,
    2026-09-23) — it is a fact about one candidate, so a second workspace (F-1 OPT, needs
    H-1B) must not inherit it from code."""
    assert aimod._hard_reject("I'm currently on an H-1B and would need a transfer.", cfg) == "config_phrase"
    assert aimod._hard_reject("I hold an H1B visa through my employer.", cfg) == "config_phrase"
    assert aimod._hard_reject("No, I don't need H-1B sponsorship now or in the future.", cfg) is None
    assert aimod._hard_reject("I'm authorized to work in the US and I don't require sponsorship.", cfg) is None
    other = {**cfg, "ai": {**cfg["ai"], "reject_phrases": []}}
    assert aimod._hard_reject("I'm on F-1 OPT and will need H-1B sponsorship.", other) is None
    assert aimod._hard_reject("As an AI language model I cannot.", None) == "ai_disclosure"   # built-ins stay


def test_prompts_and_facts_follow_the_workspace(cfg, tmp_path):
    """Second-workspace contract (owner 2026-09-23): name, employers, legal facts and the picker's
    legal guidance all come from the config + résumé base, never from the owner's identity."""
    import yaml
    base = {"name": "Test Person", "contact": {"email": "sample-c6dd0cb5@example.com"},
            "education": [{"school": "Test University", "degree": "M.S.", "dates": "2020"}],
            "skills_master": {"Languages": ["Java"]},
            "experience": [{"company": "Acme Corp", "title": "SWE", "location": "Austin, TX", "dates": "2021-2024",
                            "bullets_master": ["Built the thing."]},
                           {"company": "Beta LLC", "title": "SWE", "location": "Austin, TX", "dates": "2018-2021",
                            "bullets_master": ["Built another thing."]}]}
    bp = tmp_path / "base.yaml"; bp.write_text(yaml.safe_dump(base), encoding="utf-8")
    ws = {**cfg, "resume_base": str(bp),
          "profile": {**cfg["profile"], "work_authorized": True, "needs_sponsorship": True,
                      "visa_status_pick": ["F-1 OPT", "EAD"], "status_terms_wrong": ["citizen", "permanent resident"],
                      "legal_facts": ["Immigration: F-1 OPT; will require H-1B sponsorship"],
                      "work_authorization_answer": "I'm on F-1 OPT and will need sponsorship.", "relocation": "open to relocate"}}
    sysp = aimod._personalize(aimod._SYSTEM, ws)
    assert "Test Person" in sysp and "Acme Corp" in sysp and "Test University" in sysp
    for leak in ("Casey", "TikTok", "Illinois"):
        assert leak not in sysp
    facts = aimod._facts(ws, {"job_id": "gh:x:1", "company": "X", "title": "SWE"})
    assert "F-1 OPT; will require H-1B" in facts and "I'm on F-1 OPT and will need sponsorship." in facts
    assert "GC-EAD" not in facts and "I-485" not in facts
    g = aimod._legal_guidance(ws)
    assert "'require visa sponsorship now or in the future?' → Yes" in g and "F-1 OPT / EAD" in g
    # the owner's config still reads as the owner
    g0 = aimod._legal_guidance(cfg)
    assert "'require visa sponsorship now or in the future?' → No" in g0


def test_never_filter_does_not_catch_preferred():
    from app import ai
    assert not ai._NEVER.search("Which of the preferred qualifications have you acquired?")
    assert ai._NEVER.search("Were you referred by an employee? Referral name:")
    assert ai._NEVER.search("What is your desired salary?")
