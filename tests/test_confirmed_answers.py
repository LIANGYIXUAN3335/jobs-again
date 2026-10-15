"""Answers the owner writes on the board: what they match, who may write them, and how a job runs again."""
import json
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent_lab, agent_runner, ai, application_identity, confirmed_answers, tailoring
from app.agent_review import FormField, Snapshot, confirmed_answer, demo_context, digest, review, rule_review

PREFIX = "/api/agent-lab/"
RUNNER = PREFIX + "runner/"
EXTENSION = "chrome-extension://abc"
OFFSITE = "Are there any circumstances preventing you from traveling internationally to a week-long team offsite?"
SAME = [
    ("Are you able to travel?", "are you able to travel?"),
    ("Are you able to travel?", "ARE YOU ABLE TO TRAVEL"),
    ("Are you able to travel?", "Are you able to travel? *"),
    ("Current company", "Current company (required)"),
]
DIFFERENT = [
    ("Are you able to travel?", "Are you able to travel internationally?"),
    ("Are you able to travel?", "Why are you able to travel?"),
    ("Current company", "Current company size"),
    ("Why this role?", "Why this company?"),
    # Words after the title are a statement she never saw, whatever the title ends in.
    ("Are you able to travel?", "Are you able to travel? We visit customers twice a year."),
    ("Acknowledgement", "Acknowledgement I agree to binding arbitration."),
    ("Acknowledgement", "Acknowledgement: I agree to binding arbitration."),
    ("Please confirm:", "Please confirm: I agree to binding arbitration."),
    # One word apart, and it is the opposite statement.
    ("Do you have no restrictions?", "Do you have restrictions?"),
    ("I have relatives working here.", "I have no relatives working here."),
    ("I have no relatives working here.", "I have relatives working here."),
]


@pytest.mark.parametrize("stored, seen", SAME)
def test_the_same_question_matches(stored, seen):
    assert re.search(confirmed_answers.question_topic(stored), seen, re.I)


@pytest.mark.parametrize("stored, seen", DIFFERENT)
def test_a_different_question_does_not(stored, seen):
    assert not re.search(confirmed_answers.question_topic(stored), seen, re.I)


def test_a_very_long_question_matches_whole_or_as_the_extension_cuts_it():
    long = " ".join(f"word{i}" for i in range(60)) + "?"
    topic = confirmed_answers.question_topic(long)
    assert re.search(topic, long, re.I) and re.search(topic, long[:300], re.I), "the extension reads 300 characters"
    assert not re.search(topic, " ".join(f"word{i}" for i in range(20)), re.I)
    assert not re.search(topic, long[:290], re.I), "only a text the extension really cut may end early"
    other = long[:300] + " and you waive your rights."
    assert not re.search(topic, other, re.I)
    assert re.search(topic, other[:300], re.I), "the extension cannot tell, so the Reviewer has to"
    assert not re.search(confirmed_answers.whole_text_topic(topic), other[:300], re.I)


def test_a_first_word_past_the_limit_leaves_no_cut_branch_that_matches_anything():
    topic = confirmed_answers.question_topic("x" * 400)
    assert not re.search(topic, "y" * 300, re.I) and re.search(topic, "x" * 400, re.I)


def test_the_extension_reads_the_pattern_the_same_way():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    cases = [[confirmed_answers.question_topic(stored), seen.lower(), True] for stored, seen in SAME]
    cases += [[confirmed_answers.question_topic(stored), seen.lower(), False] for stored, seen in DIFFERENT]
    # What a label cut as clean() cuts it reads: past that point the Reviewer tells two statements apart.
    statement = "I acknowledge the notice and have no objection. " + " ".join(f"clause{i}" for i in range(50))
    block = f"Acknowledgement * {statement}".lower()[:300]
    topic = confirmed_answers.question_topic("Acknowledgement", statement)
    cases += [[topic, block, True], [topic, block.replace(" no ", " "), False], [topic, f"acknowledgement * {statement}".lower(), True],
              [topic, f"acknowledgement * {statement} and more".lower(), False], [confirmed_answers.question_topic("Acknowledgement"), block, False],
              [confirmed_answers.question_topic("Acknowledgement"), "acknowledgement *", True],
              [confirmed_answers.question_topic("Acknowledgement", "I agree to arbitration."), "acknowledgement: i agree to arbitration", True],
              [confirmed_answers.question_topic("Acknowledgement", "I agree to arbitration."), "acknowledgement: i agree to terms", False]]
    script = "const cases=JSON.parse(process.argv[1]);for(const [t,s,w] of cases)if(new RegExp(t,'i').test(s)!==w)throw Error(t+' / '+s);"
    subprocess.run([node, "-e", script, json.dumps(cases)], check=True, timeout=20)


def test_remember_change_forget_and_file_permissions():
    cfg = {}
    first = confirmed_answers.remember(cfg, "Are you able to travel?", "Only within the US.")
    again = confirmed_answers.remember(cfg, "ARE YOU ABLE TO TRAVEL", "Yes")
    assert again["id"] == first["id"] and again["yesno"] == "yes" and len(confirmed_answers.all_answers(cfg)) == 1
    assert confirmed_answers.change(cfg, first["id"], "No")["yesno"] == "no"
    path = confirmed_answers.path(cfg)
    assert path.stat().st_mode & 0o077 == 0
    confirmed_answers.forget(cfg, first["id"])
    assert confirmed_answers.all_answers(cfg) == []
    for action in (lambda: confirmed_answers.change(cfg, "gone", "x"), lambda: confirmed_answers.forget(cfg, "gone")):
        with pytest.raises(KeyError):
            action()
    with pytest.raises(ValueError):
        confirmed_answers.remember(cfg, "?!", "x")
    with pytest.raises(ValueError):
        confirmed_answers.remember(cfg, "Real question", "   ")


def test_a_broken_file_means_no_answers_and_the_question_stops_for_the_owner():
    path = confirmed_answers.path({})
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert confirmed_answers.all_answers({}) == []


def test_answers_stay_in_their_own_workspace():
    other = {"workspace": {"name": "Taylor"}}
    assert confirmed_answers.path({}) != confirmed_answers.path(other)
    confirmed_answers.remember({}, "Can you travel?", "No")
    assert confirmed_answers.all_answers(other) == []


def test_an_answer_naming_the_company_stays_with_that_company():
    cfg = {}
    confirmed_answers.remember(cfg, "Why do you want this job?", "I have used Notion for years.", company="Notion")
    confirmed_answers.remember(cfg, "Are you able to commute?", "Yes", company="Notion")
    assert [a["question"] for a in confirmed_answers.for_company(cfg, "notion")] == ["Why do you want this job?", "Are you able to commute?"]
    assert [a["question"] for a in confirmed_answers.for_company(cfg, "Close")] == ["Are you able to commute?"]
    assert [a["question"] for a in confirmed_answers.for_company(cfg)] == ["Are you able to commute?"]


def test_the_answer_to_this_exact_question_beats_a_topic_wide_one():
    cfg = {}
    passport = "Confirming that you have a valid passport? (necessary for international travel)"
    confirmed_answers.remember(cfg, "国际出差", "I'm not able to travel internationally right now.", topic="international travel")
    item = confirmed_answers.remember(cfg, passport, "Yes")
    usable = {"confirmed_answers": confirmed_answers.for_company(cfg)}
    assert confirmed_answer(passport, usable)["id"] == item["id"]
    assert "not able" in confirmed_answer("Is international travel a problem?", usable)["answer"]


def test_a_sentence_cannot_go_into_a_choice_so_it_stops_and_says_why():
    item = confirmed_answers.remember({}, "国际出差", "I'm not able to travel internationally right now.", topic="international travel")
    issue = rule_review(_form("Passport? (necessary for international travel)", "", "radio", ["Yes", "No"]), _context(item))[0]
    assert not issue.auto_fix and "本人选" in issue.reason


def test_models_see_the_answers_as_confirmed_facts(monkeypatch):
    cfg = {}
    monkeypatch.setattr(tailoring, "load_base", lambda _: {})
    monkeypatch.setattr(ai, "_role_identity", lambda *_: ("Casey Example", {}))
    confirmed_answers.remember(cfg, "Are you able to travel abroad?", "No", fact="Cannot travel abroad right now.")
    confirmed_answers.remember(cfg, "Preferred language?", "Go")
    facts = ai._facts(cfg, {"company": "Example", "job_id": "x"})
    assert "Cannot travel abroad right now." in facts
    assert "Question: Preferred language? Answer: Go" in facts


def _form(label, value, kind="text", options=()):
    return Snapshot(url="https://jobs.ashbyhq.com/x/y", document_id="d", coverage="complete",
                    fields=[FormField(id="q", label=label, kind=kind, value=value, required=True, options=list(options))])


def _context(*answers):
    return {"facts": [{"id": a["id"], "labels": [], "value": a["answer"]} for a in answers], "confirmed_answers": list(answers)}


@pytest.mark.parametrize("value, flagged", [("No", False), ("no", False), ("Not sure", True), ("Yes", True), ("", True)])
def test_a_one_word_answer_must_be_the_whole_value(value, flagged):
    item = confirmed_answers.remember({}, "Are you able to relocate?", "No")
    assert bool(rule_review(_form("Are you able to relocate?", value), _context(item))) is flagged


def test_a_remembered_choice_can_be_fixed_only_when_the_option_exists():
    item = confirmed_answers.remember({}, "Are you able to relocate?", "No")
    fix = rule_review(_form("Are you able to relocate?", "", "radio", ["Yes", "No"]), _context(item))[0]
    assert fix.auto_fix and fix.replacement == "No"
    assert not rule_review(_form("Are you able to relocate?", "", "radio", ["Maybe"]), _context(item))[0].auto_fix


@pytest.fixture
def board(tmp_path, monkeypatch):
    candidates = [{"job_id": f"job-{i}", "company": "Example", "title": f"Engineer {i}",
                   "url": f"https://jobs.ashbyhq.com/example/job-{i}/application",
                   "apply_profile": "casey", "resume_ready": True, "status": "new"} for i in (1, 2)]
    monkeypatch.setattr(agent_lab, "runner_candidates", lambda _: candidates)
    monkeypatch.setattr(application_identity, "prepare_casey_claim", lambda _cfg, job: job)
    # Nothing here reads a database. A test about what the board says about a stopped job sets its own.
    monkeypatch.setattr(agent_lab, "stopped_job_facts", lambda _cfg, _jobs: {})

    def context(job_id, profile, _url):
        return {**demo_context(), "job_id": job_id, "apply_profile": profile, "application_status": "new",
                "submission_blocker": "", "confirmed_answers": confirmed_answers.for_company({}, "Example")}

    def reviewer(snapshot, ctx, cfg):
        def model(*_):
            return json.dumps({"decision": "pass", "checked_field_ids": [f.id for f in snapshot.fields],
                               "support": {f.id: ["name-first"] for f in snapshot.fields}, "issues": [], "summary": "已检查。"}), "fixture"
        return review(snapshot, ctx, cfg, model)

    app = FastAPI()
    app.include_router(agent_lab.build_router({}, context_loader=context, reviewer=reviewer, run_dir=tmp_path))
    with TestClient(app) as client:
        session = re.search(r'name="review-token" content="([^"]+)"', client.get("/agent-lab").text)[1]
        client.headers.update({"Origin": "http://testserver", "X-JobScan-Review": session})
        yield SimpleNamespace(client=client, directory=tmp_path, candidates=candidates, session=session)


def _claim(client, worker="worker-1"):
    assert client.post(RUNNER + "start", json={"apply_profile": "casey", "max_jobs": 1}).status_code == 200
    claim = client.post(RUNNER + "claim", json={"worker_id": worker}).json()
    assert claim["status"] == "claimed"
    return claim, {"lease_id": claim["lease_id"], "token": claim["token"], "worker_id": worker}


def _stop_on_a_blank_question(board, label=OFFSITE, options=()):
    claim, lease = _claim(board.client)
    body = {"job_id": claim["job"]["job_id"], "apply_profile": "casey", "demo": False, "runner_lease": lease,
            "snapshot": {"url": claim["job"]["url"], "document_id": "d", "coverage": "complete",
                         "fields": [{"id": "q", "label": label, "kind": "radio" if options else "textarea", "value": "",
                                     "required": True, "options": list(options)}]}}
    assert board.client.post(PREFIX + "review", json=body).json()["status"] == "needs_user"
    finish = board.client.post(RUNNER + "finish", json={**lease, "outcome": "needs_user", "evidence": "必答题没有答案。"})
    assert finish.status_code == 200
    return claim, lease


def test_a_stuck_question_reaches_the_board_and_survives_a_restart(board):
    claim, _ = _stop_on_a_blank_question(board)
    assert "token" not in board.directory.joinpath("stuck_questions.json").read_text()
    view = board.client.get(PREFIX + "answers").json()
    job = view["paused"][0]
    assert job["job_id"] == claim["job"]["job_id"] and job["company"] == "Example"
    assert job["questions"][0]["label"] == OFFSITE and job["questions"][0]["answer"] == ""
    assert "必填项未填写" in job["questions"][0]["reason"]
    app = FastAPI()
    app.include_router(agent_lab.build_router({}, context_loader=lambda *_: {}, run_dir=board.directory))
    with TestClient(app) as fresh:
        session = re.search(r'name="review-token" content="([^"]+)"', fresh.get("/agent-lab").text)[1]
        fresh.headers.update({"Origin": "http://testserver", "X-JobScan-Review": session})
        assert fresh.get(PREFIX + "answers").json()["paused"][0]["questions"][0]["label"] == OFFSITE


def test_answering_marks_the_question_and_the_answer_can_be_changed_and_deleted(board):
    claim, _ = _stop_on_a_blank_question(board)
    view = board.client.post(PREFIX + "answers", json={"question": OFFSITE, "answer": "I can't travel abroad.", "job_id": claim["job"]["job_id"]}).json()
    assert view["paused"][0]["questions"][0]["answer"] == "I can't travel abroad."
    item = view["answers"][0]
    assert item["question"] == OFFSITE and set(item) == {"id", "question", "description", "answer", "company"}
    view = board.client.put(PREFIX + "answers/" + item["id"], json={"answer": "Not right now."}).json()
    assert view["answers"][0]["answer"] == "Not right now."
    assert board.client.put(PREFIX + "answers/gone", json={"answer": "x"}).status_code == 404
    assert board.client.post(PREFIX + "answers", json={"question": "?!", "answer": "x"}).status_code == 400
    view = board.client.delete(PREFIX + "answers/" + item["id"]).json()
    assert view["answers"] == [] and view["paused"][0]["questions"][0]["answer"] == ""


def test_only_the_board_can_write_answers_or_ask_for_a_retry(board):
    claim, _ = _stop_on_a_blank_question(board)
    code = board.client.post(PREFIX + "pair-code", json={}).json()["code"]
    extension = {"Origin": EXTENSION}
    token = board.client.post(PREFIX + "pair", json={"code": code}, headers=extension).json()["token"]
    extension["X-JobScan-Review"] = token
    assert board.client.get(RUNNER + "status", headers=extension).status_code == 200
    assert board.client.get(PREFIX + "answers", headers=extension).status_code == 403
    assert board.client.post(PREFIX + "answers", json={"question": "Q?", "answer": "A"}, headers=extension).status_code == 403
    assert board.client.post(RUNNER + "retry", json={"job_id": claim["job"]["job_id"]}, headers=extension).status_code == 403
    assert confirmed_answers.all_answers({}) == []


def test_retry_runs_that_one_job_again_and_the_old_lease_stays_dead(board):
    claim, old = _stop_on_a_blank_question(board)
    job_id = claim["job"]["job_id"]
    assert board.client.post(RUNNER + "claim", json={"worker_id": "worker-1"}).json()["status"] == "paused"
    state = board.client.post(RUNNER + "retry", json={"job_id": job_id}).json()
    assert state["enabled"] is True and state["max_jobs"] == 1 and state["pause_reason"] == ""
    again = board.client.post(RUNNER + "claim", json={"worker_id": "worker-1"}).json()
    assert again["status"] == "claimed" and again["job"]["job_id"] == job_id and again["lease_id"] != old["lease_id"]
    assert board.client.post(RUNNER + "check", json=old).status_code == 404
    new = {"lease_id": again["lease_id"], "token": again["token"], "worker_id": "worker-1"}
    assert board.client.post(RUNNER + "finish", json={**new, "outcome": "submitted", "evidence": "Employer said success."}).status_code == 200
    # One retry is one job, and a finished job can never be retried.
    assert board.client.post(RUNNER + "claim", json={"worker_id": "worker-1"}).json()["status"] == "paused"
    assert board.client.post(RUNNER + "retry", json={"job_id": job_id}).status_code == 409


# "unknown" with no permit on file is a plain stop: nothing was clicked. With one, see the test on ever permitted below.
@pytest.mark.parametrize("outcome, allowed", [("needs_user", True), ("blocked", True), ("error", True),
                                              ("unknown", True), ("submitted", False)])
def test_only_a_job_that_stopped_before_submit_can_be_retried(board, outcome, allowed):
    claim, lease = _claim(board.client)
    assert board.client.post(RUNNER + "finish", json={**lease, "outcome": outcome, "evidence": "结果。"}).status_code == 200
    assert (board.client.post(RUNNER + "retry", json={"job_id": claim["job"]["job_id"]}).status_code == 200) is allowed


def test_a_lease_that_expired_before_any_click_can_be_retried(board, monkeypatch):
    claim, _ = _claim(board.client)
    real = agent_runner.time.time
    monkeypatch.setattr(agent_runner.time, "time", lambda: claim["expires_at"] + 1)
    state = board.client.get(RUNNER + "status").json()
    monkeypatch.setattr(agent_runner.time, "time", real)
    record = next(j for j in state["jobs"] if j["job"]["job_id"] == claim["job"]["job_id"])
    assert record["status"] == "blocked" and "从没点过提交" in record["evidence"] and state["enabled"] is False
    assert board.client.post(RUNNER + "retry", json={"job_id": claim["job"]["job_id"]}).status_code == 200


def test_a_lease_that_expired_after_a_click_stays_unknown(board, monkeypatch):
    claim, _ = _claim(board.client)
    receipt = board.directory / ("submission-" + digest([claim["job"]["job_id"], "real"]) + ".json")
    receipt.write_text(json.dumps({"attempts": 1, "outcome": "submitting"}))
    real = agent_runner.time.time
    monkeypatch.setattr(agent_runner.time, "time", lambda: claim["expires_at"] + 1)
    state = board.client.get(RUNNER + "status").json()
    monkeypatch.setattr(agent_runner.time, "time", real)
    assert next(j for j in state["jobs"] if j["job"]["job_id"] == claim["job"]["job_id"])["status"] == "unknown"
    assert board.client.post(RUNNER + "retry", json={"job_id": claim["job"]["job_id"]}).status_code == 409


def test_the_board_links_to_this_folders_runner_page(board):
    url = board.client.get(RUNNER + "status").json()["worker"]["runner_url"]
    assert re.fullmatch(r"chrome-extension://[a-p]{32}/runner\.html", url)


def test_a_batch_moves_on_past_a_stop_and_ends_at_its_limit(board):
    board.candidates.append({**board.candidates[0], "job_id": "job-3", "title": "Engineer 3"})
    for index, job in enumerate(board.candidates):
        job["company"] = f"Example {index}"
    assert board.client.post(RUNNER + "start", json={"apply_profile": "casey", "max_jobs": 3}).status_code == 200
    seen = []
    for outcome in ("needs_user", "blocked", "submitted"):
        claim = board.client.post(RUNNER + "claim", json={"worker_id": "w"}).json()
        assert claim["status"] == "claimed"
        seen.append(claim["job"]["job_id"])
        lease = {"lease_id": claim["lease_id"], "token": claim["token"], "worker_id": "w"}
        state = board.client.post(RUNNER + "finish", json={**lease, "outcome": outcome, "evidence": f"结果 {outcome}"}).json()
    assert seen == ["job-1", "job-2", "job-3"]
    assert state["enabled"] is False and state["pause_reason"] == "limit_reached"
    view = board.client.get(PREFIX + "answers").json()
    assert [(row["job_id"], row["status"]) for row in view["batch"]] == [("job-1", "needs_user"), ("job-2", "blocked"), ("job-3", "submitted")]
    assert {p["job_id"] for p in view["paused"]} == {"job-1", "job-2"}


@pytest.mark.parametrize("outcome", ["unknown", "error"])
def test_a_batch_stops_at_once_when_a_result_is_unclear_or_something_broke(board, outcome):
    assert board.client.post(RUNNER + "start", json={"apply_profile": "casey", "max_jobs": 2}).status_code == 200
    claim = board.client.post(RUNNER + "claim", json={"worker_id": "w"}).json()
    lease = {"lease_id": claim["lease_id"], "token": claim["token"], "worker_id": "w"}
    if outcome == "unknown":   # Only a click makes it unclear.
        (board.directory / ("submission-" + digest([claim["job"]["job_id"], "real"]) + ".json")).write_text(json.dumps({"attempts": 1, "outcome": "submitting"}))
    state = board.client.post(RUNNER + "finish", json={**lease, "outcome": outcome, "evidence": "结果。"}).json()
    assert state["enabled"] is False and state["pause_reason"] == outcome
    assert board.client.post(RUNNER + "claim", json={"worker_id": "w"}).json()["status"] == "paused"


@pytest.mark.parametrize("count, status", [(1, 200), (5, 200), (20, 200), (0, 422), (21, 422), (2.5, 422)])
def test_batch_size_limits(board, count, status):
    assert board.client.post(RUNNER + "start", json={"apply_profile": "casey", "max_jobs": count}).status_code == status


def test_a_job_whose_submit_was_ever_permitted_cannot_be_retried(board):
    claim, _ = _stop_on_a_blank_question(board)
    job_id = claim["job"]["job_id"]
    receipt = board.directory / ("submission-" + digest([job_id, "real"]) + ".json")
    receipt.write_text(json.dumps({"attempts": 1, "outcome": "validation_error"}))
    assert board.client.post(RUNNER + "retry", json={"job_id": job_id}).status_code == 409
    view = board.client.get(PREFIX + "answers").json()
    assert view["paused"] == [] and view["batch"][0]["clicked"] is True


def test_retry_refused_while_another_job_runs(board):
    claim, _ = _stop_on_a_blank_question(board)
    _claim(board.client)
    assert board.client.post(RUNNER + "retry", json={"job_id": claim["job"]["job_id"]}).status_code == 409


def test_retry_of_a_job_no_longer_eligible_stops_and_says_so(board):
    claim, _ = _stop_on_a_blank_question(board)
    assert board.client.post(RUNNER + "retry", json={"job_id": claim["job"]["job_id"]}).status_code == 200
    board.candidates[:] = [c for c in board.candidates if c["job_id"] != claim["job"]["job_id"]]
    assert board.client.post(RUNNER + "claim", json={"worker_id": "worker-1"}).json()["status"] == "paused"
    state = board.client.get(RUNNER + "status").json()
    assert state["enabled"] is False and "没有重跑" in state["events"][-1]["text"]


def test_a_normal_batch_never_takes_a_paused_job_back(board):
    first, _ = _stop_on_a_blank_question(board)
    second, _ = _claim(board.client)
    assert second["job"]["job_id"] != first["job"]["job_id"]


def test_the_board_can_read_the_answers_it_needs_only_with_a_session(board):
    board.client.headers.pop("X-JobScan-Review")
    assert board.client.get(PREFIX + "answers").status_code == 403


@pytest.mark.parametrize("value, flagged", [
    ("Yes, no restrictions", True), ("No restrictions", False), ("No, none.", False), ("Not at all", True), ("I have no restrictions", True),
])
def test_a_confirmed_no_is_read_from_the_first_word(value, flagged):
    item = confirmed_answers.remember({}, "Are you able to relocate?", "No")
    assert bool(rule_review(_form("Are you able to relocate?", value), _context(item))) is flagged


def test_a_models_doubt_stands_when_the_value_only_contains_the_confirmed_word():
    item = confirmed_answers.remember({}, "Are you able to relocate?", "No")
    s = _form("Are you able to relocate?", "Yes, no restrictions", "radio", ["Yes, no restrictions", "No"])
    result = review(s, _context(item), {}, lambda *_: (json.dumps({"decision": "pass", "checked_field_ids": ["q"], "support": {"q": [item["id"]]}, "issues": [], "summary": "ok"}), "stub"))
    assert result["decision"] == "needs_fix" and result["issues"][0]["replacement"] == "No"


LANGUAGE = "What is your preferred programming language for interviews?"


def _preference():
    return confirmed_answers.remember({}, LANGUAGE, "Python", soft=True)


def _doubt(field):
    return json.dumps({"decision": "needs_user", "checked_field_ids": [field.id], "support": {},
                       "issues": [{"field_id": field.id, "reason": "不确定", "evidence_ids": []}], "summary": "不确定"}), "stub"


def test_a_preference_is_waived_on_a_choice_control():
    item = _preference()
    s = _form(LANGUAGE, "Go", "radio", ["Go", "Java"])
    assert rule_review(s, _context(item)) == []
    assert review(s, _context(item), {}, lambda *_: _doubt(s.fields[0]))["decision"] == "pass"


@pytest.mark.parametrize("kind", ["text", "textarea"])
def test_a_preference_is_not_waived_in_free_text(kind):
    item = _preference()
    s = _form(LANGUAGE, "COBOL and a long story about it", kind)
    issue = rule_review(s, _context(item))[0]
    assert issue.replacement == "Python" and issue.auto_fix
    # Typed in her own words it is fine, and the model's doubt about those words does not overrule her.
    typed = _form(LANGUAGE, "Python", kind)
    assert review(typed, _context(item), {}, lambda *_: _doubt(typed.fields[0]))["decision"] == "pass"


def test_a_confirmed_number_is_put_into_the_number_box():
    item = confirmed_answers.remember({}, "How many years of experience do you have?", "7")
    fix = rule_review(_form("How many years of experience do you have?", "", "number"), _context(item))[0]
    assert fix.auto_fix and fix.replacement == "7"
    words = confirmed_answers.remember({}, "How long have you coded?", "About seven years")
    assert not rule_review(_form("How long have you coded?", "", "number"), _context(words))[0].auto_fix


def _real_language_topic():
    from app.config import ROOT
    path = ROOT / "data" / "agent_lab" / "confirmed_answers.casey.json"
    if not path.exists():
        pytest.skip("the owner's answers are not on this machine")
    return next(a["topic"] for a in json.loads(path.read_text(encoding="utf-8"))["answers"] if a["id"] == "q-3fd65a4ef969")


@pytest.mark.parametrize("label, matches", [
    ("What is your preferred coding language?", True),
    ("What is your preferred programming language for interviews?", True),
    ("Which programming language do you prefer?", True),
    ("Which coding language would you like to use in the interview?", True),
    ("In the coding interview, which programming language would you like to use?", True),
    ("What are your strongest programming languages and technical areas? Please list specific languages.", False),
    ("What programming languages do you have 5+ years of professional experience in?", False),
    ("Which programming languages have you used?", False),
    ("What are your strongest programming languages? We prefer candidates with Go.", False),
])
def test_the_interview_language_answer_covers_only_a_preference(label, matches):
    assert bool(re.search(_real_language_topic(), label.lower(), re.I)) is matches


@pytest.mark.parametrize("label", [
    "Programming language you prefer to avoid",
    "For our interview, list all programming languages you have 5+ years with.",
    "Before your interview, which programming languages have you used professionally?",
    "Which programming language do you prefer for production backend work?",
    "Coding language you would not like to use for the interview",
    "Interviewers will ask about the programming languages you used in production",
])
def test_the_interview_language_answer_stays_off_questions_about_her_record(label):
    assert not re.search(_real_language_topic(), label.lower(), re.I)
    assert not re.search(_real_language_topic(), label, re.I), "the Reviewer reads the label as written"


def test_the_answers_on_file_read_the_same_for_the_reviewer():
    from app.config import ROOT
    path = ROOT / "data" / "agent_lab" / "confirmed_answers.casey.json"
    if not path.exists():
        pytest.skip("the owner's answers are not on this machine")
    for item in json.loads(path.read_text(encoding="utf-8"))["answers"]:
        assert confirmed_answers.whole_text_topic(item["topic"]) == item["topic"], item["id"]


def test_a_preference_answers_no_question_about_what_she_has_done_whatever_its_topic_reaches():
    wide = {"id": "lang", "topic": "programming.{0,12}language", "answer": "Python", "soft": True}
    context = _context(wide)
    label = "Which programming languages have you used in production?"
    assert confirmed_answer(label, context) is None
    s = _form(label, "Python", "text")
    assert review(s, context, {}, lambda *_: _doubt(s.fields[0]))["decision"] == "needs_user"
    assert confirmed_answer("What is your preferred programming language?", context) is wide
    # An answer she gave to this very question is hers, whatever words it holds.
    exact = {"id": "exact", "topic": "programming.{0,12}language", "answer": "Python"}
    assert confirmed_answer(label, _context(exact)) is exact


ARBITRATION = "I agree to binding arbitration and waive any right to a jury trial."
NOTICE = "I acknowledge the GDPR notice."


def _statement_form(value, description=ARBITRATION, label="Acknowledgement", kind="radio"):
    return Snapshot(url="https://jobs.ashbyhq.com/x/y", document_id="d", coverage="complete",
                    fields=[FormField(id="q", label=label, kind=kind, value=value, required=True, options=["Yes", "No"], description=description)])


@pytest.mark.parametrize("label", ["Acknowledgement", "Acknowledgement:", "Acknowledgement."])
def test_an_answer_to_a_bare_title_signs_nothing_under_that_title_elsewhere(label):
    item = confirmed_answers.remember({}, label, "Yes")
    context = _context(item)
    assert confirmed_answer(label, context, ARBITRATION) is None
    blank = rule_review(_statement_form("", label=label), context)
    assert not any(issue.auto_fix for issue in blank) and blank
    filled = _statement_form("Yes", label=label)
    assert rule_review(filled, context), "the Yes field must not skip the check for a signed statement"
    assert review(filled, context, {}, lambda *_: _doubt(filled.fields[0]))["decision"] == "needs_user", "the model's objection stands"
    # The same answer still serves the same bare title.
    assert confirmed_answer(label, context)["id"] == item["id"]


def test_an_answer_to_a_statement_covers_that_statement_and_no_other():
    item = confirmed_answers.remember({}, "Acknowledgement", "Yes", description=NOTICE)
    context = _context(item)
    assert confirmed_answer("Acknowledgement", context, NOTICE)["id"] == item["id"]
    assert confirmed_answer("  acknowledgement:", context, "I  acknowledge the\nGDPR notice")["id"] == item["id"], "spacing, case and punctuation do not count"
    assert confirmed_answer("Acknowledgement", context) is None
    assert confirmed_answer("Acknowledgement", context, ARBITRATION) is None
    assert confirmed_answer("Other title", context, NOTICE) is None
    filled = _statement_form("Yes", description=NOTICE)
    assert rule_review(filled, context) == []
    assert review(filled, context, {}, lambda *_: _doubt(filled.fields[0]))["decision"] == "pass", "her own answer overrules the model on this statement"
    fix = rule_review(_statement_form("", description=NOTICE), context)[0]
    assert fix.auto_fix and fix.replacement == "Yes"


def test_the_words_yes_and_no_count_for_everyone_who_reads_the_answer():
    item = confirmed_answers.remember({}, "Do you have no restrictions?", "Yes")
    assert not re.search(item["topic"], "Do you have restrictions?", re.I)
    assert confirmed_answer("Do you have restrictions?", _context(item)) is None
    assert confirmed_answer("Do you have no restrictions?", _context(item))["id"] == item["id"]


def test_an_answer_saved_when_yes_and_no_were_optional_no_longer_reaches_its_opposite():
    cfg = {}
    item = confirmed_answers.remember(cfg, "Do you have no restrictions?", "Yes")
    path = confirmed_answers.path(cfg)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["answers"][0]["topic"] = r"^\W*do\W*you\W*have\W*(?:no)?\W*restrictions\W*(?:required\W*)?$"
    path.write_text(json.dumps(stored), encoding="utf-8")
    loaded = confirmed_answers.all_answers(cfg)[0]
    assert not re.search(loaded["topic"], "Do you have restrictions?", re.I)
    assert re.search(loaded["topic"], "Do you have no restrictions?", re.I)
    # Answering it again finds the same entry.
    assert confirmed_answers.remember(cfg, "Do you have no restrictions?", "No")["id"] == item["id"]
    assert len(confirmed_answers.all_answers(cfg)) == 1


def _no_model(*_):
    raise AssertionError("no model's say can lift this stop, so none is asked")


OPENING = " ".join(f"clause{i}" for i in range(60))


@pytest.mark.parametrize("stored, seen", [
    ("I have relatives working here.", "I have no relatives working here."),
    ("I have no relatives working here.", "I have relatives working here."),
    (OPENING + " Records stay in California.", OPENING + " Records go to our partners in any country."),
])
def test_a_statement_that_only_reads_like_her_answer_stops_with_nothing_to_fix(stored, seen):
    item = confirmed_answers.remember({}, "Family notice", "Yes", description=stored)
    context = _context(item)
    filled = _statement_form("Yes", label="Family notice", description=seen)
    issues = rule_review(filled, context)
    assert [issue.field_id for issue in issues] == ["q"] and issues[0].auto_fix is False and issues[0].evidence_ids == [item["id"]]
    assert review(filled, context, {}, _no_model)["decision"] == "needs_user"
    # Her own statement still goes through, and a question nothing like it is the model's to read.
    assert rule_review(_statement_form("Yes", label="Family notice", description=stored), context) == []
    assert rule_review(_statement_form("Yes", label="Family notice", description="Something else altogether."), context) == []


def test_a_near_miss_only_matters_once_something_fills_the_field():
    item = confirmed_answers.remember({}, "Family notice", "Yes", description="I have relatives working here.")
    blank = rule_review(_statement_form("", label="Family notice", description="I have no relatives working here."), _context(item))
    assert [issue.reason for issue in blank] == ["必填项未填写；没有匹配的确认事实。"]


def test_a_pattern_written_by_hand_has_no_near_miss():
    item = confirmed_answers.remember({}, "国际出差", "No", topic="travel\\w*\\s+internationally")
    assert not confirmed_answers.reads_alike(item, "国际出差")


@pytest.mark.parametrize("title, options, expected", [
    ("GDPR Acknowledgement", ["I Acknowledge/Accept this Statement"], True), ("Arbitration Acknowledgment:", [], True), ("Consent Notice", [], True), ("Family notice", [], True),
    ("Acknowledgement", [], True), ("Please confirm:", [], True), ("Privacy Policy", [], True), ("Terms", [], True), ("Legal", ["I accept the statement above"], True),
    ("Do you consent?", [], False), ("I consent", [], False), ("I acknowledge by submitting an application for this role that I accept and agree.", ["I acknowledge"], False),
    ("Open to relocation if necessary:", [], False), ("Work Authorization", ["Can work for any employer"], False), ("U.S. Work Authorization Status", [], False), ("Why this role?", [], False),
])
def test_what_counts_as_a_title_naming_a_statement_written_elsewhere(title, options, expected):
    assert confirmed_answers.statement_title(title, options) is expected


def test_a_yes_to_a_bare_statement_title_stays_with_that_company():
    item = confirmed_answers.remember({}, "GDPR Acknowledgement", "Yes", company="Illumio")
    assert item["company"] == "Illumio"
    assert confirmed_answers.for_company({}, "Other") == []
    assert [a["id"] for a in confirmed_answers.for_company({}, "illumio")] == [item["id"]]
    # With the statement beside it the answer says what it covers, and any company may use it.
    signed = confirmed_answers.remember({}, "GDPR Acknowledgement", "Yes", description="I acknowledge the notice.", company="Illumio")
    assert signed["company"] == ""
    # A plain question is not one of these.
    assert confirmed_answers.remember({}, "Are you able to relocate?", "No", company="Illumio")["company"] == ""


def test_past_what_the_extension_reads_the_reviewer_still_tells_two_statements_apart():
    opening = " ".join(f"clause{i}" for i in range(60))
    item = confirmed_answers.remember({}, "Acknowledgement", "Yes", description=opening + " I keep my rights.")
    assert confirmed_answer("Acknowledgement", _context(item), opening + " I keep my rights.")["id"] == item["id"]
    assert confirmed_answer("Acknowledgement", _context(item), opening + " I waive my rights.") is None


def test_two_statements_under_one_title_keep_two_answers():
    first = confirmed_answers.remember({}, "Acknowledgement", "Yes", description=NOTICE)
    second = confirmed_answers.remember({}, "Acknowledgement", "No", description=ARBITRATION)
    assert first["id"] != second["id"] and len(confirmed_answers.all_answers({})) == 2
    usable = {"confirmed_answers": confirmed_answers.for_company({})}
    assert confirmed_answer("Acknowledgement", usable, ARBITRATION)["answer"] == "No"


def test_the_exact_statement_still_goes_before_a_topic_wide_answer():
    wide = confirmed_answers.remember({}, "国际出差", "I can't.", topic="international travel")
    exact = confirmed_answers.remember({}, "Acknowledgement", "Yes", description="I accept international travel.")
    assert [a["id"] for a in confirmed_answers.for_company({})] == [exact["id"], wide["id"]]


def test_a_fact_says_which_statement_the_answer_covers():
    item = confirmed_answers.remember({}, "Acknowledgement", "Yes", description=NOTICE)
    text = confirmed_answers.fact_text(item)
    assert NOTICE in text and "only this exact statement" in text
    assert confirmed_answers.fact_text(confirmed_answers.remember({}, "Q?", "A")) == "Question: Q? Answer: A"


def _stop_with_rows(board, rows):
    """A job stopped on these rows, whichever way the facts about it read."""
    claim, lease = _claim(board.client)
    fields = [{"id": f"q{i}", "kind": "textarea", "value": "", "required": True, **row} for i, row in enumerate(rows)]
    body = {"job_id": claim["job"]["job_id"], "apply_profile": "casey", "demo": False, "runner_lease": lease,
            "snapshot": {"url": claim["job"]["url"], "document_id": "d", "coverage": "complete", "fields": fields}}
    assert board.client.post(PREFIX + "review", json=body).json()["status"] == "needs_user"
    assert board.client.post(RUNNER + "finish", json={**lease, "outcome": "needs_user", "evidence": "必答题没有答案。"}).status_code == 200
    return claim["job"]["job_id"]


def _paused(board):
    return board.client.get(PREFIX + "answers").json()


def test_a_saved_answer_carries_the_statement_it_was_given_for(board):
    job_id = _stop_with_rows(board, [{"label": "Acknowledgement", "description": NOTICE}])
    view = _paused(board)
    assert view["paused"][0]["questions"][0]["description"] == NOTICE and view["paused"][0]["questions"][0]["answer"] == ""
    # A form saved with no statement leaves this question open, the safe way round.
    view = board.client.post(PREFIX + "answers", json={"question": "Acknowledgement", "answer": "Yes", "job_id": job_id}).json()
    assert view["paused"][0]["questions"][0]["answer"] == ""
    view = board.client.post(PREFIX + "answers", json={"question": "Acknowledgement", "description": NOTICE, "answer": "Yes", "job_id": job_id}).json()
    assert view["paused"][0]["questions"][0]["answer"] == "Yes"
    assert {a["description"] for a in view["answers"]} == {"", NOTICE}


def test_the_panel_sends_only_the_title_and_the_answer_still_covers_the_statement(board):
    job_id = _stop_with_rows(board, [{"label": "Acknowledgement", "description": NOTICE}])
    assert board.client.post(PREFIX + "stuck-answer", json={"question": "Acknowledgement", "answer": "Yes", "job_id": job_id}).status_code == 200
    item = confirmed_answers.all_answers({})[0]
    assert item["description"] == NOTICE and confirmed_answer("Acknowledgement", _context(item), NOTICE)
    assert confirmed_answer("Acknowledgement", _context(item), ARBITRATION) is None


EDUCATION = [{"label": "Education History", "kind": "select", "options": ["January", "February"]}] * 4


def test_a_title_repeated_on_one_form_gets_no_answer_box(board):
    job_id = _stop_with_rows(board, [*EDUCATION, {"label": "Why us?"}])
    job = _paused(board)["paused"][0]
    assert [q["label"] for q in job["questions"]] == ["Why us?"]
    assert len(job["page_issues"]) == 1 and "Education History" in job["page_issues"][0] and "4 次" in job["page_issues"][0]
    for route, body in (("answers", {}), ("stuck-answer", {})):
        reply = board.client.post(PREFIX + route, json={"question": "Education History", "answer": "January", "job_id": job_id, **body})
        assert reply.status_code == 409 and "手动填" in reply.json()["detail"], route
    assert confirmed_answers.all_answers({}) == []


def test_only_one_required_row_of_a_repeated_title_still_gets_no_answer_box(board):
    rows = [{**EDUCATION[0], "required": index == 0, "label": "Education History"} for index in range(4)]
    job_id = _stop_with_rows(board, rows)
    job = _paused(board)["paused"][0]
    assert job["questions"] == []
    assert "4 次" in job["page_issues"][0]
    for route in ("answers", "stuck-answer"):
        reply = board.client.post(PREFIX + route, json={"question": "Education History", "answer": "January", "job_id": job_id})
        assert reply.status_code == 409 and "手动填" in reply.json()["detail"], route
    assert confirmed_answers.all_answers({}) == []


def test_two_statements_under_one_title_each_keep_a_box_and_the_panel_answer_lands_on_the_one_she_read(board):
    job_id = _stop_with_rows(board, [{"label": "Acknowledgement", "description": NOTICE}, {"label": "Acknowledgement", "description": ARBITRATION}])
    assert len(_paused(board)["paused"][0]["questions"]) == 2
    reply = board.client.post(PREFIX + "stuck-answer", json={"question": "Acknowledgement", "answer": "Yes", "job_id": job_id})
    assert reply.status_code == 409, "with no statement it cannot tell which she answered"
    assert board.client.post(PREFIX + "stuck-answer", json={"question": "Acknowledgement", "description": ARBITRATION, "answer": "Yes", "job_id": job_id}).status_code == 200
    assert [a["description"] for a in confirmed_answers.all_answers({})] == [ARBITRATION]


def test_a_statement_nobody_can_see_gets_no_answer_on_the_board_until_a_stop_shows_it(board):
    reply = board.client.post(PREFIX + "answers", json={"question": "GDPR Acknowledgement", "answer": "I Acknowledge/Accept this Statement", "job_id": "job-1", "company": "Illumio"})
    assert reply.status_code == 409 and "只写在申请页上" in reply.json()["detail"]
    assert confirmed_answers.all_answers({}) == []
    job_id = _stop_with_rows(board, [{"label": "GDPR Acknowledgement"}])
    assert board.client.post(PREFIX + "answers", json={"question": "GDPR Acknowledgement", "answer": "Yes", "job_id": job_id}).status_code == 200
    assert board.client.post(PREFIX + "answers", json={"question": "GDPR Acknowledgement", "description": NOTICE, "answer": "Yes", "job_id": ""}).status_code == 200


def _facts(monkeypatch, **fact):
    monkeypatch.setattr(agent_lab, "stopped_job_facts", lambda _cfg, jobs: {job["job_id"]: fact for job in jobs})


def test_a_role_no_longer_new_leaves_the_card_and_its_row_says_nothing_waits(board, monkeypatch):
    _stop_with_rows(board, [{"label": "Why us?"}])
    _facts(monkeypatch, closed=True)
    view = _paused(board)
    assert view["paused"] == [] and "不用再管" in view["batch"][0]["note"] and view["batch"][0]["unanswered"] == 0


@pytest.mark.parametrize("why", ["正在技术面，这家公司先不投。", "职位名不是工程岗，不在自动队列里。", "投不了：要另外传文件：Writing sample"])
def test_a_job_that_cannot_run_says_why_with_no_boxes_and_no_retry(board, monkeypatch, why):
    _stop_with_rows(board, [{"label": "Motivations"}])
    _facts(monkeypatch, why=why)
    view = _paused(board)
    job = view["paused"][0]
    assert job["note"] == why and job["questions"] == [] and job["page_issues"] == [] and job["can_retry"] is False
    assert view["batch"][0]["note"] == why and view["batch"][0]["unanswered"] == 0


def test_a_stop_older_than_the_code_drops_its_questions_when_the_form_now_reads_ready(board, monkeypatch):
    _stop_with_rows(board, [{"label": "Phone Number", "kind": "radio", "options": ["Yes - I consent", "No"]}])
    _facts(monkeypatch, verdict="ready")
    monkeypatch.setattr(agent_lab, "code_changed_at", lambda: agent_runner.time.time() + 60)
    view = _paused(board)
    job = view["paused"][0]
    assert job["questions"] == [] and job["can_retry"] is True and "不用答" in job["note"] and "重跑" in job["note"]
    assert view["batch"][0]["unanswered"] == 0 and "不用答" in view["batch"][0]["note"]


def test_a_stop_older_than_the_code_hands_back_no_old_questions_while_the_form_is_unread(board, monkeypatch):
    _stop_with_rows(board, [{"label": "Phone Number", "kind": "radio", "options": ["Yes - I consent", "No"]}])
    _facts(monkeypatch, verdict="unread")
    monkeypatch.setattr(agent_lab, "code_changed_at", lambda: agent_runner.time.time() + 60)
    job = _paused(board)["paused"][0]
    assert job["questions"] == [] and job["can_retry"] is True and "重新读表单" in job["note"]
    monkeypatch.setattr(agent_lab, "code_changed_at", lambda: agent_runner.time.time() - 60)
    assert [q["label"] for q in _paused(board)["paused"][0]["questions"]] == ["Phone Number"], "a newer stop keeps them"


def test_a_stop_newer_than_the_code_keeps_its_questions_even_when_the_form_reads_ready(board, monkeypatch):
    _stop_with_rows(board, [{"label": "Why us?"}])
    _facts(monkeypatch, verdict="ready")
    monkeypatch.setattr(agent_lab, "code_changed_at", lambda: agent_runner.time.time() - 60)
    job = _paused(board)["paused"][0]
    assert [q["label"] for q in job["questions"]] == ["Why us?"] and job["note"] == "" and job["can_retry"] is True


@pytest.mark.parametrize("fact", [{"verdict": "ask", "waits": "还有题没答"}, {"verdict": "essay", "waits": "打开开关"}, {"verdict": "essay"}])
def test_a_form_that_still_asks_keeps_its_questions_however_old_the_stop(board, monkeypatch, fact):
    _stop_with_rows(board, [{"label": "Why us?"}])
    _facts(monkeypatch, **fact)
    monkeypatch.setattr(agent_lab, "code_changed_at", lambda: agent_runner.time.time() + 60)
    job = _paused(board)["paused"][0]
    assert [q["label"] for q in job["questions"]] == ["Why us?"]
    assert job["can_retry"] is ("waits" not in fact) and job["note"] == fact.get("waits", "")


def test_a_stop_with_no_review_behind_it_is_not_called_fixed(board, monkeypatch):
    claim, lease = _claim(board.client)
    assert board.client.post(RUNNER + "finish", json={**lease, "outcome": "blocked", "evidence": "申请页一直不在前台。"}).status_code == 200
    _facts(monkeypatch, verdict="ready")
    monkeypatch.setattr(agent_lab, "code_changed_at", lambda: agent_runner.time.time() + 60)
    job = _paused(board)["paused"][0]
    assert job["note"] == "" and job["evidence"] == "申请页一直不在前台。" and job["can_retry"] is True


def test_a_click_that_stopped_on_an_email_code_is_named_on_its_row(board):
    claim, lease = _claim(board.client)
    (board.directory / ("submission-" + digest([claim["job"]["job_id"], "real"]) + ".json")).write_text(json.dumps({"attempts": 1}))
    board.client.post(RUNNER + "finish", json={**lease, "outcome": "blocked", "evidence": "页面显示验证码已发送，并有验证码输入框；需要处理验证步骤。"})
    row = _paused(board)["batch"][0]
    assert row["clicked"] is True and row["needs_code"] is True


@pytest.mark.parametrize("evidence, clicked", [("页面有验证码，需要用户处理。", True), ("页面显示验证码或人机验证，需要用户处理。", True),
                                               ("页面显示验证码已发送，并有验证码输入框；需要处理验证步骤。", False)])
def test_a_captcha_or_an_unclicked_form_is_not_called_waiting_for_a_code(board, evidence, clicked):
    claim, lease = _claim(board.client)
    if clicked:
        (board.directory / ("submission-" + digest([claim["job"]["job_id"], "real"]) + ".json")).write_text(json.dumps({"attempts": 1}))
    board.client.post(RUNNER + "finish", json={**lease, "outcome": "blocked", "evidence": evidence})
    assert _paused(board)["batch"][0]["needs_code"] is False


def _finish_stopped(board, job_id):
    claim = board.client.post(RUNNER + "claim", json={"worker_id": "w"}).json()
    assert claim["job"]["job_id"] == job_id
    return {"lease_id": claim["lease_id"], "token": claim["token"], "worker_id": "w"}


def test_the_requeue_places_count_only_jobs_that_can_run_and_say_where_they_land(board, monkeypatch):
    for index, job in enumerate(board.candidates):
        job["company"] = f"Example {index}"
    board.candidates.append({**board.candidates[0], "job_id": "fresh", "company": "Fresh", "title": "Engineer"})
    assert board.client.post(RUNNER + "start", json={"apply_profile": "casey", "max_jobs": 2}).status_code == 200
    for job_id in ("job-1", "job-2"):
        lease = _finish_stopped(board, job_id)
        assert board.client.post(RUNNER + "finish", json={**lease, "outcome": "needs_user", "evidence": "停下。"}).status_code == 200
    board.candidates[:] = [next(c for c in board.candidates if c["job_id"] == "fresh"), next(c for c in board.candidates if c["job_id"] == "job-2")]
    assert board.client.post(RUNNER + "requeue", json={}).json()["requeued"] == 2
    places = board.client.get(PREFIX + "requeue-places").json()
    assert places == {"runnable": [{"company": "Example 1", "title": "Engineer 2", "place": 2}], "cannot": 1}
    extension = {"Origin": EXTENSION}
    token = board.client.post(PREFIX + "pair", json={"code": board.client.post(PREFIX + "pair-code", json={}).json()["code"]}, headers=extension).json()["token"]
    assert board.client.get(PREFIX + "requeue-places", headers={**extension, "X-JobScan-Review": token}).status_code == 403


def test_next_batch_places_walk_the_queue_like_a_claim():
    job = lambda job_id, company: {"job_id": job_id, "company": company, "title": "Engineer"}
    record = lambda job_id, company, status, finished=1000.0: {"job": job(job_id, company), "status": status, "finished_at": finished}
    candidates = [job("sent-co", "Sent"), job("twin", "Acme"), job("stopped-twin", "Acme"), job("old", "Old"), job("a", "Alpha"), job("stopped", "Beta"),
                  job("clicked-co", "Clicked"), job("clicked-co-2", "Clicked"), job("gap", "Gap")]
    records = [record("done", "Sent", "submitted"), record("stopped-twin", "Acme", "blocked"), record("stopped", "Beta", "needs_user"),
               record("old", "Old", "expired"), record("gap-old", "Gap", "submitted", finished=1000.0 - 40 * 86400)]
    places = agent_lab.next_batch_places(candidates, records, lambda job_id: job_id != "clicked-co", now=1000.0)
    # Acme's first row takes that company's one slot, so its stopped twin waits for a later batch.
    assert places == {"stopped": 3}
