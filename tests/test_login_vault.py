"""Saved job-site logins: exact host only, nothing on disk but the index, and only the extension gets one out."""
import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent_lab, login_vault as V
from app.agent_review import demo_context

SITE = "https://nvidia.wd5.myworkdayjobs.com/login"
HOST = "nvidia.wd5.myworkdayjobs.com"
SECRET = "fake-not-a-real-password"


@pytest.fixture
def keychain(tmp_path, monkeypatch):
    store = {}
    monkeypatch.setattr(V, "_root", tmp_path)
    monkeypatch.setattr(V.cfgmod, "workspace_name", lambda _cfg: "test")
    monkeypatch.setattr(V, "_keychain_write", lambda service, account, password: store.__setitem__((service, account), password))
    monkeypatch.setattr(V, "_keychain_read", lambda service, account: store.get((service, account)))
    monkeypatch.setattr(V, "_keychain_delete", lambda service, account: store.pop((service, account), None))
    return store


@pytest.mark.parametrize("site", [
    "https://accounts.google.com", "https://www.chase.com/login", "https://evil-myworkdayjobs.com", "https://myworkdayjobs.com.evil.com",
    "http://nvidia.wd5.myworkdayjobs.com", "https://user:sample-17bd9242@example.com", "https://appleid.apple.com", ""])
def test_only_an_https_job_site_can_hold_a_login(site):
    with pytest.raises(ValueError):
        V.site_host(site)


def test_a_url_or_a_bare_host_gives_the_exact_host():
    assert V.site_host(SITE) == HOST and V.site_host(HOST) == HOST


def test_the_password_stays_out_of_every_file_and_only_its_exact_host_gets_it(keychain, tmp_path):
    assert V.save({}, SITE, "me@example.test", SECRET) == HOST
    assert keychain == {("jobscan-login.test", HOST): SECRET}
    assert not any(SECRET in p.read_text() for p in tmp_path.iterdir() if p.is_file())
    assert V.listing({})[0]["username"] == "me@example.test" and "password" not in V.listing({})[0]
    assert V.fill_for({}, HOST) == {"username": "me@example.test", "password": SECRET}
    assert V.fill_for({}, "nvidia.wd1.myworkdayjobs.com") is None
    assert V.fill_for({}, "myworkdayjobs.com") is None


def test_saving_again_replaces_and_removing_clears_both_places(keychain):
    V.save({}, SITE, "old@example.test", "fake-1")
    V.save({}, SITE, "new@example.test", "fake-2")
    assert V.fill_for({}, HOST) == {"username": "new@example.test", "password": "fake-2"}
    V.remove({}, HOST)
    assert keychain == {} and V.listing({}) == [] and V.fill_for({}, HOST) is None
    with pytest.raises(KeyError):
        V.remove({}, HOST)


def test_a_password_with_quotes_goes_through_stdin_and_never_the_command_line(monkeypatch):
    calls = []

    class Done:
        returncode = 0
        stderr = ""
    monkeypatch.setattr(V.subprocess, "run", lambda args, **kw: calls.append((args, kw)) or Done())
    V._keychain_write("svc", "host", 'fa"ke\\pw')
    args, kw = calls[0]
    assert args == [V._SECURITY, "-i"] and 'fa"ke' not in " ".join(args)
    assert kw["input"] == 'add-generic-password -U -s "svc" -a "host" -w "fa\\"ke\\\\pw"\n'


@pytest.fixture
def board(keychain, tmp_path):
    app = FastAPI()
    app.include_router(agent_lab.build_router({}, context_loader=lambda *_: demo_context(), reviewer=lambda *_: {}, run_dir=tmp_path))
    with TestClient(app) as client:
        session = re.search(r'name="review-token" content="([^"]+)"', client.get("/agent-lab").text)[1]
        yield client, {"Origin": "http://testserver", "X-JobScan-Review": session}


def test_the_board_saves_lists_and_deletes_but_never_reads_a_password_back(board):
    client, headers = board
    saved = client.post("/api/agent-lab/logins", json={"site": SITE, "username": "me@example.test", "password": SECRET}, headers=headers)
    assert saved.status_code == 200 and SECRET not in saved.text
    assert SECRET not in client.get("/api/agent-lab/logins", headers=headers).text
    assert client.post("/api/agent-lab/logins/fill", json={"host": HOST}, headers=headers).status_code == 403
    assert client.delete(f"/api/agent-lab/logins/{HOST}", headers=headers).status_code == 200


def test_a_site_outside_the_list_is_refused_with_a_reason(board):
    client, headers = board
    refused = client.post("/api/agent-lab/logins", json={"site": "https://www.chase.com", "username": "a", "password": SECRET}, headers=headers)
    assert refused.status_code == 400 and "招聘网站" in refused.json()["detail"]


def test_the_extension_gets_only_a_login_saved_for_that_host_and_a_page_gets_nothing(board):
    client, headers = board
    client.post("/api/agent-lab/logins", json={"site": SITE, "username": "me@example.test", "password": SECRET}, headers=headers)
    code = client.post("/api/agent-lab/pair-code", json={}, headers=headers).json()["code"]
    extension = "chrome-extension://" + "a" * 32
    token = client.post("/api/agent-lab/pair", json={"code": code}, headers={"Origin": extension}).json()["token"]
    own = {"Origin": extension, "X-JobScan-Review": token}
    assert client.post("/api/agent-lab/logins/status", json={"host": HOST}, headers=own).json() == {"username": "me@example.test"}
    assert client.post("/api/agent-lab/logins/fill", json={"host": HOST}, headers=own).json() == {"username": "me@example.test", "password": SECRET}
    assert client.post("/api/agent-lab/logins/fill", json={"host": "other.myworkdayjobs.com"}, headers=own).status_code == 404
    assert client.post("/api/agent-lab/logins/fill", json={"host": HOST}, headers={"Origin": "https://evil.example", "X-JobScan-Review": token}).status_code == 403
