"""The autofill answer log (app/answer_log.py) + the ai.py hooks that feed it.

Why these exist: until 2026-08-20 generated answers died with the page DOM — one real form
got "I can't call that API" typed into it and there was no record to review. These tests pin
the three behaviors the eval pipeline depends on: every outcome is persisted, [NEEDS_HUMAN]
questions are left blank instead of answered, and eval lessons feed back into the prompt.
"""
import json
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
        "Do you require visa sponsorship?",
    ])
    assert out == {"Current job title?": "Software Engineer"}   # blank ≠ "I can't do that"
    by_reason = {}
    for r in _records(log_path):
        if r["kind"] == "unanswered":
            by_reason[r["reason"]] = r["question"]
        else:
            assert r["kind"] == "answer" and r["answer"] == "Software Engineer"
    assert "sponsorship" in by_reason["never_filter"]
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
