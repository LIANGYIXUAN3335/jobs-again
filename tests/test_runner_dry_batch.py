"""The dry-run tool must never reach a real service, and must never say a run passed when nothing was tested.

Playwright, Chromium and the real forms are not used here: a real run costs money and sends her details to an employer's
Ashby servers. drive() runs against a fake browser and a stub service, and the child's safety is checked on the real app.
"""
import http.client
import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools import runner_dry_batch as tool


ROOT = Path(tool.__file__).resolve().parent.parent
GRAPHQL = "https://jobs.ashbyhq.com/api/non-user-graphql"
REAL_POPEN = subprocess.Popen   # The fixture below replaces it for most tests.


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def stub_service():
    """A local service answering each path from a dict. A callable gives (status, body)."""
    servers = []

    def run(routes):
        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self):
                reply = routes.get(self.path, (404, {"detail": "not found"}))
                status, body = reply() if callable(reply) else reply
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = answer

            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        servers.append(server)
        return server.server_address[1]
    yield run
    for server in servers:
        server.shutdown()
        server.server_close()


class FakeChild:
    def __init__(self, log=None):
        self.returncode, self.hangs, self.log = None, False, log if log is not None else []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.log.append("terminate")

    def kill(self):
        self.log.append("kill")
        self.hangs = False

    def wait(self, timeout=None):
        self.log.append("wait")
        if self.hangs:
            raise subprocess.TimeoutExpired("child", timeout)
        return 0


@pytest.fixture
def isolated_main(monkeypatch):
    """main() with no real data copied, no process started and drive() only recording. Signal handlers are put back after."""
    log = []
    calls = types.SimpleNamespace(drive=[], popen=[], copied=[], serve=[], tmp=[], log=log, child=FakeChild(log))
    real_mkdtemp = tool.tempfile.mkdtemp

    def mkdtemp(**options):
        calls.tmp.append(Path(real_mkdtemp(**options)))
        return str(calls.tmp[-1])

    def popen(args, env=None):
        calls.popen.append((args, env))
        return calls.child

    def rmtree(path, ignore_errors=False):
        calls.log.append("rmtree")
        real_rmtree(path, ignore_errors=ignore_errors)
    real_rmtree = tool.shutil.rmtree
    monkeypatch.setattr(tool.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(tool.subprocess, "Popen", popen)
    monkeypatch.setattr(tool.shutil, "rmtree", rmtree)
    monkeypatch.setattr(tool, "copy_real_data", lambda tmp: calls.copied.append(tmp))
    monkeypatch.setattr(tool, "serve", lambda tmp, port: calls.serve.append(port))
    monkeypatch.setattr(tool, "drive", lambda tmp, port, count: calls.drive.append(port) or 0)
    saved = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    yield calls
    for signum, handler in saved.items():
        signal.signal(signum, handler)


def run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["runner_dry_batch.py", *args])
    return tool.main()


@pytest.mark.parametrize("port", ["8787", "8788"])
@pytest.mark.parametrize("extra", [[], ["--serve", "/nowhere"]])
def test_a_real_service_port_is_refused_before_anything_starts(monkeypatch, isolated_main, port, extra):
    assert run_main(monkeypatch, "1", "--port", port, *extra) != 0
    calls = isolated_main
    assert not calls.copied and not calls.popen and not calls.serve and not calls.drive


def test_a_port_that_already_answers_is_refused_before_anything_starts(monkeypatch, isolated_main):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert run_main(monkeypatch, "1", "--port", str(listener.getsockname()[1])) != 0
    calls = isolated_main
    assert not calls.copied and not calls.popen and not calls.drive


def test_something_on_the_port_that_does_not_speak_http_gets_the_same_refusal(monkeypatch):
    class Banner:
        def poll(self):
            return None

    def urlopen(*_a, **_k):
        raise http.client.BadStatusLine("SSH-2.0-OpenSSH")
    monkeypatch.setattr(tool.urllib.request, "urlopen", urlopen)
    assert "something other than this run's app" in tool.child_problem(Banner(), 18787, "this-run", tries=2)


def test_the_browser_profile_is_gone_even_when_a_dying_browser_writes_it_back(monkeypatch):
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        Path(folder).mkdir()   # What Chromium does while it is being killed.
        (Path(folder) / "Local State").write_text("{}")
    monkeypatch.setattr(tool.time, "sleep", sleep)
    with tool._profile_folder() as folder:
        assert Path(folder).is_dir()
    assert sleeps == [1.0] and not Path(folder).exists()


def test_the_browser_stops_before_its_profile_is_removed():
    source = (ROOT / "tools" / "runner_dry_batch.py").read_text(encoding="utf-8")
    assert "with _profile_folder() as profile, sync_playwright() as p:" in source


def test_port_answers_tells_a_taken_port_from_a_free_one():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert tool.port_answers(port) is True
    assert tool.port_answers(port) is False


@pytest.mark.parametrize("reply", [
    (200, {"nonce": "someone-else"}),
    (200, {"status": "ok"}),
    (200, ["not", "a", "dict"]),
    (200, "<html>the real board</html>"),
    (404, {"detail": "Not Found"}),
    (403, {"detail": "no"}),
])
def test_a_reply_without_this_runs_nonce_is_never_driven(monkeypatch, isolated_main, stub_service, reply):
    # The port was free when checked, and then something else took it before the child could.
    port = stub_service({tool.NONCE_PATH: reply})
    monkeypatch.setattr(tool, "port_answers", lambda _: False)
    assert run_main(monkeypatch, "1", "--port", str(port)) != 0
    calls = isolated_main
    assert not calls.drive
    assert calls.log == ["terminate", "wait", "rmtree"] and not calls.tmp[0].exists()


def test_the_matching_nonce_is_driven_and_was_handed_to_the_child(monkeypatch, isolated_main, stub_service):
    monkeypatch.setattr(tool.secrets, "token_hex", lambda _: "this-run")
    port = stub_service({tool.NONCE_PATH: (200, {"nonce": "this-run"})})
    monkeypatch.setattr(tool, "port_answers", lambda _: False)
    assert run_main(monkeypatch, "1", "--port", str(port)) == 0
    calls = isolated_main
    assert calls.drive == [port]
    assert calls.popen[0][1][tool.NONCE_ENV] == "this-run"
    assert calls.log == ["terminate", "wait", "rmtree"]


def test_a_child_that_died_is_not_waited_for_or_driven(monkeypatch, isolated_main):
    calls = isolated_main
    calls.child.returncode = 1
    monkeypatch.setattr(tool.time, "sleep", lambda _: pytest.fail("It should have seen the exit at once"))
    assert run_main(monkeypatch, "1", "--port", str(free_port())) != 0
    assert not calls.drive


def test_a_child_that_never_answers_is_not_driven(monkeypatch):
    monkeypatch.setattr(tool.time, "sleep", lambda _: None)
    assert "did not answer" in tool.child_problem(FakeChild(), free_port(), "this-run", tries=2)


def test_a_child_that_will_not_stop_is_killed_before_its_folder_is_deleted(monkeypatch, isolated_main):
    calls = isolated_main
    calls.child.hangs = True
    calls.child.returncode = 1
    run_main(monkeypatch, "1", "--port", str(free_port()))
    assert calls.log == ["terminate", "wait", "kill", "wait", "rmtree"]


def test_a_failed_copy_removes_the_temp_folder_with_no_child_to_stop(monkeypatch, isolated_main):
    def fail(tmp):
        raise OSError("disk full")
    monkeypatch.setattr(tool, "copy_real_data", fail)
    with pytest.raises(OSError):
        run_main(monkeypatch, "1", "--port", str(free_port()))
    calls = isolated_main
    assert calls.log == ["rmtree"] and not calls.tmp[0].exists() and not calls.popen


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_still_stops_the_child_and_removes_the_copies(monkeypatch, isolated_main, stub_service, signum):
    # With no handler from main(), the signal lands here instead of killing the test run.
    signal.signal(signum, lambda *_: pytest.fail("main() left this signal to skip its cleanup"))
    calls = isolated_main
    monkeypatch.setattr(tool.secrets, "token_hex", lambda _: "this-run")
    port = stub_service({tool.NONCE_PATH: (200, {"nonce": "this-run"})})
    monkeypatch.setattr(tool, "port_answers", lambda _: False)
    monkeypatch.setattr(tool, "drive", lambda *_: signal.raise_signal(signum))
    with pytest.raises(SystemExit) as stopped:
        run_main(monkeypatch, "1", "--port", str(port))
    assert stopped.value.code == 128 + signum
    assert calls.log == ["terminate", "wait", "rmtree"] and not calls.tmp[0].exists()


def test_a_real_child_echoes_the_nonce_refuses_the_permit_and_is_gone_afterwards(monkeypatch, isolated_main):
    """The whole handshake on the real web app, on a free port, against an empty temp folder. No browser and no model."""
    port = free_port()
    calls = isolated_main
    seen = {}

    def drive(tmp, driven_port, count):
        request = tool.urllib.request.Request(f"http://127.0.0.1:{driven_port}/api/agent-lab/submit-permit", data=b"{}",
                                              headers={"Content-Type": "application/json"})
        with pytest.raises(tool.urllib.error.HTTPError) as refused:
            tool.urllib.request.urlopen(request, timeout=10)
        seen.update(status=refused.value.code, detail=json.loads(refused.value.read().decode())["detail"], up=tool.port_answers(driven_port))
        return 0
    monkeypatch.setattr(tool.subprocess, "Popen", REAL_POPEN)
    monkeypatch.setattr(tool, "drive", drive)
    assert run_main(monkeypatch, "1", "--port", str(port)) == 0
    assert seen == {"status": 409, "detail": "试跑：不发放提交许可，不点提交。", "up": True}
    assert tool.port_answers(port) is False
    assert not calls.tmp[0].exists()


def test_serve_with_no_nonce_refuses_to_start(tmp_path):
    env = {key: value for key, value in os.environ.items() if key != tool.NONCE_ENV}
    finished = subprocess.run([sys.executable, str(ROOT / "tools" / "runner_dry_batch.py"), "--serve", str(tmp_path), "--port", str(free_port())],
                              env=env, capture_output=True, text=True, timeout=30)
    assert finished.returncode != 0 and "nonce" in finished.stderr


def test_the_real_app_is_guarded_and_points_at_no_real_token(tmp_path):
    """In a process of its own: serve() replaces config and module paths for good, which would leak into every other test."""
    script = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import uvicorn
seen = {}
uvicorn.run = lambda app, **options: seen.update(options, app=app)
from tools import runner_dry_batch as tool
tool.serve(Path(sys.argv[1]), 1)
from fastapi.testclient import TestClient
from app import config as cfgmod, gcal, google_auth, interview_ledger
client = TestClient(seen["app"])
legacy, workspace = cfgmod.resolve_path(gcal.TOKEN_PATH), google_auth.token_path(cfgmod.load_config())
# Only once both paths are known to be temp ones: on a broken serve() this call could refresh the real token.
safe = Path(legacy).is_relative_to(sys.argv[1]) and Path(workspace).is_relative_to(sys.argv[1])
print(json.dumps({"legacy": str(legacy), "workspace": str(workspace), "calendar": interview_ledger.refresh_calendar(cfgmod.load_config()) if safe else None,
                  "graceful": seen.get("timeout_graceful_shutdown"), "host": seen.get("host"),
                  "nonce": client.get(tool.NONCE_PATH).json(), "permit": client.post("/api/agent-lab/submit-permit", json={}).status_code,
                  "code": client.get("/api/email-code").json()}))
"""
    env = {**os.environ, tool.NONCE_ENV: "this-run"}
    finished = subprocess.run([sys.executable, "-c", script, str(tmp_path), str(ROOT)], env=env, capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert finished.returncode == 0, finished.stderr[-2000:]
    seen = json.loads(finished.stdout.strip().splitlines()[-1])
    assert Path(seen["legacy"]).is_relative_to(tmp_path) and Path(seen["workspace"]).is_relative_to(tmp_path)
    assert seen["calendar"]["ok"] is False   # No token to find, so the first press of Start cannot reach Google.
    assert seen["graceful"] == 5 and seen["host"] == "127.0.0.1"
    assert seen["nonce"] == {"nonce": "this-run"} and seen["permit"] == 409 and seen["code"] == {"code": None}


def test_the_guard_passes_everything_else_through():
    app = FastAPI()
    app.get("/agent-lab/runner")(lambda: {"page": "board"})
    tool.refuse_submission(app, "this-run")
    client = TestClient(app)
    assert client.get("/agent-lab/runner").json() == {"page": "board"}
    assert client.get(tool.NONCE_PATH).json() == {"nonce": "this-run"}


@pytest.mark.parametrize("operation", ["ApiAutocompleteGeoLocation", "ApiCreateFileUploadHandle", "ApiJobPosting",
                                       "ApiOrganizationFromHostedJobsPageName", "ApiSetFormValue", "ApiSetFormValueToFile"])
def test_the_operations_seen_on_real_forms_pass(operation):
    body = json.dumps({"operationName": operation, "variables": {"path": "_systemfield_name", "value": "x"}, "query": "mutation " + operation})
    assert tool.names_submit(f"{GRAPHQL}?op={operation}", body) is False


@pytest.mark.parametrize("url, body", [
    (f"{GRAPHQL}?op=ApiSubmitSingleApplicationFormAction", None),
    (f"{GRAPHQL}?op=apisubmitapplication", None),
    (f"{GRAPHQL}?op=Api%53UBMIT", None),
    ("https://jobs.ashbyhq.com/api/application/submit", None),
    (GRAPHQL, json.dumps({"operationName": "applicationSUBMIT"})),
    (GRAPHQL, json.dumps({"variables": {"filler": "x" * 500}, "operationName": "ApiSubmitForm"})),
    (GRAPHQL, json.dumps([{"operationName": "ApiSetFormValue"}, {"operationName": "ApiSubmitForm"}])),
    (GRAPHQL, 'not json, but it says submit near the start'),
])
def test_any_spelling_of_submit_is_caught(url, body):
    assert tool.names_submit(url, body) is True


class FakePage:
    def __init__(self, url="chrome-extension://abc/runner.html", fields=None, closes=False):
        self.url, self.fields, self.closes = url, fields, closes

    def goto(self, url):
        pass

    def evaluate(self, script):
        if self.closes:
            raise RuntimeError("Target page, context or browser has been closed")
        return self.fields if script == tool.FIELDS else None

    def fill(self, *args):
        pass

    def click(self, *args):
        pass

    def reload(self):
        pass

    def wait_for_timeout(self, ms):
        pass


class FakeContext:
    def __init__(self, pages):
        self.pages, self.guard, self.service_workers = list(pages), None, [types.SimpleNamespace(url="chrome-extension://abc/background.js")]

    def route(self, pattern, handler):
        self.guard = handler

    def new_page(self):
        return FakePage()

    def close(self):
        pass


def job_record(name, status="blocked", evidence="试跑：不发放提交许可，不点提交。"):
    return {"status": status, "evidence": evidence,
            "job": {"job_id": name, "company": name, "title": "Engineer", "url": f"https://jobs.ashbyhq.com/{name}/1/application"}}


@pytest.fixture
def run_drive(monkeypatch, stub_service, tmp_path):
    """drive() on a fake browser and a stub service. `states` are the status replies, last one repeated."""
    def run(states, started=200, pages=(), count=1, clock=None):
        states = list(states)
        routes = {"/agent-lab/runner": (200, '<meta name="review-token" content="t">'),
                  "/api/agent-lab/pair-code": (200, {"code": "c"}),
                  "/api/agent-lab/runner/start": (started, {"detail": "no"} if started != 200 else {}),
                  "/api/agent-lab/runner/status": lambda: (200, states.pop(0) if len(states) > 1 else states[0])}
        context = FakeContext(pages)
        browser = types.SimpleNamespace(chromium=types.SimpleNamespace(launch_persistent_context=lambda *a, **k: context))

        class Playwright:
            def __enter__(self):
                return browser

            def __exit__(self, *args):
                return False
        monkeypatch.setitem(sys.modules, "playwright.sync_api", types.SimpleNamespace(sync_playwright=Playwright))
        if clock:
            monkeypatch.setattr(tool, "time", types.SimpleNamespace(time=clock, sleep=lambda _: None))
        folder = tmp_path / f"run-{len(list(tmp_path.iterdir()))}"
        folder.mkdir()
        return tool.drive(folder, stub_service(routes), count), context
    return run


def test_exit_is_non_zero_when_the_start_was_refused(run_drive, capsys):
    # runner_dry_batch.py 25: above the batch limit, so the service says 422 and nothing runs.
    code, _ = run_drive([{"enabled": False, "jobs": []}], started=422, count=25)
    assert code != 0 and "422" in capsys.readouterr().out


def test_exit_is_non_zero_when_no_form_was_seen(run_drive, capsys):
    code, _ = run_drive([{"enabled": False, "jobs": []}])
    assert code != 0 and "Nothing was tested" in capsys.readouterr().out


def test_exit_is_non_zero_and_names_the_form_that_missed_the_permit(run_drive, capsys):
    states = [{"enabled": False, "jobs": [job_record("Good"), job_record("Stuck", "needs_user", "有一道题要你回答。")]}]
    code, _ = run_drive(states, count=2)
    out = capsys.readouterr().out
    assert code != 0 and "1 of 2 forms" in out
    assert "did not reach the permit: Stuck · Engineer" in out and "did not reach the permit: Good" not in out


def test_exit_is_non_zero_when_a_form_was_still_running_at_the_deadline(run_drive, capsys):
    ticks = iter([0, 1, 10**6])
    state = {"enabled": True, "active": {"job": {}}, "jobs": [job_record("Good"), job_record("Slow", "claimed", "")]}
    code, _ = run_drive([state], count=2, clock=lambda: next(ticks))
    out = capsys.readouterr().out
    assert code != 0 and "did not reach the permit: Slow · Engineer" in out


def test_exit_is_zero_only_when_every_form_reached_the_permit(run_drive, capsys):
    code, _ = run_drive([{"enabled": False, "jobs": [job_record("Good"), job_record("Fine")]}], count=2)
    assert code == 0 and "2 of 2 forms" in capsys.readouterr().out


def test_a_tab_closing_while_its_fields_are_read_does_not_end_the_run(run_drive, capsys):
    pages = [FakePage("https://jobs.ashbyhq.com/Gone/1/application", closes=True),
             FakePage("https://jobs.ashbyhq.com/Next/1/application", fields=[{"label": "Name", "value": "Casey"}])]
    states = [{"enabled": True, "jobs": [job_record("Gone")]}, {"enabled": False, "jobs": [job_record("Gone"), job_record("Next")]}]
    code, _ = run_drive(states, pages=pages, count=2)
    out = capsys.readouterr().out
    assert code == 0 and "Name => Casey" in out and "2 of 2 forms" in out


def test_the_guard_aborts_a_lower_case_submit_and_the_run_then_fails(run_drive, capsys):
    routes = []

    class Route:
        def __init__(self, url, body=None, binary=False):
            self.url, self.body, self.binary = url, body, binary

        @property
        def request(self):
            return self

        @property
        def post_data(self):
            if self.binary:
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "binary")
            return self.body

        def abort(self):
            routes.append(("abort", self.url))

        def continue_(self):
            routes.append(("continue", self.url))
    code, context = run_drive([{"enabled": False, "jobs": [job_record("Good")]}])
    assert code == 0
    upload = Route(f"{GRAPHQL}?op=ApiSetFormValueToFile", binary=True)
    context.guard(upload)
    context.guard(Route(f"{GRAPHQL}?op=apisubmitapplication"))
    assert routes == [("continue", upload.url), ("abort", f"{GRAPHQL}?op=apisubmitapplication")]
    capsys.readouterr()
    assert tool.report(200, {"Good": True}, [f"{GRAPHQL}?op=apisubmitapplication"]) != 0
    assert "op=apisubmitapplication" in capsys.readouterr().out
