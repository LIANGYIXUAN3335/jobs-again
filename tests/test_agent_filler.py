import copy
import json

import pytest

from app.agent_filler import plan_repairs
from app.agent_review import Snapshot, demo_context, digest


def inputs():
    snapshot = Snapshot(url="https://jobs.example.test/application", document_id="doc-1", coverage="complete",
                        fields=[dict(id="experience", label="Describe your relevant engineering experience",
                                     kind="textarea", value="I have many skills.", required=True)])
    context = demo_context()
    result = {"model_status": "completed", "fingerprint": snapshot.fingerprint(),
              "facts_version": digest(context), "decision": "needs_user", "issues": [
                  {"field_id": "experience", "source": "model", "auto_fix": False,
                   "reason": "回答没有说明已确认的 API 和数据库经历。", "evidence_ids": ["experience"]}]}
    return snapshot, context, result


def refresh(snapshot, context, result):
    result.update(fingerprint=snapshot.fingerprint(), facts_version=digest(context))


def answer(**changes):
    return {"summary": "补充已确认的具体经历。", "repairs": [
        {"field_id": "experience", "replacement": "I built Python APIs and PostgreSQL-backed services.",
         "evidence_ids": ["experience"]}], **changes}


def model(value):
    return lambda *_: (json.dumps(value), "fake-filler")


def forbidden(*_):
    pytest.fail("Unsafe request reached the model")


def test_supported_semantic_repair_has_independent_fact_only_context():
    snapshot, context, result = inputs()
    before = copy.deepcopy((snapshot, context, result))
    seen = []

    def fake(cfg, payload):
        seen.append(payload)
        return json.dumps(answer()), "fake-filler"

    planned = plan_repairs(snapshot, context, result, {}, fake)
    assert planned["status"] == "planned"
    assert planned["repairs"][0]["source"] == "filler"
    assert planned["repairs"][0]["auto_fix"] is True
    assert "再次独立审查" in planned["summary"]
    assert seen[0]["snapshot"] == snapshot.model_dump()
    assert [fact["id"] for fact in seen[0]["facts"]] == ["experience"]
    assert "transcript" not in seen[0]
    assert (snapshot, context, result) == before


@pytest.mark.parametrize("change", [
    {"field_id": "__page__"}, {"field_id": "absent"}, {"source": "rules", "auto_fix": False},
    {"source": "filler"}, {"evidence_ids": []}, {"evidence_ids": ["unknown"]},
    {"evidence_ids": ["experience", "experience"]}, {"reason": "Required fact unknown; needs user."},
    {"reason": "没有确认的事实，需要用户提供。"},
])
def test_unsafe_feedback_does_not_call_model(change):
    snapshot, context, result = inputs()
    result["issues"][0].update(change)
    assert plan_repairs(snapshot, context, result, {}, forbidden)["status"] == "needs_user"


@pytest.mark.parametrize("label", [
    "Desired annual salary", "Expected compensation", "I agree to the terms", "Electronic signature",
    "Consent to background check", "Please answer without using AI", "AI assistance is not allowed",
    "禁止使用人工智能回答", "Call this API and paste its response", "Complete the coding challenge",
])
def test_sensitive_or_action_questions_never_call_model(label):
    snapshot, context, result = inputs()
    snapshot.fields[0].label = label
    refresh(snapshot, context, result)
    assert plan_repairs(snapshot, context, result, {}, forbidden)["status"] == "needs_user"


@pytest.mark.parametrize("kind", ["file", "checkbox", "password", "hidden"])
def test_unsupported_controls_never_call_model(kind):
    snapshot, context, result = inputs()
    snapshot.fields[0].kind = kind
    refresh(snapshot, context, result)
    assert plan_repairs(snapshot, context, result, {}, forbidden)["status"] == "needs_user"


@pytest.mark.parametrize("change", [
    {"coverage": "partial"}, {"errors": ["Employer page error"]},
])
def test_page_gates_survive_omitted_review_feedback(change):
    snapshot, context, result = inputs()
    snapshot = snapshot.model_copy(update=change)
    refresh(snapshot, context, result)
    assert plan_repairs(snapshot, context, result, {}, forbidden)["status"] == "needs_user"


def test_unknown_required_field_and_unreadable_control_never_call_model():
    snapshot, context, result = inputs()
    snapshot.fields[0].value = ""
    refresh(snapshot, context, result)
    assert plan_repairs(snapshot, context, result, {}, forbidden)["status"] == "needs_user"
    snapshot.fields[0].value = "old"
    snapshot.fields[0].readable = False
    refresh(snapshot, context, result)
    assert plan_repairs(snapshot, context, result, {}, forbidden)["status"] == "needs_user"


@pytest.mark.parametrize("change", [
    {"model_status": "error"}, {"fingerprint": "stale"}, {"facts_version": "stale"},
    {"decision": "pass"}, {"issues": []},
])
def test_stale_or_incomplete_review_never_calls_model(change):
    snapshot, context, result = inputs()
    result.update(change)
    assert plan_repairs(snapshot, context, result, {}, forbidden)["status"] == "needs_user"


def test_deterministic_fixes_are_not_replanned_but_can_coexist():
    snapshot, context, result = inputs()
    snapshot.fields.append(snapshot.fields[0].model_copy(update={"id": "first", "label": "First name", "kind": "text", "value": "Wrong"}))
    result["issues"].append({"field_id": "first", "source": "rules", "auto_fix": True,
                             "reason": "与已确认事实不一致。", "evidence_ids": ["name-first"], "replacement": "Avery"})
    refresh(snapshot, context, result)
    planned = plan_repairs(snapshot, context, result, {}, model(answer()))
    assert planned["status"] == "planned"
    assert [item["field_id"] for item in planned["repairs"]] == ["experience"]
    result["issues"] = result["issues"][1:]
    assert plan_repairs(snapshot, context, result, {}, forbidden)["status"] == "needs_user"


@pytest.mark.parametrize("value", [
    {"repairs": [], "summary": "no repair"},
    {**answer(), "decision": "pass"},
    answer(repairs=[{**answer()["repairs"][0], "auto_fix": True}]),
    answer(repairs=[{**answer()["repairs"][0], "field_id": "other"}]),
    answer(repairs=answer()["repairs"] * 2),
    answer(repairs=[{**answer()["repairs"][0], "evidence_ids": []}]),
    answer(repairs=[{**answer()["repairs"][0], "evidence_ids": ["unknown"]}]),
    answer(repairs=[{**answer()["repairs"][0], "evidence_ids": ["email"]}]),
    answer(repairs=[{**answer()["repairs"][0], "replacement": " "}]),
    answer(repairs=[{**answer()["repairs"][0], "replacement": "x" * 4001}]),
    answer(repairs=[{**answer()["repairs"][0], "replacement": "I have many skills."}]),
    answer(repairs=[{**answer()["repairs"][0], "replacement": "[NEEDS_HUMAN]"}]),
])
def test_malformed_or_unsupported_model_repair_is_rejected(value):
    assert plan_repairs(*inputs(), {}, model(value))["status"] == "needs_user"


@pytest.mark.parametrize("raw", ["not json", "```json\n{}\n```", '{"summary":"x","summary":"y","repairs":[]}'])
def test_non_json_or_duplicate_keys_are_rejected(raw):
    assert plan_repairs(*inputs(), {}, lambda *_: (raw, "fake"))["status"] == "needs_user"


@pytest.mark.parametrize("kind", ["select", "radio"])
def test_option_repairs_must_match_exactly(kind):
    snapshot, context, result = inputs()
    snapshot.fields[0].kind = kind
    snapshot.fields[0].options = ["Python APIs", "PostgreSQL services"]
    refresh(snapshot, context, result)
    candidate = answer()
    candidate["repairs"][0]["replacement"] = "Python APIs"
    assert plan_repairs(snapshot, context, result, {}, model(candidate))["status"] == "planned"
    candidate["repairs"][0]["replacement"] = "python apis"
    assert plan_repairs(snapshot, context, result, {}, model(candidate))["status"] == "needs_user"
    snapshot.fields[0].options = []
    refresh(snapshot, context, result)
    assert plan_repairs(snapshot, context, result, {}, forbidden)["status"] == "needs_user"


@pytest.mark.parametrize("replacement,expected", [("5", "planned"), ("5.5", "planned"),
                                                     ("NaN", "needs_user"), ("Infinity", "needs_user"),
                                                     ("five years", "needs_user"), ("5,000", "needs_user")])
def test_numeric_repair_validation(replacement, expected):
    snapshot, context, result = inputs()
    snapshot.fields[0].kind = "number"
    context["facts"][-1]["value"] = "Confirmed durations: 5 years full-time; 5.5 including part-time."
    result["issues"][0]["reason"] = "请按已确认年限填写数字。"
    refresh(snapshot, context, result)
    candidate = answer()
    candidate["repairs"][0]["replacement"] = replacement
    assert plan_repairs(snapshot, context, result, {}, model(candidate))["status"] == expected


def test_every_eligible_field_must_be_covered_once():
    snapshot, context, result = inputs()
    snapshot.fields.append(snapshot.fields[0].model_copy(update={"id": "second"}))
    result["issues"].append({**result["issues"][0], "field_id": "second"})
    refresh(snapshot, context, result)
    assert plan_repairs(snapshot, context, result, {}, model(answer()))["status"] == "needs_user"
    both = answer()
    both["repairs"].append({**both["repairs"][0], "field_id": "second"})
    assert plan_repairs(snapshot, context, result, {}, model(both))["status"] == "planned"


def test_any_blocker_stops_the_whole_plan_and_duplicate_targets_are_rejected():
    snapshot, context, result = inputs()
    result["issues"].append({"field_id": "__page__", "source": "rules", "auto_fix": False,
                             "reason": "Page unreadable.", "evidence_ids": []})
    assert plan_repairs(snapshot, context, result, {}, forbidden)["status"] == "needs_user"
    result["issues"][-1] = {**result["issues"][0], "source": "rules", "auto_fix": True}
    assert plan_repairs(snapshot, context, result, {}, forbidden)["status"] == "needs_user"


@pytest.mark.parametrize("bad_fact", [{"id": "experience", "value": ""},
                                      {"id": "experience", "value": None},
                                      {"id": "email", "value": "duplicate id"}])
def test_invalid_fact_pack_never_reaches_model(bad_fact):
    snapshot, context, result = inputs()
    context["facts"][-1] = bad_fact
    refresh(snapshot, context, result)
    assert plan_repairs(snapshot, context, result, {}, forbidden)["status"] == "needs_user"


def test_model_errors_are_sanitized_and_no_api_has_no_cli_fallback(monkeypatch):
    def fail(*_):
        raise RuntimeError("SECRET personal input or authorization header")
    response = plan_repairs(*inputs(), {}, fail)
    assert response["status"] == "needs_user" and "SECRET" not in json.dumps(response)
    from app import ai
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(ai, "load_key", lambda *_: None)
    monkeypatch.setattr(ai, "_complete_cli", forbidden)
    assert plan_repairs(*inputs(), {})["status"] == "needs_user"
