import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import agent_runner


PREFIX = agent_runner.PREFIX


def candidate(job_id="job-1", **changes):
    value = dict(job_id=job_id, company="Example", title="Software Engineer",
                 url=f"https://jobs.example.com/{job_id}", apply_profile="casey",
                 resume_ready=True, status="new")
    return {**value, **changes}


def client(directory, candidates=None, terminal_callback=None, prepare_claim=None, expected=""):
    def auth(request):
        value = request.headers.get("x-owner")
        if not value:
            raise HTTPException(403)
        return value
    app = FastAPI()
    router = agent_runner.build_runner_router(
        {}, auth, candidate_loader=lambda: candidates or [], run_dir=directory,
        terminal_callback=terminal_callback, prepare_claim=prepare_claim,
        expected_extension_version=expected)
    app.state.runner_router = router
    app.include_router(router)
    return TestClient(app, headers={"x-owner": "owner-secret"})


def start(c, **changes):
    return c.post(PREFIX + "/start", json={"apply_profile": "casey", **changes})


def claim(c, worker="worker-1"):
    return c.post(PREFIX + "/claim", json={"worker_id": worker})


def lease(claimed, **changes):
    return {"worker_id": "worker-1", "lease_id": claimed["lease_id"],
            "token": claimed["token"], **changes}


def finish(c, claimed, outcome="submitted", **changes):
    fields = {"outcome": outcome, "evidence": "Explicit employer confirmation", **changes}
    return c.post(PREFIX + "/finish", json=lease(claimed, **fields))


def test_disabled_idle_and_required_auth(tmp_path):
    c = client(tmp_path, [candidate()])
    assert claim(c).json() == {"status": "paused", "reason": "not_started"}
    assert c.get(PREFIX + "/status", headers={"x-owner": ""}).status_code == 403
    assert start(c).status_code == 200
    empty = client(tmp_path, [])
    assert claim(empty).json() == {"status": "idle"}
    assert empty.get(PREFIX + "/status").json()["claimed_count"] == 0


def test_single_claim_across_concurrent_service_instances(tmp_path):
    clients = [client(tmp_path, [candidate(), candidate("job-2")]) for _ in range(8)]
    assert start(clients[0], max_jobs=2).status_code == 200
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda pair: claim(pair[1], f"worker-{pair[0]}").json(), enumerate(clients)))
    assert sum(result["status"] == "claimed" for result in results) == 1
    assert sum(result["status"] == "busy" for result in results) == 7
    assert clients[0].get(PREFIX + "/status").json()["claimed_count"] == 1


def _claim_from_process(directory):
    return claim(client(directory, [candidate(), candidate("job-2")])).json()


def test_file_lock_serializes_independent_server_processes(tmp_path):
    c = client(tmp_path, [candidate(), candidate("job-2")])
    start(c, max_jobs=2)
    with ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_claim_from_process, [str(tmp_path)] * 3))
    assert sum(item["status"] == "claimed" for item in results) == 1
    assert sum(item["status"] == "busy" for item in results) == 2


def test_restart_preserves_active_and_terminal_claims(tmp_path):
    candidates = [candidate()]
    original = client(tmp_path, candidates)
    start(original)
    claimed = claim(original).json()
    restarted = client(tmp_path, candidates)
    assert claim(restarted).json()["status"] == "busy"
    assert finish(restarted, claimed).status_code == 200
    again = client(tmp_path, candidates)
    start(again)
    assert claim(again).json() == {"status": "idle"}
    assert again.get(PREFIX + "/status").json()["jobs"][0]["status"] == "submitted"


def test_timeout_stops_batch_and_never_reclaims(tmp_path, monkeypatch):
    c = client(tmp_path, [candidate(), candidate("job-2")])
    start(c, max_jobs=2)
    claimed = claim(c).json()
    monkeypatch.setattr(agent_runner.time, "time", lambda: claimed["expires_at"] + 1)
    state = c.get(PREFIX + "/status").json()
    assert state["phase"] == "needs_user"
    assert state["jobs"][0]["status"] == "unknown"
    assert not state["enabled"] and state["active"] is None
    assert finish(c, claimed).status_code == 409
    start(c)
    assert claim(c).json()["job"]["job_id"] == "job-2"


@pytest.mark.parametrize("change", [
    {"worker_id": "other-worker"}, {"token": "wrong-token"}, {"lease_id": "absent"},
])
def test_lease_binding_rejects_mismatches(tmp_path, change):
    c = client(tmp_path, [candidate()])
    start(c)
    claimed = claim(c).json()
    assert finish(c, claimed, **change).status_code == 404
    assert c.get(PREFIX + "/status").json()["active"] is not None


def test_owner_binding_and_finish_idempotency(tmp_path):
    c = client(tmp_path, [candidate()])
    start(c)
    claimed = claim(c).json()
    other = client(tmp_path)
    other.headers["x-owner"] = "other-owner"
    assert finish(other, claimed).status_code == 404
    assert other.post(PREFIX + "/event", json=lease(claimed, role="filler", text="hi")).status_code == 404
    assert finish(c, claimed).json()["submitted_count"] == 1
    assert finish(c, claimed).json()["submitted_count"] == 1
    assert finish(c, claimed, outcome="error").status_code == 409


def test_limit_counts_claims_and_pause_does_not_cancel_active(tmp_path):
    c = client(tmp_path, [candidate(), candidate("job-2")])
    start(c)
    claimed = claim(c).json()
    assert start(c).status_code == 409
    paused = c.post(PREFIX + "/pause").json()
    assert paused["phase"] == "pausing" and paused["active"]
    assert claim(c).json()["status"] == "paused"
    assert finish(c, claimed).status_code == 200
    assert claim(c).json()["status"] == "paused"
    state = c.get(PREFIX + "/status").json()
    assert state["claimed_count"] == state["submitted_count"] == 1


def test_paused_active_finish_does_not_enable_larger_batch(tmp_path):
    c = client(tmp_path, [candidate(), candidate("job-2")])
    start(c, max_jobs=2)
    claimed = claim(c).json()
    c.post(PREFIX + "/pause")
    state = finish(c, claimed).json()
    assert not state["enabled"] and state["phase"] == "paused"


@pytest.mark.parametrize("outcome", ["error", "unknown"])
def test_an_unclear_or_broken_result_stops_the_batch(tmp_path, outcome):
    c = client(tmp_path, [candidate(), candidate("job-2")])
    start(c, max_jobs=2)
    claimed = claim(c).json()
    state = finish(c, claimed, outcome).json()
    assert state["phase"] == "needs_user" and state["claimed_count"] == 1
    assert state["submitted_count"] == 0 and not state["enabled"]
    start(c)
    assert claim(c).json()["job"]["job_id"] == "job-2"


@pytest.mark.parametrize("outcome", ["needs_user", "blocked"])
def test_a_stop_before_submit_moves_the_batch_on_and_ends_it_at_the_limit(tmp_path, outcome):
    c = client(tmp_path, [candidate(), candidate("job-2")])
    start(c, max_jobs=2)
    state = finish(c, claim(c).json(), outcome).json()
    assert state["enabled"] and state["submitted_count"] == 0
    second = claim(c).json()
    assert second["job"]["job_id"] == "job-2"
    state = finish(c, second, outcome).json()
    assert state["phase"] == "needs_user" and not state["enabled"] and state["pause_reason"] == outcome


def test_mutable_queue_is_rechecked_without_identity_changes(tmp_path):
    values = [candidate()]
    c = client(tmp_path, values)
    start(c)
    values[0]["apply_profile"] = "alex"
    assert claim(c).json() == {"status": "idle"}
    values.extend([
        candidate("not-ready", resume_ready=False), candidate("applied", status="applied"),
        candidate("js-url", url="javascript:alert(1)"), candidate("wrong-ready", resume_ready=1),
        candidate("url-secret", url="https://user:password@example.com"), candidate("good")])
    assert claim(c).json()["job"]["job_id"] == "good"
    assert values[0]["apply_profile"] == "alex"


def test_public_output_and_disk_never_contain_raw_auth_tokens(tmp_path):
    c = client(tmp_path, [candidate(secret="candidate-secret")])
    start(c)
    claimed = claim(c).json()
    response = c.post(PREFIX + "/event", json=lease(claimed, role="reviewer", to="filler", text="Check name."))
    assert response.json() == {"status": "recorded"}
    public = json.dumps(c.get(PREFIX + "/status").json())
    stored = (tmp_path / "runner_state.json").read_text()
    event_log = (tmp_path / "runner_events.jsonl").read_text()
    for value in ["owner-secret", claimed["token"], "candidate-secret"]:
        assert value not in public + stored + event_log
    assert "owner_hash" not in public and "token_hash" not in public
    assert "Check name." in public
    assert (tmp_path / "runner_state.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "runner_events.jsonl").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("payload", [
    {}, {"apply_profile": "alex"}, {"apply_profile": "casey", "max_jobs": 0},
    {"apply_profile": "casey", "max_jobs": 21}, {"apply_profile": "casey", "max_jobs": True},
])
def test_start_requires_explicit_profile_and_finite_bound(tmp_path, payload):
    c = client(tmp_path)
    assert c.post(PREFIX + "/start", json=payload).status_code == 422


def test_oversize_events_are_rejected_and_corrupt_ledger_fails_closed(tmp_path):
    c = client(tmp_path, [candidate()])
    start(c)
    claimed = claim(c).json()
    assert c.post(PREFIX + "/event", json=lease(claimed, role="filler", text="x" * 2001)).status_code == 422
    (tmp_path / "runner_state.json").write_text("{broken")
    assert claim(c).status_code == 503
    assert (tmp_path / "runner_state.json").read_text() == "{broken"


def test_expired_is_terminal_without_counting_as_submitted_and_callback_runs_once(tmp_path):
    calls = []
    def callback(job, outcome, evidence):
        stored = json.loads((tmp_path / "runner_state.json").read_text())
        assert stored["jobs"][job["job_id"]]["status"] == "claimed"
        assert stored["jobs"][job["job_id"]]["terminal_callback"]["status"] == "started"
        calls.append((job, outcome, evidence))
    c = client(tmp_path, [candidate()], callback)
    start(c)
    claimed = claim(c).json()
    finished = finish(c, claimed, "expired", evidence="This job is no longer available.")
    assert finished.status_code == 200
    state = finished.json()
    assert state["jobs"][0]["status"] == "expired"
    assert state["submitted_count"] == 0 and state["claimed_count"] == 1
    assert state["phase"] == "completed" and not state["enabled"]
    assert finish(c, claimed, "expired").status_code == 200
    assert len(calls) == 1 and calls[0][1] == "expired"
    start(c)
    assert claim(c).json()["status"] == "idle"


def test_expired_does_not_exhaust_a_larger_finite_batch(tmp_path):
    c = client(tmp_path, [candidate(), candidate("job-2")])
    start(c, max_jobs=2)
    state = finish(c, claim(c).json(), "expired").json()
    assert state["phase"] == "idle" and state["enabled"]
    assert claim(c).json()["job"]["job_id"] == "job-2"


def test_terminal_callback_failure_is_durable_and_never_retried(tmp_path):
    calls = []
    def fail(*args):
        calls.append(args)
        raise RuntimeError("SECRET provider response")
    c = client(tmp_path, [candidate()], fail)
    start(c)
    claimed = claim(c).json()
    assert finish(c, claimed, "expired").status_code == 503
    state = c.get(PREFIX + "/status").json()
    assert state["phase"] == "needs_user" and not state["enabled"]
    assert state["jobs"][0]["status"] == "unknown"
    assert state["jobs"][0]["reported_outcome"] == "expired"
    assert state["jobs"][0]["terminal_callback_status"] == "failed"
    assert "SECRET" not in json.dumps(state)
    restarted = client(tmp_path, [candidate()], fail)
    assert finish(restarted, claimed, "expired").status_code == 409
    assert len(calls) == 1
    start(restarted)
    assert claim(restarted).json()["status"] == "idle"


def test_interrupted_terminal_callback_is_not_replayed_after_restart(tmp_path):
    c = client(tmp_path, [candidate()])
    start(c)
    claimed = claim(c).json()
    path = tmp_path / "runner_state.json"
    state = json.loads(path.read_text())
    state["jobs"]["job-1"].update(terminal_callback={"status": "started"}, reported_outcome="expired")
    path.write_text(json.dumps(state))
    restarted = client(tmp_path, [candidate()], lambda *_: pytest.fail("Callback must not replay"))
    current = restarted.get(PREFIX + "/status").json()
    assert current["jobs"][0]["status"] == "unknown"
    assert current["jobs"][0]["terminal_callback_status"] == "interrupted"
    assert finish(restarted, claimed, "expired").status_code == 409


def test_invalid_or_expired_lease_cannot_invoke_terminal_callback(tmp_path, monkeypatch):
    c = client(tmp_path, [candidate()], lambda *_: pytest.fail("Invalid lease invoked callback"))
    start(c)
    claimed = claim(c).json()
    assert finish(c, claimed, "expired", token="wrong").status_code == 404
    monkeypatch.setattr(agent_runner.time, "time", lambda: claimed["expires_at"] + 1)
    assert finish(c, claimed, "expired").status_code == 409


def test_preparation_happens_once_after_durable_claim_and_before_payload(tmp_path):
    calls = []
    def prepare(job):
        state = json.loads((tmp_path / "runner_state.json").read_text())
        assert state["jobs"][job["job_id"]]["status"] == "claimed"
        assert state["jobs"][job["job_id"]]["preparation"]["status"] == "started"
        calls.append(job)
        return {**job, "title": "Prepared title", "private_extra": "not returned"}
    c = client(tmp_path, [candidate()], prepare_claim=prepare)
    start(c)
    result = claim(c)
    assert result.status_code == 200
    assert result.json()["status"] == "claimed" and result.json()["job"]["title"] == "Prepared title"
    assert "private_extra" not in result.json()["job"]
    assert claim(client(tmp_path, [candidate()], prepare_claim=prepare)).json()["status"] == "busy"
    assert len(calls) == 1
    assert c.get(PREFIX + "/status").json()["jobs"][0]["preparation_status"] == "completed"


def test_preparation_none_preserves_candidate_and_ineligible_candidates_never_prepare(tmp_path):
    calls = []
    values = [candidate(apply_profile="alex")]
    c = client(tmp_path, values, prepare_claim=lambda job: calls.append(job))
    start(c)
    assert claim(c).json()["status"] == "idle" and not calls
    values[0]["apply_profile"] = "casey"
    assert claim(c).json()["job"] == candidate()
    assert len(calls) == 1


@pytest.mark.parametrize("bad", [{"job_id": "other"}, {"url": "https://other.example/job"},
                                  {"resume_ready": False}, {"apply_profile": "alex"}])
def test_preparation_cannot_change_target_or_return_unready_candidate(tmp_path, bad):
    c = client(tmp_path, [candidate()], prepare_claim=lambda job: {**job, **bad})
    start(c)
    assert claim(c).status_code == 503
    state = c.get(PREFIX + "/status").json()
    assert state["phase"] == "needs_user" and state["jobs"][0]["status"] == "blocked"
    assert not state["enabled"] and state["active"] is None


def test_preparation_exception_is_private_and_cannot_repeat_after_restart(tmp_path):
    calls = []
    def fail(job):
        calls.append(job)
        raise RuntimeError("SECRET personal data")
    c = client(tmp_path, [candidate()], prepare_claim=fail)
    start(c)
    # Nothing was opened, so the batch goes on to the next role.
    assert claim(c).json()["status"] == "idle"
    state = c.get(PREFIX + "/status").json()
    assert state["jobs"][0]["preparation_status"] == "failed" and state["jobs"][0]["status"] == "blocked"
    assert state["enabled"] and state["active"] is None
    assert "SECRET" not in json.dumps(state)
    restarted = client(tmp_path, [candidate()], prepare_claim=fail)
    start(restarted)
    assert claim(restarted).json()["status"] == "idle" and len(calls) == 1


def test_interrupted_preparation_is_not_replayed(tmp_path):
    c = client(tmp_path, [candidate()])
    start(c)
    claim(c)
    path = tmp_path / "runner_state.json"
    state = json.loads(path.read_text())
    state["jobs"]["job-1"]["preparation"] = {"status": "started"}
    path.write_text(json.dumps(state))
    restarted = client(tmp_path, [candidate()], prepare_claim=lambda *_: pytest.fail("Do not replay"))
    current = restarted.get(PREFIX + "/status").json()
    assert current["jobs"][0]["status"] == "unknown"
    assert current["jobs"][0]["preparation_status"] == "interrupted"
    assert not current["enabled"]
    start(restarted)
    assert claim(restarted).json()["status"] == "idle"


def test_check_and_callable_return_only_active_job_and_expiry(tmp_path):
    c = client(tmp_path, [candidate()])
    start(c)
    claimed = claim(c).json()
    expected = {"status": "active", "job_id": "job-1", "expires_at": claimed["expires_at"]}
    assert c.post(PREFIX + "/check", json=lease(claimed)).json() == expected
    validator = c.app.state.runner_router.require_active_lease
    assert validator(agent_runner.LeaseRequest(**lease(claimed)), "owner-secret", "job-1") == expected
    restarted = client(tmp_path, [candidate()])
    assert restarted.post(PREFIX + "/check", json=lease(claimed)).json() == expected
    # Pausing the queue stops new jobs; it deliberately does not cancel an active job.
    c.post(PREFIX + "/pause")
    assert c.post(PREFIX + "/check", json=lease(claimed)).json() == expected


@pytest.mark.parametrize("change", [{"worker_id": "other"}, {"token": "wrong"}, {"lease_id": "missing"}])
def test_check_rejects_wrong_worker_token_or_lease(tmp_path, change):
    c = client(tmp_path, [candidate()])
    start(c)
    claimed = claim(c).json()
    assert c.post(PREFIX + "/check", json=lease(claimed, **change)).status_code == 404


def test_check_requires_auth_and_callable_binds_owner_and_job(tmp_path):
    c = client(tmp_path, [candidate()])
    start(c)
    claimed = claim(c).json()
    body = agent_runner.LeaseRequest(**lease(claimed))
    assert c.post(PREFIX + "/check", json=lease(claimed), headers={"x-owner": ""}).status_code == 403
    assert c.post(PREFIX + "/check", json=lease(claimed), headers={"x-owner": "other"}).status_code == 404
    validator = c.app.state.runner_router.require_active_lease
    with pytest.raises(HTTPException) as wrong_owner:
        validator(body, "other", "job-1")
    assert wrong_owner.value.status_code == 404
    with pytest.raises(HTTPException) as wrong_job:
        validator(body, "owner-secret", "job-2")
    assert wrong_job.value.status_code == 409
    with pytest.raises(HTTPException) as no_auth:
        validator(body, "", "job-1")
    assert no_auth.value.status_code == 403


@pytest.mark.parametrize("outcome", ["submitted", "expired", "needs_user", "blocked", "error", "unknown"])
def test_check_rejects_every_finished_lease(tmp_path, outcome):
    c = client(tmp_path, [candidate()])
    start(c)
    claimed = claim(c).json()
    assert finish(c, claimed, outcome).status_code == 200
    assert c.post(PREFIX + "/check", json=lease(claimed)).status_code == 409
    with pytest.raises(HTTPException) as finished:
        c.app.state.runner_router.require_active_lease(
            agent_runner.LeaseRequest(**lease(claimed)), "owner-secret", "job-1")
    assert finished.value.status_code == 409


def test_check_expires_lease_and_does_not_renew_it(tmp_path, monkeypatch):
    c = client(tmp_path, [candidate()])
    start(c)
    claimed = claim(c).json()
    c.post(PREFIX + "/check", json=lease(claimed))
    before = c.get(PREFIX + "/status").json()["active"]["expires_at"]
    assert before == claimed["expires_at"]
    monkeypatch.setattr(agent_runner.time, "time", lambda: claimed["expires_at"])
    assert c.post(PREFIX + "/check", json=lease(claimed)).status_code == 409
    state = c.get(PREFIX + "/status").json()
    assert state["jobs"][0]["status"] == "unknown" and state["active"] is None
    assert not state["enabled"]


def test_claim_event_says_why_this_job_was_selected(tmp_path):
    c = client(tmp_path, [candidate(selection_reason="未投递；简历就绪")])
    start(c)
    assert claim(c).json()["job"]["selection_reason"] == "未投递；简历就绪"
    texts = [event["text"] for event in c.get(PREFIX + "/status").json()["events"]]
    assert any("为什么选它：未投递；简历就绪" in text for text in texts)


def test_worker_liveness_and_version_come_only_from_the_page_header(tmp_path):
    c = client(tmp_path, expected="0.5.1")
    assert c.get(PREFIX + "/status").json()["worker"] == {
        "online": False, "version": "", "identity": "", "expected_version": "0.5.1", "runner_url": ""}
    status = c.get(PREFIX + "/status", headers={"x-jobscan-extension-version": "0.5.0",
                                                "x-jobscan-extension-identity": "alex"}).json()
    assert status["worker"] == {"online": True, "version": "0.5.0", "identity": "alex", "expected_version": "0.5.1", "runner_url": ""}
    assert c.get(PREFIX + "/status").json()["worker"]["online"] is True


def test_start_records_when_the_batch_began(tmp_path):
    c = client(tmp_path)
    assert c.get(PREFIX + "/status").json()["started_at"] == 0
    assert start(c).json()["started_at"] > 0


def start_batch(api, count):
    return api.post(PREFIX + "/start", json={"apply_profile": "casey", "max_jobs": count}, headers={"x-owner": "owner"})


def claim_job(api):
    return api.post(PREFIX + "/claim", json={"worker_id": "w"}, headers={"x-owner": "owner"}).json()


def finish_job(api, claim, outcome="submitted", evidence="done"):
    body = {"lease_id": claim["lease_id"], "token": claim["token"], "worker_id": "w", "outcome": outcome, "evidence": evidence}
    return api.post(PREFIX + "/finish", json=body, headers={"x-owner": "owner"})


def runner_client(directory, candidates, **options):
    def auth(request):
        return request.headers.get("x-owner") or "browser:board"
    app = FastAPI()
    app.include_router(agent_runner.build_runner_router({}, auth, candidate_loader=lambda: candidates, run_dir=directory, **options))
    return TestClient(app)


def test_a_batch_above_ten_survives_a_reload(tmp_path):
    api = runner_client(tmp_path, [candidate(f"job-{i}", company=f"Co {i}") for i in range(15)])
    assert start_batch(api, 15).status_code == 200
    assert api.get(PREFIX + "/status", headers={"x-owner": "owner"}).status_code == 200
    assert claim_job(api)["status"] == "claimed"


def test_one_role_per_company_in_a_batch_and_the_batch_ends_when_nothing_is_left(tmp_path):
    jobs = [candidate("a-1", company="Acme"), candidate("a-2", company="ACME, "), candidate("b-1", company="Beta")]
    api = runner_client(tmp_path, jobs, one_per_company=True)
    start_batch(api, 5)
    first = claim_job(api)
    assert first["job"]["job_id"] == "a-1"
    finish_job(api, first, "needs_user")
    second = claim_job(api)
    assert second["job"]["job_id"] == "b-1"
    finish_job(api, second)
    assert claim_job(api) == {"status": "paused", "reason": "queue_empty"}
    start_batch(api, 5)
    assert claim_job(api)["job"]["job_id"] == "a-2"


@pytest.mark.parametrize("outcome", ["submitted", "unknown"])
def test_a_role_that_may_have_gone_out_holds_its_company_into_the_next_batch(tmp_path, outcome):
    jobs = [candidate("a-1", company="Acme"), candidate("a-2", company="ACME, "), candidate("b-1", company="Beta")]
    api = runner_client(tmp_path, jobs, one_per_company=True)
    start_batch(api, 5)
    finish_job(api, claim_job(api), outcome)
    start_batch(api, 5)
    assert claim_job(api)["job"]["job_id"] == "b-1"


def test_a_stop_after_the_click_holds_its_company_and_one_before_it_does_not(tmp_path):
    jobs = [candidate("a-1", company="Acme"), candidate("a-2", company="Acme"),
            candidate("b-1", company="Beta"), candidate("b-2", company="Beta")]
    clicked = set()
    api = runner_client(tmp_path, jobs, one_per_company=True, never_clicked=lambda job_id: job_id not in clicked)
    start_batch(api, 5)
    claimed = claim_job(api)
    clicked.add(claimed["job"]["job_id"])
    finish_job(api, claimed, "blocked", "页面出现验证码。")   # a-1, after its click
    start_batch(api, 5)
    second = claim_job(api)
    assert second["job"]["job_id"] == "b-1"
    finish_job(api, second, "needs_user", "有一道题要你回答。")
    start_batch(api, 5)
    assert claim_job(api)["job"]["job_id"] == "b-2"


def test_the_hold_ends_after_the_same_gap_as_an_applied_date(tmp_path, monkeypatch):
    api = runner_client(tmp_path, [candidate("a-1", company="Acme"), candidate("a-2", company="Acme")], one_per_company=True)
    start_batch(api, 5)
    finish_job(api, claim_job(api), "submitted")
    now = agent_runner.time.time()
    monkeypatch.setattr(agent_runner.time, "time", lambda: now + (agent_runner.SAME_COMPANY_GAP_DAYS - 1) * 86400)
    start_batch(api, 5)
    assert claim_job(api) == {"status": "idle"}
    monkeypatch.setattr(agent_runner.time, "time", lambda: now + (agent_runner.SAME_COMPANY_GAP_DAYS + 1) * 86400)
    start_batch(api, 5)
    assert claim_job(api)["job"]["job_id"] == "a-2"


def test_three_stops_in_a_row_pause_the_batch_and_a_success_resets_the_count(tmp_path):
    api = runner_client(tmp_path, [candidate(f"job-{i}", company=f"Co {i}") for i in range(8)])
    start_batch(api, 8)
    for outcome in ("needs_user", "needs_user", "submitted", "needs_user", "needs_user"):
        state = finish_job(api, claim_job(api), outcome).json()
        assert state["enabled"] is True
    state = finish_job(api, claim_job(api), "needs_user").json()
    assert state["enabled"] is False and state["pause_reason"] == "too_many_stops"
    assert state["phase"] == "needs_user"


def test_an_employer_refusal_does_not_count_toward_the_stop_limit(tmp_path):
    api = runner_client(tmp_path, [candidate(f"job-{i}", company=f"Co {i}") for i in range(5)])
    start_batch(api, 5)
    for _ in range(4):
        state = finish_job(api, claim_job(api), "blocked", "雇主拒收：already applied").json()
        assert state["enabled"] is True


def test_a_refusal_on_the_last_job_of_a_batch_reads_as_finished_not_as_waiting_for_her(tmp_path):
    api = runner_client(tmp_path, [candidate("job-1")])
    start_batch(api, 1)
    state = finish_job(api, claim_job(api), "blocked", "雇主拒收：already applied").json()
    assert state["enabled"] is False and state["pause_reason"] == "limit_reached" and state["phase"] == "completed"


def test_a_stop_on_the_last_job_of_a_batch_still_waits_for_her(tmp_path):
    api = runner_client(tmp_path, [candidate("job-1")], never_clicked=lambda _: True)
    start_batch(api, 1)
    state = finish_job(api, claim_job(api), "blocked", "雇主拒收：already applied, with no click behind it").json()
    assert state["pause_reason"] == "blocked" and state["phase"] == "needs_user"


def test_a_spam_flag_stops_the_batch_at_once(tmp_path):
    api = runner_client(tmp_path, [candidate(f"job-{i}", company=f"Co {i}") for i in range(3)])
    start_batch(api, 3)
    state = finish_job(api, claim_job(api), "blocked", "雇主拒收：Possible spam detected").json()
    assert state["enabled"] is False and state["pause_reason"] == "spam_flag"


@pytest.mark.parametrize("outcome, evidence", [
    ("needs_user", "页面上写着这句话，但还没有点提交，不算雇主拒收，请你看一眼：We receive too many applications to respond."),
    ("needs_user", "Reviewer quoted the form: possible spam"),
    ("blocked", "雇主拒收：We receive too many applications to respond."),   # An older page, with no click on file.
])
def test_limit_words_with_no_click_behind_them_do_not_stop_the_batch_as_spam(tmp_path, outcome, evidence):
    api = runner_client(tmp_path, [candidate(f"job-{i}", company=f"Co {i}") for i in range(3)], never_clicked=lambda _: True)
    start_batch(api, 3)
    state = finish_job(api, claim_job(api), outcome, evidence).json()
    assert state["enabled"] is True and state["pause_reason"] != "spam_flag"
    assert state["jobs"][0]["status"] == outcome


def test_a_spam_flag_after_a_real_click_still_stops_the_batch(tmp_path):
    clicked = set()
    api = runner_client(tmp_path, [candidate(f"job-{i}", company=f"Co {i}") for i in range(3)], never_clicked=lambda job_id: job_id not in clicked)
    start_batch(api, 3)
    claimed = claim_job(api)
    clicked.add(claimed["job"]["job_id"])
    state = finish_job(api, claimed, "blocked", "雇主拒收：Too many applications from this person.").json()
    assert state["enabled"] is False and state["pause_reason"] == "spam_flag"
    assert state["phase"] == "needs_user"
    assert any("疑似垃圾" in event["text"] for event in state["events"])


def test_a_refusal_with_no_click_behind_it_counts_as_a_stop_and_tells_the_owner(tmp_path):
    told = []
    api = runner_client(tmp_path, [candidate(f"job-{i}", company=f"Co {i}") for i in range(3)],
                        never_clicked=lambda _: True, notify=told.append)
    start_batch(api, 3)
    finish_job(api, claim_job(api), "blocked", "雇主拒收：Application limit reached.")
    assert any("停下了" in message for message in told)


def test_requeue_frees_only_stopped_jobs_that_never_clicked_and_only_for_a_person(tmp_path):
    jobs = [candidate(f"job-{i}", company=f"Co {i}") for i in range(3)]
    clicked = set()
    api = runner_client(tmp_path, jobs, never_clicked=lambda job_id: job_id not in clicked)
    start_batch(api, 3)
    finish_job(api, claim_job(api), "needs_user")   # job-0
    second = claim_job(api)
    clicked.add(second["job"]["job_id"])
    finish_job(api, second, "needs_user")           # job-1 clicked Submit
    finish_job(api, claim_job(api), "submitted")    # job-2
    assert api.post(PREFIX + "/requeue", headers={"x-owner": "extension:token"}).status_code == 403
    assert api.post(PREFIX + "/requeue", headers={"x-owner": "browser:board"}).json() == {"requeued": 1}
    start_batch(api, 3)
    claim = claim_job(api)
    assert claim["job"]["job_id"] == "job-0"
    finish_job(api, claim)
    assert claim_job(api)["status"] == "idle" or claim_job(api)["status"] == "paused"


def test_a_batch_holds_the_mac_awake_only_while_it_can_run(tmp_path, monkeypatch):
    started = []

    class Awake:
        def __init__(self, args):
            started.append(args)
            self.running = True

        def poll(self):
            return None if self.running else 0

        def terminate(self):
            self.running = False
    monkeypatch.setattr(agent_runner.subprocess, "Popen", Awake)
    api = runner_client(tmp_path, [candidate()], stay_awake=True)
    assert started == [] or api is not None
    start_batch(api, 1)
    assert len(started) == 1 and started[0][0] == "/usr/bin/caffeinate"
    finish_job(api, claim_job(api))
    assert len(started) == 1
    start_batch(api, 1)
    assert len(started) == 2


def test_a_receipt_this_ledger_never_saw_stops_the_claim_and_holds_the_company(tmp_path):
    # Fill → Review → Submit on the panel, with no receipt from the employer: the role stays new and only the permit file knows.
    jobs = [candidate("a-1", company="Acme"), candidate("a-2", company="ACME, "), candidate("b-1", company="Beta")]
    api = runner_client(tmp_path, jobs, one_per_company=True, never_clicked=lambda job_id: job_id != "a-1")
    start_batch(api, 5)
    assert claim_job(api)["job"]["job_id"] == "b-1"


def test_such_a_job_is_not_claimed_even_without_the_company_rule_or_on_a_requeue_or_a_retry(tmp_path):
    clicked = set()
    api = runner_client(tmp_path, [candidate("a-1", company="Acme")], never_clicked=lambda job_id: job_id not in clicked)
    start_batch(api, 5)
    finish_job(api, claim_job(api), "needs_user", "有一道题要你回答。")
    assert api.post(PREFIX + "/requeue").json() == {"requeued": 1}
    clicked.add("a-1")   # she pressed Submit on the panel after that stop
    start_batch(api, 5)
    assert claim_job(api)["status"] != "claimed"
    assert api.post(PREFIX + "/retry", json={"job_id": "a-1"}).status_code == 409


def test_an_unknown_with_no_permit_on_file_is_a_plain_retryable_stop(tmp_path):
    jobs = [candidate("a-1", company="Acme"), candidate("a-2", company="Acme")]
    api = runner_client(tmp_path, jobs, one_per_company=True, never_clicked=lambda _: True)
    start_batch(api, 5)
    claimed = claim_job(api)
    response = finish_job(api, claimed, "unknown", "执行回复超时，提交结果尚不明确。")
    state = response.json()
    record = state["jobs"][0]
    assert record["status"] == "blocked" and "从没点过提交" in record["evidence"]
    # Nothing can have gone out, but the page was lost, and the next one would meet the same page.
    assert state["enabled"] is False and state["pause_reason"] == "blocked" and state["phase"] == "needs_user"
    # A network retry of the same report is not a changed outcome.
    assert finish_job(api, claimed, "unknown", "执行回复超时，提交结果尚不明确。").status_code == 200
    assert api.post(PREFIX + "/retry", json={"job_id": "a-1"}).status_code == 200
    api.post(PREFIX + "/pause", headers={"x-owner": "owner"})
    assert api.post(PREFIX + "/requeue").json() == {"requeued": 1}
    # It holds nothing: the sibling is free in the next batch.
    start_batch(api, 5)
    assert claim_job(api)["job"]["job_id"] == "a-1"


def test_an_unknown_after_a_click_stays_unknown_and_holds_everything(tmp_path):
    clicked = set()
    jobs = [candidate("a-1", company="Acme"), candidate("a-2", company="Acme")]
    api = runner_client(tmp_path, jobs, one_per_company=True, never_clicked=lambda job_id: job_id not in clicked)
    start_batch(api, 5)
    claimed = claim_job(api)
    clicked.add("a-1")
    state = finish_job(api, claimed, "unknown", "没有读到回执。").json()
    assert state["jobs"][0]["status"] == "unknown" and state["pause_reason"] == "unknown"
    assert api.post(PREFIX + "/retry", json={"job_id": "a-1"}).status_code == 409
    start_batch(api, 5)
    assert claim_job(api) == {"status": "idle"}


def test_a_finish_for_a_lease_this_owner_does_not_hold_is_404(tmp_path):
    api = runner_client(tmp_path, [candidate()])
    start_batch(api, 1)
    claimed = claim_job(api)
    body = {"lease_id": claimed["lease_id"], "token": claimed["token"], "worker_id": "w", "outcome": "submitted", "evidence": "x"}
    assert api.post(PREFIX + "/finish", json=body, headers={"x-owner": "someone-else"}).status_code == 404


def test_a_retry_waits_for_the_form_read_instead_of_calling_the_job_ineligible(tmp_path):
    """A form read over a day ago is unread until the background read finishes; that is not a reason to refuse the retry."""
    job = {"job_id": "ab:x", "company": "X", "title": "T", "url": "https://jobs.ashbyhq.com/x/1/application",
           "apply_profile": "casey", "resume_ready": True, "status": "new"}
    ready, reading, started = [job], [False], []
    app = FastAPI()
    app.include_router(agent_runner.build_runner_router({}, lambda request: "browser:owner", candidate_loader=lambda: list(ready), run_dir=tmp_path,
                                                        never_clicked=lambda job_id: True, on_start=lambda: started.append(1),
                                                        reading=lambda: reading[0]))
    client = TestClient(app)
    client.post(PREFIX + "/start", json={"apply_profile": "casey", "max_jobs": 1})
    claimed = client.post(PREFIX + "/claim", json={"worker_id": "w"}).json()
    client.post(PREFIX + "/finish", json={"worker_id": "w", "lease_id": claimed["lease_id"], "token": claimed["token"],
                                          "outcome": "needs_user", "evidence": "stop"})
    started.clear()
    ready[:], reading[0] = [], True
    assert client.post(PREFIX + "/retry", json={"job_id": "ab:x"}).status_code == 200
    assert started == [1]
    assert client.post(PREFIX + "/claim", json={"worker_id": "w"}).json()["status"] == "idle"
    ready[:], reading[0] = [job], False
    assert client.post(PREFIX + "/claim", json={"worker_id": "w"}).json()["status"] == "claimed"


def retry_job(api, job_id):
    return api.post(PREFIX + "/retry", json={"job_id": job_id})


def runner_status(api):
    return api.get(PREFIX + "/status", headers={"x-owner": "owner"}).json()


def stopped_role_with_a_sibling_gone_out(directory, outcome, **options):
    """Role b at Acme stops before Submit, then role a at the same company is clicked and ends with this outcome."""
    clicked = set()
    jobs = [candidate("b", company="Acme"), candidate("a", company="ACME, ")]
    api = runner_client(directory, jobs, one_per_company=True, never_clicked=lambda job_id: job_id not in clicked, **options)
    start_batch(api, 1)
    finish_job(api, claim_job(api), "needs_user", "有一道题要你回答。")
    start_batch(api, 1)
    sibling = claim_job(api)
    assert sibling["job"]["job_id"] == "a"
    clicked.add("a")
    finish_job(api, sibling, outcome, "没有读到回执。")
    return api, jobs


@pytest.mark.parametrize("outcome", ["unknown", "submitted", "blocked"])
def test_a_retry_is_refused_while_another_role_at_the_company_may_have_gone_out(tmp_path, outcome):
    started = []
    api, _ = stopped_role_with_a_sibling_gone_out(tmp_path, outcome, on_start=lambda: started.append(1))
    started.clear()
    refused = retry_job(api, "b")
    assert refused.status_code == 409
    assert "Acme" in refused.json()["detail"] and "可能已经投出去了" in refused.json()["detail"]
    state = runner_status(api)
    assert state["enabled"] is False and state["retry_job_id"] is None
    assert started == []   # Nothing was read or armed for a retry that cannot run.


def test_a_click_only_a_receipt_remembers_also_refuses_the_retry(tmp_path):
    # Fill → Review → Submit on the panel: no ledger record for a, only its receipt.
    clicked = set()
    jobs = [candidate("b", company="Acme"), candidate("a", company="Acme")]
    api = runner_client(tmp_path, jobs, one_per_company=True, never_clicked=lambda job_id: job_id not in clicked)
    start_batch(api, 1)
    finish_job(api, claim_job(api), "needs_user", "有一道题要你回答。")
    clicked.add("a")
    assert retry_job(api, "b").status_code == 409


def test_a_click_on_a_role_the_queue_leaves_out_holds_the_company_too(tmp_path):
    # Fill → Review → Submit on a sibling preflight would not run: it is not a candidate and has no ledger record, only its receipt.
    outside = []
    jobs = [candidate("b", company="Acme"), candidate("c", company="Beta")]
    api = runner_client(tmp_path, jobs, one_per_company=True, never_clicked=lambda _: True, clicked_roles=lambda: outside)
    start_batch(api, 1)
    first = claim_job(api)
    assert first["job"]["job_id"] == "b"
    finish_job(api, first, "needs_user", "有一道题要你回答。")
    outside.append({"job_id": "a", "company": "ACME, ", "title": "Backend Engineer"})
    refused = retry_job(api, "b")
    assert refused.status_code == 409 and "Backend Engineer" in refused.json()["detail"]
    start_batch(api, 2)
    assert claim_job(api)["job"]["job_id"] == "c", "and the queue walks past that company"


def test_a_sibling_that_stopped_before_its_click_does_not_refuse_the_retry(tmp_path):
    jobs = [candidate("b", company="Acme"), candidate("a", company="Acme")]
    api = runner_client(tmp_path, jobs, one_per_company=True, never_clicked=lambda _: True)
    start_batch(api, 1)
    finish_job(api, claim_job(api), "needs_user", "有一道题要你回答。")
    start_batch(api, 1)
    finish_job(api, claim_job(api), "needs_user", "有一道题要你回答。")
    assert retry_job(api, "b").status_code == 200
    assert claim_job(api)["job"]["job_id"] == "b"


def test_the_retry_hold_ends_after_the_same_gap_as_the_queue(tmp_path, monkeypatch):
    api, jobs = stopped_role_with_a_sibling_gone_out(tmp_path, "unknown")
    del jobs[1]   # While that role stays new with a receipt on file, the queue holds the company too.
    now = agent_runner.time.time()
    monkeypatch.setattr(agent_runner.time, "time", lambda: now + (agent_runner.SAME_COMPANY_GAP_DAYS + 1) * 86400)
    assert retry_job(api, "b").status_code == 200
    assert claim_job(api)["job"]["job_id"] == "b"


def test_a_click_while_a_retry_waits_stops_the_claim(tmp_path):
    clicked = set()
    jobs = [candidate("b", company="Acme"), candidate("a", company="Acme")]
    api = runner_client(tmp_path, jobs, one_per_company=True, never_clicked=lambda job_id: job_id not in clicked)
    start_batch(api, 1)
    finish_job(api, claim_job(api), "needs_user", "有一道题要你回答。")
    assert retry_job(api, "b").status_code == 200
    clicked.add("a")   # She pressed Submit on a's panel while the retry waited for the page.
    assert claim_job(api) == {"status": "paused", "reason": "needs_user"}
    assert runner_status(api)["active"] is None


def test_a_queue_that_cannot_be_read_refuses_the_retry(tmp_path):
    class Queue(list):
        broken = False

        def __iter__(self):
            if self.broken:
                raise RuntimeError("database locked")
            return super().__iter__()
    jobs = Queue([candidate("b", company="Acme")])
    api = runner_client(tmp_path, jobs, one_per_company=True, never_clicked=lambda _: True)
    start_batch(api, 1)
    finish_job(api, claim_job(api), "needs_user", "有一道题要你回答。")
    jobs.broken = True
    assert retry_job(api, "b").status_code == 503
    assert runner_status(api)["retry_job_id"] is None


def test_a_batch_does_not_end_while_the_forms_are_still_being_read(tmp_path):
    told, reads = [], [True]
    jobs = [candidate("a", company="Acme")]
    api = runner_client(tmp_path, jobs, notify=told.append, reading=lambda: reads[0])
    start_batch(api, 5)
    finish_job(api, claim_job(api), "needs_user", "有一道题要你回答。")
    assert claim_job(api) == {"status": "idle"}
    state = runner_status(api)
    assert state["enabled"] is True and state["pause_reason"] != "queue_empty"
    assert not any("这一轮结束" in message for message in told)
    # The read turned up another ready form, and the batch carries on.
    jobs.append(candidate("b", company="Beta"))
    reads[0] = False
    second = claim_job(api)
    assert second["job"]["job_id"] == "b"
    finish_job(api, second, "needs_user", "有一道题要你回答。")
    assert claim_job(api) == {"status": "paused", "reason": "queue_empty"}


def test_a_new_batch_cannot_replace_a_retry_that_is_still_waiting(tmp_path):
    jobs = [candidate("a", company="Acme"), candidate("b", company="Beta")]
    api = runner_client(tmp_path, jobs, never_clicked=lambda _: True)
    start_batch(api, 1)
    finish_job(api, claim_job(api), "needs_user", "有一道题要你回答。")
    assert retry_job(api, "a").status_code == 200
    refused = start_batch(api, 1)
    assert refused.status_code == 409
    assert "Acme" in refused.json()["detail"] and "暂停" in refused.json()["detail"]
    assert runner_status(api)["retry_job_id"] == "a"
    assert claim_job(api)["job"]["job_id"] == "a"


def test_a_retry_no_page_picks_up_gives_up_so_the_board_is_not_stuck(tmp_path, monkeypatch):
    jobs = [candidate("a", company="Acme"), candidate("b", company="Beta")]
    api = runner_client(tmp_path, jobs, never_clicked=lambda _: True)
    start_batch(api, 1)
    finish_job(api, claim_job(api), "needs_user", "有一道题要你回答。")
    retry_job(api, "a")
    now = agent_runner.time.time()
    monkeypatch.setattr(agent_runner.time, "time", lambda: now + agent_runner.RETRY_WAIT_SECONDS - 5)
    assert runner_status(api)["enabled"] is True
    monkeypatch.setattr(agent_runner.time, "time", lambda: now + agent_runner.RETRY_WAIT_SECONDS + 5)
    state = runner_status(api)
    assert state["enabled"] is False and state["retry_job_id"] is None and state["phase"] == "needs_user"
    assert "没点提交" in state["events"][-1]["text"]
    assert claim_job(api) == {"status": "paused", "reason": "needs_user"}
    assert start_batch(api, 1).status_code == 200
    assert claim_job(api)["job"]["job_id"] == "b"


def test_a_page_waiting_on_a_long_form_read_keeps_the_retry_alive(tmp_path, monkeypatch):
    reads, jobs = [False], [candidate("a", company="Acme")]
    api = runner_client(tmp_path, jobs, never_clicked=lambda _: True, reading=lambda: reads[0])
    start_batch(api, 1)
    finish_job(api, claim_job(api), "needs_user", "有一道题要你回答。")
    jobs.clear()
    reads[0] = True
    retry_job(api, "a")
    now = agent_runner.time.time()
    for step in (1, 2, 3):
        moment = now + step * (agent_runner.RETRY_WAIT_SECONDS - 5)
        monkeypatch.setattr(agent_runner.time, "time", lambda moment=moment: moment)
        assert claim_job(api) == {"status": "idle"}
        assert runner_status(api)["retry_job_id"] == "a"
    # The page went away during the read, so nothing asks any more.
    monkeypatch.setattr(agent_runner.time, "time", lambda: now + 4 * agent_runner.RETRY_WAIT_SECONDS)
    assert runner_status(api)["retry_job_id"] is None


def test_pausing_cancels_a_waiting_retry(tmp_path):
    jobs = [candidate("a", company="Acme"), candidate("b", company="Beta")]
    api = runner_client(tmp_path, jobs, never_clicked=lambda _: True)
    start_batch(api, 1)
    finish_job(api, claim_job(api), "needs_user", "有一道题要你回答。")
    retry_job(api, "a")
    assert api.post(PREFIX + "/pause", headers={"x-owner": "owner"}).json()["retry_job_id"] is None
    assert start_batch(api, 1).status_code == 200
    assert claim_job(api)["job"]["job_id"] == "b"


def test_a_resume_that_will_not_prepare_is_skipped_until_three_in_a_row_pause_the_batch(tmp_path):
    jobs = [candidate(f"job-{i}", company=f"Co {i}") for i in range(4)]
    def fail(job):
        raise RuntimeError("render failed")
    c = client(tmp_path, jobs, prepare_claim=fail)
    start(c, max_jobs=4)
    assert [claim(c).json()["status"] for _ in range(2)] == ["idle", "idle"]
    assert claim(c).status_code == 503
    state = c.get(PREFIX + "/status").json()
    assert not state["enabled"] and [r["status"] for r in state["jobs"]] == ["blocked"] * 3
