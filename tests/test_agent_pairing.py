"""Extension pairing. Real Chrome sends no Origin on a GET from an extension with host permission."""
import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent_lab

EXTENSION = "chrome-extension://abc"
STATUS = "/api/agent-lab/runner/status"
PAUSE = "/api/agent-lab/runner/pause"


def client(tmp_path):
    app = FastAPI()
    app.include_router(agent_lab.build_router({}, run_dir=tmp_path))
    return TestClient(app)


def pair(c):
    session = re.search(r'name="review-token" content="([^"]+)"', c.get("/agent-lab").text)[1]
    code = c.post("/api/agent-lab/pair-code", headers={"Origin": "http://testserver", "X-JobScan-Review": session}).json()["code"]
    return c.post("/api/agent-lab/pair", json={"code": code}, headers={"Origin": EXTENSION}).json()["token"]


def test_paired_extension_can_read_without_origin_but_cannot_write_without_it(tmp_path):
    with client(tmp_path) as c:
        token = pair(c)
        secret = {"X-JobScan-Review": token}
        assert c.get(STATUS, headers=secret).status_code == 200
        assert c.get(STATUS, headers={**secret, "Origin": EXTENSION}).status_code == 200
        assert c.get(STATUS, headers={**secret, "Origin": "https://evil.test"}).status_code == 403
        assert c.get(STATUS, headers={"X-JobScan-Review": "wrong"}).status_code == 403
        assert c.get(STATUS).status_code == 403
        assert c.post(PAUSE, json={}, headers=secret).status_code == 403
        assert c.post(PAUSE, json={}, headers={**secret, "Origin": EXTENSION}).status_code == 200


def test_pairing_survives_a_restart_and_only_a_hash_is_stored(tmp_path):
    with client(tmp_path) as c:
        token = pair(c)
    assert token not in (tmp_path / "paired_extensions.json").read_text()
    assert (tmp_path / "paired_extensions.json").stat().st_mode & 0o077 == 0
    with client(tmp_path) as c:
        assert c.get(STATUS, headers={"X-JobScan-Review": token}).status_code == 200
