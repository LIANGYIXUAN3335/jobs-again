#!/usr/bin/env python
"""A real batch on real public Ashby forms with submission impossible. It is only for checking a real change to Filler, Reviewer or the runner.

Tests on fake pages cannot see what the real forms do: on 2026-11-06 the scratch scripts this tool was written from found the
model leaving "issues" and a field out of its reply, which no fake page shows. It runs the real web app, the real runner page
and the real models, so each form costs about one Reviewer call (roughly $0.10).

What keeps it from applying anywhere or writing real data:
- It refuses the real service ports (8787, 8788) and any port that already answers. It drives an app only after that app has
  echoed a random nonce this run gave it, so it can only talk to the child it started.
- That child runs on a port of its own against temp copies of the databases and answer files. Its calendar token paths point
  into the temp folder, so it reads no real calendar and cannot rewrite a real token.
- That child refuses every submit permit, and Filler cannot press Submit without one.
- Any Ashby request naming Submit, in any spelling, is also aborted at the network, and so is any Greenhouse write to its
  job_app page or an applications endpoint. The output counts them; it should say 0.

What it cannot hold back: filling a form sends her name, email, phone, LinkedIn and tailored resume to the employer's Ashby
servers, as a person's half-finished form does. No application is created, but whether the employer can see that draft is
unknown. That is why this is only for checking a real change, never a routine run.

It exits non-zero unless the batch started, at least one form ran, every form reached the refused permit and nothing was aborted.

    .venv/bin/python tools/runner_dry_batch.py 5
    .venv/bin/python tools/runner_dry_batch.py 3 --board greenhouse
"""
import argparse
import contextlib
import http.client
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
REAL_SERVICE_PORTS = {8787, 8788}
NONCE_ENV = "JOBSCAN_DRY_BATCH_NONCE"
NONCE_PATH = "/__dry_batch_nonce"
HOSTS_ENV = "JOBSCAN_DRY_BATCH_HOSTS"
BOARDS = {"ashby": "jobs.ashbyhq.com", "greenhouse": "job-boards.greenhouse.io,boards.greenhouse.io"}
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)

FIELDS = """() => [...document.querySelectorAll('[class*="_fieldEntry"], .ashby-application-form-field-entry')].filter(e => e.offsetParent).map(e => {
  const label = (e.querySelector('label') || {}).innerText || '';
  const inputs = [...e.querySelectorAll('input:not([type=file]):not([type=radio]):not([type=checkbox]), textarea, select')].filter(i => i.offsetParent).map(i => i.tagName === 'SELECT' ? (i.selectedOptions[0] || {}).text : i.value);
  const ticked = [...e.querySelectorAll('input[type=radio]:checked, input[type=checkbox]:checked')].map(r => (r.closest('label') || r.parentElement).innerText.trim());
  const buttons = [...e.querySelectorAll('[class*="_active"], [aria-pressed=true]')].map(b => b.innerText.trim());
  const files = [...e.querySelectorAll('input[type=file]')].flatMap(f => [...f.files].map(x => x.name));
  return {label: label.trim().slice(0, 80), value: [...inputs, ...ticked, ...buttons, ...files].filter(Boolean).join(' | ').slice(0, 90)};
})"""


def copy_real_data(tmp: Path) -> None:
    (tmp / "lab").mkdir(parents=True)
    (tmp / "resumes").mkdir()
    for name in ("jobscan.db", "user_state.db"):
        source = sqlite3.connect(f"file:{ROOT}/data/{name}?mode=ro", uri=True)
        target = sqlite3.connect(tmp / name)
        source.backup(target)
        target.close()
        source.close()
    for stem in ("calendar_events", "confirmed_answers", "form_preflight", "interview_overrides", "runner_settings"):
        for path in (ROOT / "data" / "agent_lab").glob(f"{stem}.*.json"):
            shutil.copy(path, tmp / "lab" / path.name)
    # Rendering a claim reads the role's tailored YAML from the resumes folder and writes the PDF beside it.
    for path in (ROOT / "output" / "resumes").glob("*.yaml"):
        (tmp / "resumes" / path.name).symlink_to(path)


def refuse_submission(app, nonce: str) -> None:
    """No permit and no email code, and the nonce that proves this app is the child the tool started."""
    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def no_submission(request, call_next):
        if request.url.path == NONCE_PATH:
            return JSONResponse({"nonce": nonce})
        if "submit-permit" in request.url.path:
            return JSONResponse({"detail": "试跑：不发放提交许可，不点提交。"}, status_code=409)
        if request.url.path == "/api/email-code":
            return JSONResponse({"code": None})
        return await call_next(request)


def serve(tmp: Path, port: int) -> None:
    nonce = os.environ.get(NONCE_ENV)
    if not nonce:
        raise SystemExit("--serve is only for this tool: with no nonce, nothing could prove who is answering on the port.")
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    os.environ["JOBSCAN_QUEUE_RUNNER"] = "0"
    from app import config as cfgmod
    real_load = cfgmod.load_config

    def load(path=None):
        cfg = dict(real_load(path))
        cfg.update(db_path=str(tmp / "jobscan.db"), user_db_path=str(tmp / "user_state.db"), answers_log=str(tmp / "answers.jsonl"),
                   agent_runner={"preflight": True, "keep_awake": False},
                   google={**(cfg.get("google") or {}), "token": str(tmp / "google_token.json")})
        cfg["output_dirs"] = {**cfg["output_dirs"], "resumes": str(tmp / "resumes")}
        return cfg
    cfgmod.load_config = load

    from app import agent_lab, agent_runner, ai, confirmed_answers, form_preflight, gcal, interview_ledger, login_vault
    for module in (form_preflight, interview_ledger, login_vault, confirmed_answers):
        module._root = tmp / "lab"
    ai.USAGE_LOG = str(tmp / "model_usage.jsonl")
    # The calendar copies are already here. With no token to find, /start cannot read Google or rewrite a real token file.
    gcal.TOKEN_PATH = str(tmp / "token_gcal.json")
    agent_runner.MAX_STOPS_IN_A_ROW = 10**6   # Every form ends on a refused permit here; a real batch pauses after 3 stops.
    hosts = os.environ.get(HOSTS_ENV)
    if hosts:
        agent_lab.RUNNER_HOSTS = set(hosts.split(","))
    real_build = agent_lab.build_router
    agent_lab.build_router = lambda cfg, context_loader=None, *a, **k: real_build(cfg, context_loader, run_dir=tmp / "agent_lab")

    import uvicorn
    from app import web

    refuse_submission(web.app, nonce)
    # A review can run for minutes, and the parent is about to delete the folder this writes to.
    uvicorn.run(web.app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=5)


def names_submit(url: str, body: str | None) -> bool:
    """Whether a request names a submission, in any spelling. The real request has never been seen, so this casts wide.

    The URL's op parameter and the body's operationName are read as well as the plain text, which misses a percent-encoded
    op or an operationName past the first 400 characters. A false match only aborts one write, and the run then fails.
    """
    names = parse_qs(urlsplit(url).query).get("op", [])
    try:
        parsed = json.loads(body or "null")
    except ValueError:
        parsed = None
    # A GraphQL client can send several operations in one request.
    for operation in parsed if isinstance(parsed, list) else [parsed]:
        if isinstance(operation, dict):
            names.append(str(operation.get("operationName") or ""))
    return "submit" in " ".join([url, (body or "")[:400], *names]).lower()


def print_fields(pages, job_url: str) -> None:
    for page in pages:
        if job_url.split("/application")[0] in page.url:
            try:
                fields = page.evaluate(FIELDS)
            except Exception:  # The runner page closes the tab 800 ms after a definite outcome, and that must not end the run.
                print("  (the tab closed before its fields could be read)", flush=True)
                continue
            for field in fields:
                print(f"  {field['label']} => {field['value']}", flush=True)


def report(started: int, forms: dict[str, bool], aborted: list[str]) -> int:
    """Non-zero unless the batch started, at least one form ran, every one reached the refused permit and nothing was aborted."""
    missed = [name for name, reached in forms.items() if not reached]
    print(f"\n{len(forms) - len(missed)} of {len(forms)} forms passed review and reached the permit it refused. Submit requests aborted: {len(aborted)}.")
    for name in missed:
        print(f"  did not reach the permit: {name}")
    for url in aborted:
        print(f"  aborted: {url}")
    if started != 200:
        print(f"Nothing ran: start returned {started}.")
    elif not forms:
        print("Nothing was tested: no form was claimed.")
    return 0 if started == 200 and forms and not missed and not aborted else 1


def drive(tmp: Path, port: int, count: int) -> int:
    from playwright.sync_api import sync_playwright
    base = f"http://127.0.0.1:{port}"
    extension = tmp / "extension"
    shutil.copytree(ROOT / "extension", extension)
    for name in ("runner.js", "manifest.json"):
        path = extension / name
        path.write_text(path.read_text().replace("8787", str(port)))

    def call(path, body=None, headers=None):
        request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode() or "null")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()[:500]

    aborted, forms = [], {}
    with _profile_folder() as profile, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile, channel="chromium", headless=True, viewport={"width": 1400, "height": 1500},
                                                        args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"])

        def guard(route):
            request = route.request
            try:
                body = request.post_data
            except Exception:  # A binary upload body has no text to read.
                body = None
            if names_submit(request.url, body):
                aborted.append(request.url)
                route.abort()
            else:
                route.continue_()

        def guard_greenhouse(route):
            request = route.request
            try:
                body = request.post_data
            except Exception:  # A binary upload body has no text to read.
                body = None
            if request.method != "GET" and (re.search(r"job_app|application", urlsplit(request.url).path, re.I) or names_submit(request.url, body)):
                aborted.append(request.url)
                route.abort()
                return
            if request.method != "GET":
                print(f"  (allowed {request.method} {request.url[:120]})", flush=True)
            route.continue_()
        context.route("**/*greenhouse.io/**", guard_greenhouse)
        context.route("**/*ashbyhq.com/**", guard)
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=20000)
        runner = context.new_page()
        runner.goto(f"chrome-extension://{worker.url.split('/')[2]}/runner.html")
        runner.evaluate(f"chrome.storage.local.set({{jobscanPort: {port}, jobscanIdentity: ''}})")
        html = urllib.request.urlopen(base + "/agent-lab/runner", timeout=15).read().decode()
        board = {"X-JobScan-Review": re.search(r'name="review-token" content="([^"]+)"', html).group(1), "Origin": base}
        runner.evaluate("document.getElementById('pair-box').open = true")
        runner.fill("#pair-code", call("/api/agent-lab/pair-code", {}, board)[1]["code"])
        runner.click("#pair")
        runner.wait_for_timeout(1500)
        started = call("/api/agent-lab/runner/start", {"apply_profile": "casey", "max_jobs": count}, board)[0]
        print("start:", started, flush=True)
        jobs = []
        if started == 200:
            runner.reload()
            deadline = time.time() + count * 240
            while time.time() < deadline:
                # A sleep here would stop the route guard above, and every Ashby page would hang.
                runner.wait_for_timeout(2000)
                status, state = call("/api/agent-lab/runner/status", headers=board)
                if status != 200:
                    continue
                jobs = state.get("jobs") or []
                for record in jobs:
                    job = record["job"]
                    name = f"{job['company']} · {job['title']}"
                    if record["status"] == "claimed" or name in forms:
                        continue
                    evidence = record.get("evidence") or ""
                    forms[name] = evidence.startswith("试跑")
                    print(f"\n=== {name}\n{record['status']}: {evidence[:500]}", flush=True)
                    # What Filler wrote and the Reviewer said, so a stop shows where it came from.
                    for event in state.get("events") or []:
                        if event.get("job_id") == job["job_id"] and event.get("role") in {"filler", "reviewer"}:
                            print(f"  [{event['role']}] {event['text'][:700]}", flush=True)
                    print_fields(context.pages, job["url"])
                if not state.get("enabled") and not state.get("active"):
                    break
            # A form still running when time ran out did not reach the permit either.
            for record in jobs:
                if record["status"] == "claimed":
                    forms[f"{record['job']['company']} · {record['job']['title']}"] = False
        context.close()
    return report(started, forms, aborted)


def port_answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


@contextlib.contextmanager
def _profile_folder(wait: float = 1.0):
    """Entered before the browser, so it is removed after the browser stops. One being killed can write the folder back just after it goes."""
    profile = tempfile.mkdtemp(prefix="jobscan-dry-profile-")
    try:
        yield profile
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        time.sleep(wait)
        shutil.rmtree(profile, ignore_errors=True)


def child_problem(server, port: int, nonce: str, tries: int = 60) -> str:
    """Why whatever answers on the port is not the child just started, or "" when it is."""
    for _ in range(tries):
        if server.poll() is not None:
            return f"the app exited with code {server.returncode} before it answered. Is port {port} taken?"
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{NONCE_PATH}", timeout=2) as response:
                echoed = json.loads(response.read().decode()).get("nonce")
        # A banner or anything else that does not speak HTTP lands here too, and it is no more this run's app.
        except (urllib.error.HTTPError, http.client.HTTPException, ValueError, AttributeError):
            return f"something other than this run's app is answering on {port}."
        except OSError:
            time.sleep(1)
            continue
        return "" if echoed == nonce else f"something other than this run's app is answering on {port}."
    return f"the app did not answer on {port} after {tries} tries."


def stop_child(server) -> None:
    """Wait until it is gone: while it drains it can still write to the temp folder, and its handlers recreate what was deleted."""
    if server is None:
        return
    server.terminate()
    try:
        server.wait(10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()


def leave(signum, _frame) -> None:
    """By default these signals skip `finally`, leaving the child on the port and full database copies in the temp folder."""
    for other in STOP_SIGNALS:
        signal.signal(other, signal.SIG_IGN)   # A second signal must not cut that cleanup short.
    raise SystemExit(128 + signum)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("count", type=int, nargs="?", default=3)
    parser.add_argument("--port", type=int, default=18787)
    parser.add_argument("--board", choices=sorted(BOARDS), default="ashby", help="which job board the queue may offer")
    parser.add_argument("--serve", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.port in REAL_SERVICE_PORTS:
        print(f"Refused: {args.port} is a real JobScan service, and this tool must never drive one.", file=sys.stderr)
        return 2
    if args.serve:
        serve(Path(args.serve), args.port)
        return 0
    if port_answers(args.port):
        print(f"Refused: something already answers on {args.port}. It could be a stale copy or a real service. Nothing was started.", file=sys.stderr)
        return 2
    for signum in STOP_SIGNALS:
        signal.signal(signum, leave)
    nonce = secrets.token_hex(16)
    tmp = Path(tempfile.mkdtemp(prefix="jobscan-dry-batch-"))
    server = None
    try:
        copy_real_data(tmp)
        server = subprocess.Popen([sys.executable, __file__, "--serve", str(tmp), "--port", str(args.port)], env={**os.environ, NONCE_ENV: nonce, HOSTS_ENV: BOARDS[args.board]})
        problem = child_problem(server, args.port, nonce)
        if problem:
            print(f"Refused to drive: {problem}", file=sys.stderr)
            return 2
        return drive(tmp, args.port, args.count)
    finally:
        stop_child(server)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
