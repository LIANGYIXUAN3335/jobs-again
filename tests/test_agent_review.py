import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent_lab import build_router
from app.agent_review import Snapshot, demo_context, digest, review, rule_review


def snapshot(**changes):
    data = dict(url="http://testserver/agent-lab/fixture", document_id="page-1", coverage="complete",
                fields=[dict(id="first", label="First name", kind="text", value="Avery", required=True)])
    data.update(changes)
    return Snapshot(**data)


def model_result(s, **changes):
    data = dict(decision="pass", checked_field_ids=[f.id for f in s.fields], issues=[], summary="已检查。",
                support={f.id: ["name-first"] for f in s.fields})
    data.update(changes)
    return json.dumps(data), "stub-model"


def test_answer_no_fact_establishes_cannot_pass():
    s = snapshot(fields=[dict(id="first", label="First name", kind="text", value="Avery", required=True),
                         dict(id="extra", label="Anything to add?", kind="textarea", value="Nothing limits me.")])
    for support in ({"first": ["name-first"]}, {"first": ["name-first"], "extra": []},
                    {"first": ["name-first"], "extra": ["invented"]}):
        r = review(s, demo_context(), {}, lambda *_: model_result(s, support=support))
        assert r["decision"] == "needs_user"
        assert [i["field_id"] for i in r["issues"]] == ["extra"]
    r = review(s, demo_context(), {}, lambda *_: model_result(s, support={"first": ["name-first"], "extra": ["experience"]}))
    assert r["decision"] == "pass"


@pytest.mark.parametrize("label", [
    "Are there any circumstances preventing you from traveling internationally to a week-long team offsite?",
    "Are you able to travel abroad for work?",
    "Are you subject to a non-compete agreement?",
    "Do you have any contract with a past employer limiting which companies you can join?",
    "Please confirm before submitting.",
    "Candidate Consent Acknowledgment",
    "I agree to the mutual arbitration agreement",
])
def test_topics_no_fact_covers_stop_even_when_the_model_passes(label):
    s = snapshot(fields=[dict(id="first", label="First name", kind="text", value="Avery", required=True),
                         dict(id="q", label=label, kind="text", value="No")])
    r = review(s, demo_context(), {}, lambda *_: model_result(s, support={"first": ["name-first"], "q": ["experience"]}))
    assert r["decision"] == "needs_user"
    assert [i["field_id"] for i in r["issues"]] == ["q"]
    blank = snapshot(fields=[dict(id="first", label="First name", kind="text", value="Avery", required=True),
                             dict(id="q", label=label, kind="text", value="")])
    assert review(blank, demo_context(), {}, lambda *_: model_result(blank))["decision"] == "pass"


TRAVEL = {"id": "international-travel", "topic": "travel\\w*\\s+internationally|international travel",
          "answer": "My current visa status doesn't allow international travel right now."}
OFFSITE = "Are there any circumstances preventing you from traveling internationally to a week-long team offsite?"


def test_confirmed_answer_is_accepted_and_anything_else_gets_replaced():
    ctx = {**demo_context(), "confirmed_answers": [TRAVEL]}
    ctx["facts"] = [*ctx["facts"], {"id": TRAVEL["id"], "labels": [], "value": TRAVEL["answer"]}]

    def form(value, kind="textarea"):
        return snapshot(fields=[dict(id="first", label="First name", kind="text", value="Avery", required=True),
                                dict(id="q", label=OFFSITE, kind=kind, value=value, required=True)])

    def run(s):
        return review(s, ctx, {}, lambda *_: model_result(s, support={"first": ["name-first"], "q": [TRAVEL["id"]]}))

    assert run(form(TRAVEL["answer"]))["decision"] == "pass"
    assert run(form("Yes. " + TRAVEL["answer"]))["decision"] == "pass"
    for wrong in ("No, nothing prevents that.", ""):
        result = run(form(wrong))
        assert result["decision"] == "needs_fix"
        assert result["issues"][0]["replacement"] == TRAVEL["answer"] and result["issues"][0]["auto_fix"]
    # A Yes/No control cannot take the sentence, so it stops for the owner.
    assert run(form("No", kind="radio"))["decision"] == "needs_user"
    # A model's doubt does not overrule what the owner said, and it still judges every other field.
    s = form(TRAVEL["answer"])
    doubt = [{"field_id": "q", "reason": "不确定", "evidence_ids": []}]
    r = review(s, ctx, {}, lambda *_: model_result(s, decision="needs_user", issues=doubt, support={"first": ["name-first"]}))
    assert r["decision"] == "pass" and r["issues"] == []
    other = [{"field_id": "first", "reason": "有问题", "evidence_ids": []}]
    r = review(s, ctx, {}, lambda *_: model_result(s, decision="needs_user", issues=other, support={}))
    assert r["decision"] == "needs_user" and [i["field_id"] for i in r["issues"]] == ["first"]


@pytest.mark.parametrize("label, value, flagged", [
    ("Can you legally work in the US? Please explain if you require a visa.", "No. I'm authorized to work.", True),
    ("Are you authorized to work in the United States?", "No", True),
    ("Can you legally work in the US? Please explain if you require a visa.", "Yes. I'm authorized to work.", False),
    ("Will you now or in the future require sponsorship to legally work here?", "No. I'm authorized to work.", False),
    ("Do you need a visa to work in the US?", "No", False),
])
def test_a_leading_no_on_a_may_you_work_question_stops(label, value, flagged):
    ctx = {**demo_context(), "facts": [*demo_context()["facts"], {"id": "work_authorized", "labels": [], "value": "Yes"}]}
    s = snapshot(fields=[dict(id="first", label="First name", kind="text", value="Avery", required=True),
                         dict(id="q", label=label, kind="textarea", value=value)])
    r = review(s, ctx, {}, lambda *_: model_result(s, support={"first": ["name-first"], "q": ["work_authorized"]}))
    assert (r["decision"] == "needs_user") is flagged
    assert ("q" in [i["field_id"] for i in r["issues"]]) is flagged


def test_a_malformed_reply_gets_one_more_try_then_fails_closed():
    s = snapshot()
    good, model = model_result(s)
    replies = iter([("{not json", model), (good, model)])
    assert review(s, demo_context(), {}, lambda *_: next(replies))["decision"] == "pass"
    calls = []
    r = review(s, demo_context(), {}, lambda *_: calls.append(1) or ("{not json", model))
    assert r["decision"] == "needs_user" and r["model_status"] == "error" and len(calls) == 2


def test_reasoning_around_the_json_still_gets_reviewed():
    s = snapshot()
    text, model = model_result(s)
    for wrapped in (f"Checking each field.\n\n{text}", f"```json\n{text}\n```", f"Done.\n```json\n{text}\n```\n"):
        r = review(s, demo_context(), {}, lambda *_: (wrapped, model))
        assert r["model_status"] == "completed" and r["decision"] == "pass"


def test_rules_cannot_be_overruled_by_model():
    s = snapshot()
    s.fields[0].value = "Wrong"
    r = review(s, demo_context(), {}, lambda *_: model_result(s))
    assert r["decision"] == "needs_fix"
    assert r["issues"][0]["replacement"] == "Avery"
    assert r["issues"][0]["auto_fix"]


@pytest.mark.parametrize("change", [
    {"checked_field_ids": []},
    {"checked_field_ids": ["first", "first"]},
    {"decision": "needs_user", "issues": []},
    {"decision": "pass", "issues": [{"field_id": "first", "reason": "wrong", "evidence_ids": []}]},
    {"decision": "needs_user", "issues": [{"field_id": "invented", "reason": "wrong", "evidence_ids": []}]},
    {"decision": "needs_user", "issues": [{"field_id": "first", "reason": "wrong", "evidence_ids": ["invented"]}]},
])
def test_malformed_or_incomplete_model_verdict_fails_closed(change):
    s = snapshot()
    r = review(s, demo_context(), {}, lambda *_: model_result(s, **change))
    assert r["decision"] == "needs_user"
    assert r["model_status"] == "error"


def test_model_error_is_sanitized():
    def fail(*_):
        raise RuntimeError("SECRET request header")
    r = review(snapshot(), demo_context(), {}, fail)
    assert r["decision"] == "needs_user"
    assert "SECRET" not in json.dumps(r)


@pytest.mark.parametrize("kind,value", [("checkbox", "No"), ("file", "resume.pdf"), ("text", "")])
def test_required_or_attachment_checks_cannot_pass(kind, value):
    s = snapshot(fields=[dict(id="unknown", label="Unconfirmed question", kind=kind, value=value, required=True)])
    r = review(s, demo_context(), {}, lambda *_: model_result(s))
    assert r["decision"] == "needs_user"


def test_partial_capture_and_real_attachment_require_human():
    s = snapshot(coverage="partial")
    assert rule_review(s, demo_context())[0].field_id == "__page__"
    ctx = {**demo_context(), "requires_attachment_review": True}
    assert review(snapshot(), ctx, {}, lambda *_: model_result(snapshot()))["decision"] == "needs_user"


def test_unsupported_option_never_autofixes():
    s = snapshot()
    s.fields[0].kind = "select"
    s.fields[0].value = "Wrong"
    s.fields[0].options = ["Wrong", "Other"]
    assert not rule_review(s, demo_context())[0].auto_fix


def test_model_unknown_fact_is_not_an_automatic_repair():
    s = snapshot()
    r = review(s, demo_context(), {}, lambda *_: model_result(s, decision="needs_user", issues=[
        {"field_id": "first", "reason": "必须由用户确认。", "evidence_ids": []}]))
    assert r["decision"] == "needs_user"
    assert not r["issues"][0]["auto_fix"]


@pytest.fixture
def client(tmp_path):
    def fake(s, ctx, cfg):
        return review(s, ctx, cfg, lambda *_: model_result(s))
    app = FastAPI()
    app.include_router(build_router({}, reviewer=fake, run_dir=tmp_path))
    with TestClient(app) as c:
        html = c.get("/agent-lab").text
        token = re.search(r'name="review-token" content="([^"]+)"', html)[1]
        c.headers.update({"Origin": "http://testserver", "X-JobScan-Review": token})
        yield c


def body(**changes):
    data = dict(job_id="demo-001", demo=True, snapshot=snapshot().model_dump())
    data.update(changes)
    return data


def test_origin_and_token_required(client):
    assert client.post("/api/agent-lab/review", json=body(), headers={"Origin": "https://hostile.test"}).status_code == 403
    assert client.post("/api/agent-lab/review", json=body(), headers={"X-JobScan-Review": "wrong"}).status_code == 403


def test_two_round_repair_confirm_and_manual_edit(client, tmp_path):
    s = snapshot()
    s.fields[0].value = "Wrong"
    first = client.post("/api/agent-lab/review", json=body(snapshot=s.model_dump())).json()
    assert first["status"] == "needs_fix"
    assert "context" not in first and "owner" not in first and "snapshot" not in first
    second = client.post("/api/agent-lab/review", json=body(run_id=first["id"])).json()
    assert second["status"] == "awaiting_confirmation" and second["round"] == 2
    confirmed = client.post("/api/agent-lab/confirm", json=body(run_id=first["id"])).json()
    assert confirmed["status"] == "review_passed"
    altered = client.post("/api/agent-lab/confirm", json=body(run_id=first["id"], snapshot=s.model_dump()))
    assert altered.status_code == 409
    saved = json.loads((tmp_path / (first["id"] + ".json")).read_text())
    assert saved["status"] == "stale" and "owner" not in saved


def test_round_limit_document_binding_and_identity(client):
    first = client.post("/api/agent-lab/review", json=body()).json()
    changed = snapshot(document_id="other-page")
    assert client.post("/api/agent-lab/review", json=body(run_id=first["id"], snapshot=changed.model_dump())).status_code == 409
    assert client.post("/api/agent-lab/confirm", json=body(run_id=first["id"], apply_profile="other")).status_code == 409
    for _ in range(4):
        assert client.post("/api/agent-lab/review", json=body(run_id=first["id"])).status_code == 200
    assert client.post("/api/agent-lab/review", json=body(run_id=first["id"])).status_code == 409


def test_pairing_is_single_use_and_bound_to_extension_origin(client):
    code = client.post("/api/agent-lab/pair-code").json()["code"]
    assert client.post("/api/agent-lab/pair", json={"code": code}).status_code == 403
    origin = "chrome-extension://abc"
    paired = client.post("/api/agent-lab/pair", json={"code": code}, headers={"Origin": origin})
    token = paired.json()["token"]
    assert client.post("/api/agent-lab/pair", json={"code": code}, headers={"Origin": origin}).status_code == 403
    assert client.post("/api/agent-lab/review", json=body(), headers={"Origin": origin, "X-JobScan-Review": token}).status_code == 200
    assert client.post("/api/agent-lab/review", json=body(), headers={"Origin": "chrome-extension://other", "X-JobScan-Review": token}).status_code == 403


def test_demo_cannot_target_real_jobs(client):
    assert client.post("/api/agent-lab/review", json=body(job_id="real")).status_code == 400
    assert client.post("/api/agent-lab/review", json=body(demo=False)).status_code == 409


def test_run_ownership(client):
    first = client.post("/api/agent-lab/review", json=body()).json()
    html = client.get("/agent-lab").text
    other = re.search(r'name="review-token" content="([^"]+)"', html)[1]
    assert client.post("/api/agent-lab/confirm", json=body(run_id=first["id"]), headers={"X-JobScan-Review": other}).status_code == 404


def passed_run(client, **changes):
    run = client.post("/api/agent-lab/review", json=body(**changes)).json()
    assert client.post("/api/agent-lab/confirm", json=body(**{**changes, "run_id": run["id"]})).status_code == 200
    return run


def permit(client, run, **changes):
    return client.post("/api/agent-lab/submit-permit", json=body(run_id=run["id"], submit_authorized=True, **changes))


def report(client, run, outcome):
    return client.post("/api/agent-lab/submission-result", json=dict(run_id=run["id"], attempt_id=run["attempt_id"], outcome=outcome, evidence="Observed employer response"))


def test_submission_requires_authorization_and_fresh_review(client):
    first = client.post("/api/agent-lab/review", json=body()).json()
    assert permit(client, first).status_code == 409
    run = passed_run(client)
    assert client.post("/api/agent-lab/submit-permit", json=body(run_id=run["id"])).status_code == 409
    changed = snapshot(); changed.fields[0].value = "changed"
    assert permit(client, run, snapshot=changed.model_dump()).status_code == 409


@pytest.mark.parametrize("outcome", ["submitted", "unknown", "blocked"])
def test_terminal_result_cannot_repeat_even_from_second_run(client, outcome):
    run = passed_run(client)
    granted = permit(client, run).json()
    assert granted["status"] == "submitting"
    assert permit(client, run).status_code == 409
    assert report(client, granted, outcome).status_code == 200
    assert report(client, granted, outcome).status_code == 409
    assert permit(client, passed_run(client)).status_code == 409


def test_validation_error_requires_new_review_and_only_one_retry(client):
    run = passed_run(client)
    first = permit(client, run).json()
    assert report(client, first, "validation_error").status_code == 200
    assert permit(client, first).status_code == 409
    second = passed_run(client, run_id=first["id"])
    granted = permit(client, second).json()
    assert report(client, granted, "validation_error").status_code == 200
    assert permit(client, passed_run(client)).status_code == 409


def test_permit_binds_identity_document_and_provider_result(client):
    run = passed_run(client)
    assert permit(client, run, apply_profile="other").status_code == 409
    assert permit(client, run, snapshot=snapshot(document_id="other").model_dump()).status_code == 409
    granted = permit(client, run).json()
    forged = {**granted, "attempt_id":"invented"}
    assert report(client, forged, "submitted").status_code == 409


def test_shared_memory_enforces_mailbox_name_and_changes_version():
    from app.application_memory import memory_pack, validate_identity
    p = dict(first_name="Casey", last_name="Example", preferred_name="Casey", legal_first_name="Alex", email="sample-5fbfd918@example.com")
    validate_identity(p)
    with pytest.raises(ValueError):
        validate_identity({**p,"first_name":"Alex"})
    first = memory_pack(p, {})
    assert first["identity"]["legal_first_name"] == "Alex"
    assert first["version"] != memory_pack(p, {"projects":[{"name":"SLOServe"}]})["version"]
    assert "password" not in json.dumps(first)


def test_config_casey_card_is_explicit_and_not_inherited_from_alex():
    from app.config import load_config
    from app.tailoring import apply_profile_identity
    card = apply_profile_identity(load_config(), "casey")
    assert card["first_name"] == "Casey" and card["email"] == "sample-5fbfd918@example.com"


def one_field(**field):
    return snapshot(fields=[dict(id="q", required=False, **field)])


@pytest.mark.parametrize("label", [
    "I certify that all information in this application is accurate.",
    "I hereby certify that the above is true.",
    "I understand my response regarding work authorization must be accurate.",
    "I confirm that I am a U.S. citizen or permanent resident.",
    "I attest that this application is my own work.",
    "I authorize a background check.",
    "I have read and accept the privacy policy.",
    "I’ve read the applicant notice.",
    "By selecting Yes, I am consenting to the use of AI for evaluating my candidacy.",
    "Do you agree to our Code of Conduct?",
    "Do you accept the Applicant Privacy Notice?",
    "Please acknowledge the following.",
    "Are you willing to sign a non compete?",
    "Are you willing to sign an NDA?",
    "Are you willing to sign a confidentiality agreement?",
    "Are you willing to sign a non-disclosure agreement?",
])
def test_a_statement_to_sign_needs_the_owner(label):
    issues = rule_review(one_field(label=label, kind="radio", value="Yes", options=["Yes", "No"]), demo_context())
    assert [i.field_id for i in issues] == ["q"] and "本人回答" in issues[0].reason


def test_a_ticked_statement_stops_and_an_unticked_box_has_said_nothing():
    statement = "I have read and accept the privacy policy."
    assert rule_review(one_field(label=statement, kind="checkbox", value="Yes"), demo_context())
    assert rule_review(one_field(label=statement, kind="checkbox", value="No"), demo_context()) == []


@pytest.mark.parametrize("label, value", [
    ("Are you authorized to work in the United States?", "Yes"),
    ("Please confirm you are authorized to work in the United States.", "Yes"),
    ("Please confirm you can work from our San Francisco office 3 days a week.", "Yes"),
    ("Will you now or in the future require sponsorship?", "No"),
    ("Are you legally authorized to work in the country where this role is located?", "Yes"),
    ("Why do you want to work here?", "I understand the problem and I agree with your approach."),
])
def test_ordinary_questions_and_her_own_prose_are_not_statements(label, value):
    kind = "textarea" if value not in ("Yes", "No") else "radio"
    options = ["Yes", "No"] if kind == "radio" else []
    assert rule_review(one_field(label=label, kind=kind, value=value, options=options), demo_context()) == []


def test_the_review_reads_the_statement_under_a_bare_title():
    statement = "I certify that all information is accurate."
    plain = dict(label="Acknowledgement", kind="radio", value="Yes", options=["Yes", "No"])
    assert rule_review(one_field(**plain), demo_context()) == []
    assert rule_review(one_field(**plain, description=statement), demo_context())
    # The model reads it too, because it is part of the snapshot.
    s = one_field(**plain, description="Anything")
    seen = []
    review(s, demo_context(), {}, lambda _cfg, payload: seen.append(payload) or model_result(s))
    assert seen[0]["snapshot"]["fields"][0]["description"] == "Anything"
    with pytest.raises(ValueError):
        one_field(**plain, description="x" * 3001)


def test_a_may_you_work_question_under_a_bare_title_still_stops_a_leading_no():
    ctx = {**demo_context(), "facts": [*demo_context()["facts"], {"id": "work_authorized", "labels": [], "value": "Yes"}]}
    s = one_field(label="Question one", kind="radio", value="No", options=["Yes", "No"], description="Are you legally authorized to work in the US?")
    assert any(i.evidence_ids == ["work_authorized"] for i in rule_review(s, ctx))


def test_facts_stay_matched_on_the_label_alone():
    s = one_field(label="Email", kind="email", value="avery@example.test", description="First name")
    assert rule_review(s, demo_context()) == []


SPONSOR = "Will you now or in the future require sponsorship?"


@pytest.mark.parametrize("value, flagged", [
    ("Yes, I will require sponsorship", True),
    ("No, I do not require sponsorship", False),
])
def test_a_yes_no_fact_on_sentence_options_reads_the_first_word(value, flagged):
    s = one_field(label=SPONSOR, kind="radio", value=value, options=["Yes, I will require sponsorship", "No, I do not require sponsorship"])
    issues = rule_review(s, demo_context())
    assert bool(issues) is flagged
    if flagged:
        assert issues[0].reason == "与已确认事实不一致。" and not issues[0].auto_fix


def test_a_sentence_with_no_first_word_goes_to_the_model_and_a_blank_is_only_blank():
    options = ["I am authorized to work for any employer and do not require sponsorship", "I require sponsorship"]
    assert rule_review(one_field(label=SPONSOR, kind="radio", value=options[0], options=options), demo_context()) == []
    assert rule_review(one_field(label=SPONSOR, kind="radio", value="", options=options), demo_context()) == []


CONSENT = ["Yes - I consent to receiving text messages", "No - I do not consent"]


def phone_context():
    return {**demo_context(), "facts": [*demo_context()["facts"], {"id": "phone", "labels": ["Phone Number"], "value": "(555) 010-0000"}]}


def test_a_consent_radio_labelled_phone_is_not_a_wrong_phone_number_but_ticking_it_stops():
    blank = one_field(label="Phone Number", kind="radio", value="", options=CONSENT)
    assert rule_review(blank, phone_context()) == []
    ticked = rule_review(one_field(label="Phone Number", kind="radio", value=CONSENT[0], options=CONSENT), phone_context())
    assert len(ticked) == 1 and "本人回答" in ticked[0].reason and ticked[0].replacement is None
    assert rule_review(one_field(label="Phone Number", kind="radio", value=CONSENT[1], options=CONSENT), phone_context()) == []


def test_a_statement_in_the_chosen_option_stops_on_any_label():
    options = ["Sure thing", "I agree to binding arbitration"]
    assert rule_review(one_field(label="Preferences", kind="radio", value=options[1], options=options), demo_context())
    # Beside it, any other answer that is not a decline may have accepted the same question.
    assert rule_review(one_field(label="Preferences", kind="radio", value=options[0], options=options), demo_context())


def test_opting_out_of_recording_signs_nothing_and_consenting_to_it_stops():
    options = ["Yes, I consent to be recorded", "Opt out of recording"]
    assert rule_review(one_field(label="Interview Recording Consent", kind="radio", value=options[1], options=options), demo_context()) == []
    assert rule_review(one_field(label="Interview Recording Consent", kind="radio", value=options[0], options=options), demo_context())
    # The same holds when the question itself is worded as a consent.
    asked = "Do you consent to the recording of your interviews?"
    assert rule_review(one_field(label=asked, kind="radio", value=options[1], options=options), demo_context()) == []
    assert rule_review(one_field(label=asked, kind="radio", value=options[0], options=options), demo_context())


def test_the_prompt_limits_other_cities_to_the_roles_own_list():
    from app.agent_review import SYSTEM
    flat = re.sub(r"\s+", " ", SYSTEM)
    assert "Other cities listed for the role are not a gate" not in flat
    assert "That covers only the role's list" in flat and "outside the Bay Area, with no Bay Area choice offered, is HARD" in flat
    # The sentence that fixed the Latent false stop stays.
    assert "on-site work there, even five days a week, is a SOFT arrangement" in flat


def test_the_review_route_accepts_a_description(client):
    s = one_field(label="Acknowledgement", kind="radio", value="", options=["Yes", "No"], description="Anything")
    assert client.post("/api/agent-lab/review", json=body(snapshot=s.model_dump())).status_code == 200


@pytest.mark.parametrize("label", [
    "By submitting, you agree to our hybrid work policy and our code of conduct.",
    "You acknowledge that this remote position is at-will employment.",
    "Applicants must accept the on-call policy and the Employee Handbook.",
    "Please confirm you agree to our code of conduct and will work on-site.",
    "Please confirm you are authorized to work in the US and that you agree to the code of conduct.",
    "Please confirm you are authorized to work in the U.S. and that you have never been convicted of a felony.",
    "Will you require an export license to access export-controlled technology?",
    "Are you subject to export-control restrictions?",
    "Are you permitted to work with export-controlled technology?",
    "Are you willing to travel to a foreign country?",
    "Are you able to travel out of the country for work?",
    "Are you willing to travel by plane to customer sites across the world?",
])
def test_rule_review_stops_what_filler_must_not_sign_or_claim(label):
    s = snapshot(fields=[dict(id="q", label=label, kind="radio", value="Yes", options=["Yes", "No"])])
    assert [i.field_id for i in rule_review(s, demo_context())] == ["q"]


@pytest.mark.parametrize("label", [
    "Please confirm you are authorized to work in the United States.",
    "Please confirm you can work from our San Francisco office 3 days a week.",
    "Are you a citizen or resident of an export-controlled country?",
    "Are you willing to travel across the US?",
    "Are you willing to travel?",
])
def test_rule_review_leaves_what_has_an_answer_on_file(label):
    s = snapshot(fields=[dict(id="q", label=label, kind="radio", value="Yes", options=["Yes", "No"])])
    assert rule_review(s, demo_context()) == []


def test_a_track_record_is_not_a_recording_consent():
    from app.agent_review import RECORDING
    for label in ("Do you have a track record of conducting technical interviews?", "Do you have a strong record of interviews?"):
        assert not RECORDING.search(label), label
    for label in ("May we record your interview?", "Interview Recording Consent", "Do you consent to this interview being recorded?",
                  "We use Metaview, an AI notetaking tool, to streamline interviews by recording and summarizing discussions"):
        assert RECORDING.search(label), label
    # The exemption no longer silences a statement beside the words.
    s = snapshot(fields=[dict(id="q", label="I agree to a track record of interviews.", kind="radio", value="Yes", options=["Yes", "No"])])
    assert [i.field_id for i in rule_review(s, demo_context())] == ["q"]


def test_an_option_naming_a_status_is_raised_though_it_starts_with_the_right_word():
    context = {**demo_context(), "facts": [{"id": "work_authorized", "labels": ["Are you authorized to work in the United States?"], "value": "Yes"}]}
    label = "Are you authorized to work in the United States?"
    bad = snapshot(fields=[dict(id="q", label=label, kind="radio", value="Yes - I have a green card", options=["Yes - I have a green card", "No"])])
    assert [i.field_id for i in rule_review(bad, context)] == ["q"]
    for value in ("Yes", "Yes, I am authorized", "Yes (such as OPT, CPT)"):
        fine = snapshot(fields=[dict(id="q", label=label, kind="radio", value=value, options=[value, "No"])])
        assert rule_review(fine, context) == [], value


def test_a_field_left_off_the_list_gets_one_more_try_and_the_model_is_told_which_ids():
    s = snapshot(fields=[dict(id="first", label="First name", kind="text", value="Avery", required=True),
                         dict(id="more", label="Anything to add?", kind="textarea", value="Nothing.")])
    support = {"first": ["name-first"], "more": ["experience"]}
    payloads, replies = [], [model_result(s, checked_field_ids=["first"], support=support), model_result(s, support=support)]

    def model(_cfg, payload):
        payloads.append(payload)
        return replies.pop(0)
    result = review(s, demo_context(), {}, model)
    assert result["model_status"] == "completed" and len(payloads) == 2
    assert payloads[0]["field_ids_to_check"] == ["first", "more"]


def test_a_passing_reply_may_leave_the_empty_issues_out_and_a_failing_one_may_not():
    s = snapshot()
    text, model = model_result(s)
    reply = json.loads(text)
    del reply["issues"]
    assert review(s, demo_context(), {}, lambda *_: (json.dumps(reply), model))["decision"] == "pass"
    reply["decision"] = "needs_user"
    r = review(s, demo_context(), {}, lambda *_: (json.dumps(reply), model))
    assert r["decision"] == "needs_user" and r["model_status"] == "error"
