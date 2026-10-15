"""Runner leases remain mandatory throughout the real review/submit API path."""
import json
import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent_lab, agent_runner, application_identity
from app.agent_review import demo_context, review


PREFIX = "/api/agent-lab/"
RUNNER = PREFIX + "runner/"


@pytest.fixture
def leased_client(tmp_path, monkeypatch):
    candidates = [{"job_id": f"job-{i}", "company": "Example", "title": "Backend Engineer",
                   "url": f"https://jobs.ashbyhq.com/example/job-{i}/application",
                   "apply_profile": "casey", "resume_ready": True, "status": "new"}
                  for i in (1, 2)]
    calls = []
    monkeypatch.setattr(agent_lab, "runner_candidates", lambda _: candidates)
    monkeypatch.setattr(application_identity, "prepare_casey_claim", lambda _cfg, job: job)

    def context(job_id, profile, _url):
        return {**demo_context(), "job_id": job_id, "apply_profile": profile,
                "application_status": "new", "submission_blocker": ""}

    def reviewer(snapshot, ctx, cfg):
        calls.append(ctx["job_id"])
        def model(*_args):
            return json.dumps({"decision": "pass", "checked_field_ids": ["first"], "support": {"first": ["name-first"]},
                               "issues": [], "summary": "Confirmed fixture facts checked."}), "fixture-model"
        return review(snapshot, ctx, cfg, model)

    app = FastAPI()
    app.include_router(agent_lab.build_router({}, context_loader=context, reviewer=reviewer, run_dir=tmp_path))
    with TestClient(app) as client:
        html = client.get("/agent-lab").text
        auth_token = re.search(r'name="review-token" content="([^"]+)"', html)[1]
        client.headers.update({"Origin": "http://testserver", "X-JobScan-Review": auth_token})
        assert client.post(RUNNER + "start", json={"apply_profile": "casey", "max_jobs": 2}).status_code == 200
        claim = client.post(RUNNER + "claim", json={"worker_id": "worker-1"}).json()
        assert claim["status"] == "claimed"
        lease = {"lease_id": claim["lease_id"], "token": claim["token"], "worker_id": "worker-1"}
        body = {"job_id": claim["job"]["job_id"], "apply_profile": "casey", "demo": False,
                "runner_lease": lease, "submit_authorized": True,
                "snapshot": {"url": claim["job"]["url"], "document_id": "document-1", "coverage": "complete",
                             "fields": [{"id": "first", "label": "First name", "kind": "text",
                                         "value": "Avery", "required": True}]}}
        yield SimpleNamespace(client=client, claim=claim, lease=lease, body=body, calls=calls,
                              directory=tmp_path, auth_token=auth_token)


def confirmed_run(state):
    response = state.client.post(PREFIX + "review", json=state.body)
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "awaiting_confirmation", response.text
    response = state.client.post(PREFIX + "confirm", json={**state.body, "run_id": run["id"]})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("endpoint", ["review", "confirm", "submit-permit"])
@pytest.mark.parametrize("terminal", ["finished", "expired"])
def test_terminal_runner_lease_blocks_review_confirm_and_permit(leased_client, monkeypatch, endpoint, terminal):
    state = leased_client
    run = confirmed_run(state)
    if terminal == "finished":
        response = state.client.post(RUNNER + "finish", json={**state.lease, "outcome": "unknown",
                                                              "evidence": "Worker stopped after timeout."})
        assert response.status_code == 200
    else:
        monkeypatch.setattr(agent_runner.time, "time", lambda: state.claim["expires_at"] + 1)
    response = state.client.post(PREFIX + endpoint, json={**state.body, "run_id": run["id"]})
    assert response.status_code == 409, response.text
    assert state.calls == [state.body["job_id"]]
    assert not list(state.directory.glob("submission-*.json"))


@pytest.mark.parametrize("endpoint", ["review", "confirm", "submit-permit", "repair-plan"])
def test_existing_run_cannot_omit_its_lease(leased_client, endpoint):
    state = leased_client
    run = confirmed_run(state)
    body = {**state.body, "run_id": run["id"]}
    body.pop("runner_lease")
    response = state.client.post(PREFIX + endpoint, json=body)
    assert response.status_code == 409 and "领取凭据改变" in response.json()["detail"]
    assert state.calls == [state.body["job_id"]]
    assert not list(state.directory.glob("submission-*.json"))


@pytest.mark.parametrize("endpoint", ["review", "confirm", "submit-permit", "repair-plan"])
def test_existing_run_cannot_swap_to_another_valid_lease(leased_client, endpoint):
    state = leased_client
    run = confirmed_run(state)
    assert state.client.post(RUNNER + "finish", json={**state.lease, "outcome": "blocked",
                                                       "evidence": "Original application stopped."}).status_code == 200
    assert state.client.post(RUNNER + "start", json={"apply_profile": "casey", "max_jobs": 1}).status_code == 200
    second = state.client.post(RUNNER + "claim", json={"worker_id": "worker-1"}).json()
    assert second["status"] == "claimed" and second["job"]["job_id"] != state.body["job_id"]
    lease = {"lease_id": second["lease_id"], "token": second["token"], "worker_id": "worker-1"}
    body = {**state.body, "run_id": run["id"], "runner_lease": lease, "job_id": second["job"]["job_id"],
            "snapshot": {**state.body["snapshot"], "url": second["job"]["url"]}}
    response = state.client.post(PREFIX + endpoint, json=body)
    assert response.status_code == 409 and "领取凭据改变" in response.json()["detail"]
    assert state.calls == [state.body["job_id"]]


def test_runner_lease_is_bound_to_claimed_job_before_review(leased_client):
    state = leased_client
    response = state.client.post(PREFIX + "review", json={**state.body, "job_id": "another-job"})
    assert response.status_code == 409
    assert state.calls == []


def test_bound_run_and_disk_never_expose_bearer_credentials(leased_client):
    state = leased_client
    run = confirmed_run(state)
    response = state.client.post(PREFIX + "submit-permit", json={**state.body, "run_id": run["id"]})
    assert response.status_code == 200 and response.json()["status"] == "submitting"
    assert response.json()["runner_lease_id"] == state.claim["lease_id"]
    public_values = [run, response.json(), state.client.get(RUNNER + "status").json()]
    persisted = "\n".join(path.read_text() for path in state.directory.rglob("*.json"))
    payload = json.dumps(public_values) + persisted
    assert state.auth_token not in payload
    assert state.lease["token"] not in payload
    saved_run = json.loads((state.directory / (run["id"] + ".json")).read_text())
    assert not {"runner_lease", "token", "owner", "snapshot", "context", "receipt_path"} & saved_run.keys()
