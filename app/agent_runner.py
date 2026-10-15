"""Durable, bounded dispatch for the local extension's independent agent worker.

This module only leases eligible jobs and records worker reports. It neither clicks
employer buttons nor marks JobScan applications as applied. Candidate eligibility
(including company limits and resume readiness) is supplied by the host service.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import config as cfgmod


LEASE_SECONDS = 15 * 60
WORKER_ONLINE_SECONDS = 90
PREFIX = "/api/agent-lab/runner"
MAX_BATCH = 20
MAX_STOPS_IN_A_ROW = 3
SAME_COMPANY_GAP_DAYS = 30
AWAKE_SECONDS = 2 * 60 * 60
# Stopped before Submit, so asking for it again cannot send a second application. "unknown" can.
RETRYABLE = {"needs_user", "blocked", "error"}
# The runner page arms itself within 3 minutes of the press, then asks at least once a minute even when hidden.
# This long with no page asking means none will.
RETRY_WAIT_SECONDS = 5 * 60


class StartRequest(BaseModel):
    apply_profile: Literal["casey"]
    max_jobs: int = Field(default=1, ge=1, le=MAX_BATCH, strict=True)


class RetryRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=200)


class ClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)


class LeaseRequest(ClaimRequest):
    lease_id: str = Field(min_length=1, max_length=100)
    token: str = Field(min_length=1, max_length=200)


class EventRequest(LeaseRequest):
    role: Literal["filler", "reviewer", "system"]
    text: str = Field(min_length=1, max_length=2000)
    to: str = Field(default="", max_length=100)


class FinishRequest(LeaseRequest):
    outcome: Literal["submitted", "expired", "needs_user", "blocked", "error", "unknown"]
    evidence: str = Field(min_length=1, max_length=3000)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _candidate(value) -> dict | None:
    """Recheck the mutable loader output; never fix identity or tailor a resume."""
    if not isinstance(value, dict):
        return None
    if (value.get("apply_profile") != "casey" or
            value.get("resume_ready") is not True or value.get("status") != "new"):
        return None
    limits = {"job_id": 200, "company": 300, "title": 500, "url": 3000}
    for key, limit in limits.items():
        item = value.get(key)
        if not isinstance(item, str) or not item.strip() or len(item) > limit:
            return None
    try:
        url = urlsplit(value["url"])
        if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
            return None
    except ValueError:
        return None
    result = {key: value[key] for key in (*limits, "apply_profile", "resume_ready", "status")}
    reason = value.get("selection_reason")
    if isinstance(reason, str) and reason.strip():
        result["selection_reason"] = reason.strip()[:1000]
    return result


SPAM_FLAG = re.compile(r"possible spam|too many applications", re.I)


class _PostingChanged(Exception):
    """Preparation handed back a different posting than the one leased."""


def build_runner_router(cfg: dict, auth, candidate_loader=None, run_dir=None,
                        terminal_callback=None, prepare_claim=None,
                        expected_extension_version="", never_clicked=None, runner_url="",
                        one_per_company=False, stay_awake=False, on_start=None, notify=None, reading=None, clicked_roles=None) -> APIRouter:
    """Build routes with synchronous ``auth(request)`` and ``candidate_loader()``.

    ``auth`` returns a stable authenticated owner string. ``candidate_loader``
    returns an iterable of currently eligible job dictionaries; the runner also
    checks their identity, ready flag, status and URL immediately before leasing.
    The service remains disabled until an authenticated, explicit start request.
    Optional ``terminal_callback(job, outcome, evidence)`` records a local terminal
    change before acknowledging finish. It is attempted at most once per lease,
    including across crashes; it must never perform employer submissions.
    Optional ``prepare_claim(job)`` runs only after durable reservation and before
    returning a browser-open payload. It may prepare local identity/resume state,
    returning None or a ready candidate with the same job ID and URL. It is never
    replayed automatically; failures pause the queue before any browser action.
    Optional ``never_clicked(job_id)`` proves Submit was never clicked for a job. Without
    that proof the owner cannot ask for a job in the ledger to run again.
    """
    router = APIRouter()
    directory = Path(run_dir or cfgmod.ROOT / "data" / "agent_lab")
    state_path = directory / "runner_state.json"
    event_path = directory / "runner_events.jsonl"
    thread_lock = threading.RLock()
    # Liveness only, deliberately not durable: a restart must not report a page it has not heard from.
    worker = {"seen": 0.0, "version": "", "identity": ""}

    def fresh_state():
        return {"version": 1, "enabled": False, "apply_profile": "casey",
                "batch_id": None, "max_jobs": 1, "claimed_count": 0,
                "submitted_count": 0, "active_job_id": None, "jobs": {},
                "pause_reason": "not_started", "updated_at": time.time()}

    def load():
        if not state_path.exists():
            return fresh_state()
        try:
            state = json.loads(state_path.read_text())
            if (state["version"] != 1 or not isinstance(state["jobs"], dict) or
                    state["apply_profile"] != "casey" or
                    not isinstance(state["enabled"], bool) or
                    state["claimed_count"] < 0 or state["submitted_count"] < 0 or
                    not 1 <= state["max_jobs"] <= MAX_BATCH):
                raise ValueError("Invalid runner state")
            if state["active_job_id"] is not None:
                record = state["jobs"][state["active_job_id"]]
                if record["status"] != "claimed" or not isinstance(record["expires_at"], (int, float)):
                    raise ValueError("Invalid active lease")
            return state
        except (ValueError, KeyError, TypeError, OSError):
            # Resetting a damaged ledger could resubmit an already claimed job.
            raise HTTPException(503, "Runner ledger unavailable; inspect it before resuming.") from None

    awake = []

    def refused(outcome, evidence, job_id):
        """The employer's answer to a real click. With no permit on file nothing was sent, so nothing was refused."""
        return (outcome == "blocked" and (evidence or "").startswith("雇主拒收") and
                not (never_clicked and never_clicked(job_id)))

    def may_have_reached_employer(record):
        """An unclear result leaves the role new with no applied date, so only this ledger remembers it.

        A click that then stopped (a captcha after Submit) counts too. After the gap the company is free again,
        the same as for a role with an applied date.
        """
        if time.time() - (record.get("finished_at") or time.time()) > SAME_COMPANY_GAP_DAYS * 86400:
            return False
        return (record["status"] in {"submitted", "unknown"} or
                bool(never_clicked and not never_clicked(record["job"]["job_id"])))

    def clicked_before(job_id):
        """A receipt on file with the role still new: nobody has settled what that click did, so it must not run again."""
        return bool(never_clicked and not never_clicked(job_id))

    def eligible():
        return [job for value in (candidate_loader() if candidate_loader else []) if (job := _candidate(value))]

    def reached_employer(state, candidates):
        """The role, by company, whose application may already be there. A second role at that company has to wait.

        A click this ledger never saw (Fill → Review → Submit on the panel) leaves only its receipt behind.
        """
        jobs = [r["job"] for r in state["jobs"].values() if may_have_reached_employer(r)]
        jobs += [job for job in candidates if clicked_before(job["job_id"])]
        # A click on a role the queue leaves out: preflight would not run it, or its title is not one the queue takes.
        jobs += list(clicked_roles()) if clicked_roles else []
        return {cfgmod.norm_key(job["company"]): job for job in jobs}

    def settled_outcome(job_id, body):
        """An unclear result with no permit on file: nothing was clicked, so nothing can have reached the employer."""
        if body.outcome == "unknown" and never_clicked and never_clicked(job_id):
            return "blocked", ("这份没跑完就断了（申请页一直不在前台，或者被关掉了），从没点过提交。可以重跑。原话：" + body.evidence)[:3000]
        return body.outcome, body.evidence

    def stopped_in_a_row(state):
        batch = sorted((r for r in state["jobs"].values() if r.get("batch_id") == state["batch_id"] and r.get("finished_at")),
                       key=lambda r: r["finished_at"], reverse=True)
        count = 0
        for record in batch:
            if record["status"] in {"submitted", "expired"} or refused(record["status"], record.get("evidence"), record["job"]["job_id"]):
                break
            count += 1
        return count

    def hold_awake(state):
        """A sleeping Mac freezes the page and loses the lease, so it stays awake while a batch can run."""
        running = next((p for p in awake if p.poll() is None), None)
        if state["enabled"] or state["active_job_id"] is not None:
            if running is None and stay_awake:
                try:
                    awake[:] = [subprocess.Popen(["/usr/bin/caffeinate", "-di", "-t", str(AWAKE_SECONDS)])]
                except OSError:
                    pass
        elif running is not None:
            running.terminate()

    def save(state):
        hold_awake(state)
        state["updated_at"] = time.time()
        fd, filename = tempfile.mkstemp(prefix=".runner-state-", dir=directory)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(filename, state_path)
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(filename):
                os.unlink(filename)

    def append_event(role, text, *, record=None, to=""):
        event = {"at": time.time(), "role": role, "to": to[:100], "text": text[:3000]}
        if record:
            event.update(lease_id=record["lease_id"], job_id=record["job"]["job_id"])
        fd = os.open(event_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def events():
        if not event_path.exists():
            return []
        result = []
        # The number of displayed messages is bounded; the append-only audit stays local.
        for line in event_path.read_text().splitlines()[-100:]:
            try:
                event = json.loads(line)
                result.append({key: event[key] for key in
                               ("at", "role", "to", "text", "job_id", "lease_id") if key in event})
            except (ValueError, TypeError):
                continue
        return result

    def expire(state):
        job_id = state["active_job_id"]
        if job_id is None:
            return
        record = state["jobs"][job_id]
        preparation = record.get("preparation") or {}
        if preparation.get("status") == "started":
            preparation["status"] = "interrupted"
            record.update(status="unknown", finished_at=time.time(),
                          evidence="Local job preparation was interrupted; verify identity and resume before any retry.")
            state.update(active_job_id=None, enabled=False, pause_reason="unknown")
            save(state)
            append_event("system", record["evidence"], record=record)
            return
        callback = record.get("terminal_callback") or {}
        if callback.get("status") == "started":
            # The durable attempt marker precedes the callback. After a process crash
            # we cannot know whether its local side effect committed, so never replay it.
            callback["status"] = "interrupted"
            record.update(status="unknown", finished_at=time.time(),
                          evidence="Terminal recording was interrupted; verify the stored outcome before any retry.")
            state.update(active_job_id=None, enabled=False, pause_reason="terminal_callback_failed")
            save(state)
            append_event("system", record["evidence"], record=record)
            return
        if record["expires_at"] > time.time():
            return
        # With no submit permit there was no click, so nothing can have reached the employer.
        if never_clicked and never_clicked(job_id):
            record.update(status="blocked", finished_at=time.time(), evidence=(
                "这份没跑完就过期了，从没点过提交。多半是电脑睡着了，或申请页一直不在前台。可以重跑。"))
        else:
            record.update(status="unknown", finished_at=time.time(),
                          evidence="Worker lease expired; verify the employer outcome before any retry.")
        state.update(active_job_id=None, enabled=False, pause_reason="lease_expired")
        save(state)
        append_event("system", record["evidence"], record=record)

    def expire_retry(state):
        """A retry no page picked up. It would hold the board's buttons for good, and a page opened later does not arm itself."""
        if not state["enabled"] or not state.get("retry_job_id"):
            return
        if time.time() - max(state.get("started_at", 0), state.get("retry_polled_at", 0)) <= RETRY_WAIT_SECONDS:
            return
        state.update(enabled=False, pause_reason="needs_user", retry_job_id=None)
        save(state)
        append_event("system", f"重跑等了 {RETRY_WAIT_SECONDS // 60} 分钟，Chrome 执行台一直没接手，这次取消了：没打开申请页，也没点提交。"
                               "确认执行台开着，再到看板点一次重跑。")

    @contextmanager
    def locked_state():
        # A Python lock alone cannot protect multiple web workers or service instances.
        with thread_lock:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(directory / "runner.lock", os.O_RDWR | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a+") as lock_file:
                os.fchmod(lock_file.fileno(), 0o600)
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                try:
                    state = load()
                    expire(state)
                    expire_retry(state)
                    yield state
                finally:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)

    def owner(request):
        value = auth(request)
        if not isinstance(value, str) or not value:
            raise HTTPException(403, "Authenticated runner owner required.")
        return _hash(value)

    def public_record(record):
        result = {key: record[key] for key in
                ("job", "lease_id", "worker_id", "status", "claimed_at", "expires_at",
                 "finished_at", "evidence", "batch_id", "reported_outcome", "reported_evidence") if key in record}
        if record.get("terminal_callback"):
            result["terminal_callback_status"] = record["terminal_callback"]["status"]
        if record.get("preparation"):
            result["preparation_status"] = record["preparation"]["status"]
        return result

    def public(state):
        active = state["jobs"].get(state["active_job_id"])
        if active:
            phase = "running" if state["enabled"] else "pausing"
        elif state["pause_reason"] in {"lease_expired", "needs_user", "blocked", "error", "unknown",
                                      "terminal_callback_failed", "spam_flag", "too_many_stops"}:
            phase = "needs_user"
        elif state["pause_reason"] == "limit_reached":
            phase = "completed"
        else:
            phase = "idle" if state["enabled"] else "paused"
        result = {key: state[key] for key in
                  ("enabled", "apply_profile", "batch_id", "max_jobs", "claimed_count",
                   "submitted_count", "pause_reason", "updated_at")}
        result.update(phase=phase, active=public_record(active) if active else None,
                      jobs=[public_record(record) for record in state["jobs"].values()],
                      events=events(), started_at=state.get("started_at", 0), retry_job_id=state.get("retry_job_id"),
                      # A hidden tab's timers can slow to one tick a minute, hence the window.
                      worker={"online": time.time() - worker["seen"] < WORKER_ONLINE_SECONDS,
                              "version": worker["version"], "identity": worker["identity"],
                              "expected_version": expected_extension_version, "runner_url": runner_url})
        return result

    def owned(state, body, owner_hash):
        record = next((r for r in state["jobs"].values() if r["lease_id"] == body.lease_id), None)
        if (not record or record["owner_hash"] != owner_hash or
                record["worker_id"] != body.worker_id or
                not secrets.compare_digest(record["token_hash"], _hash(body.token))):
            raise HTTPException(404, "Runner lease not found.")
        return record

    def active_lease(body: LeaseRequest, owner_hash: str, job_id: str | None = None):
        with locked_state() as state:
            record = owned(state, body, owner_hash)
            claimed_job_id = record["job"]["job_id"]
            if job_id is not None and claimed_job_id != job_id:
                raise HTTPException(409, "Runner lease belongs to a different job.")
            if (record["status"] != "claimed" or state["active_job_id"] != claimed_job_id or
                    record["expires_at"] <= time.time()):
                # Also check the deadline after loading; a lease can cross its deadline
                # while this request is validating its owner and job binding.
                expire(state)
                raise HTTPException(409, "Runner lease is no longer active; stop this application.")
            return {"status": "active", "job_id": claimed_job_id, "expires_at": record["expires_at"]}

    def require_active_lease(body: LeaseRequest, authenticated_owner_string: str, job_id: str):
        """Check a worker at every review/permit boundary, using raw auth() owner.

        Callers must still check again immediately before a browser side effect;
        this response is current evidence, not a lease renewal or durable permit.
        """
        if not isinstance(authenticated_owner_string, str) or not authenticated_owner_string:
            raise HTTPException(403, "Authenticated runner owner required.")
        if not isinstance(body, LeaseRequest) or not isinstance(job_id, str) or not job_id:
            raise HTTPException(400, "A valid runner lease and job binding are required.")
        return active_lease(body, _hash(authenticated_owner_string), job_id)

    # The parent review router uses exactly the same durable gate as the worker endpoint.
    router.require_active_lease = require_active_lease

    def jobs_snapshot():
        with locked_state() as state:
            return state["batch_id"], [public_record(record) for record in state["jobs"].values()]

    router.jobs_snapshot = jobs_snapshot

    @router.post(PREFIX + "/check")
    def check(body: LeaseRequest, request: Request):
        return active_lease(body, owner(request))

    @router.post(PREFIX + "/start")
    def start(body: StartRequest, request: Request):
        owner(request)
        with locked_state() as state:
            if state["active_job_id"] is not None:
                raise HTTPException(409, "An active application must finish or expire before a new batch.")
            if state["enabled"] and state.get("retry_job_id"):
                # A new batch takes the next role in the queue, not the one she asked to run again.
                waiting = state["jobs"][state["retry_job_id"]]["job"]
                raise HTTPException(409, f"你要重跑的 {waiting['company']} · {waiting['title']} 还在等 Chrome 执行台接手，新的一轮不能替换它。"
                                         f"先点暂停，再回看板点一次重跑；或者等 {RETRY_WAIT_SECONDS // 60} 分钟，它会自己取消。")
            state.update(enabled=True, apply_profile=body.apply_profile,
                         batch_id=secrets.token_hex(12), max_jobs=body.max_jobs, retry_job_id=None,
                         claimed_count=0, submitted_count=0, pause_reason="", started_at=time.time())
            save(state)
            append_event("system", f"Started an Casey test batch, at most {body.max_jobs} job(s).")
        if on_start is not None:
            on_start()
        with locked_state() as state:
            return public(state)

    @router.post(PREFIX + "/requeue")
    def requeue(request: Request):
        # Bug fixes and new answers can clear a stop, so the owner may put stopped forms back in one click.
        if not str(auth(request)).startswith("browser:"):
            raise HTTPException(403, "只能在 8787 看板上操作。")
        with locked_state() as state:
            jobs = [r for r in state["jobs"].values() if r["status"] in RETRYABLE and not r.get("requeued")]
            freed = [r for r in jobs if never_clicked and never_clicked(r["job"]["job_id"])]
            for record in freed:
                record["requeued"] = True
            save(state)
            if freed:
                append_event("system", f"你把 {len(freed)} 份停下、从没点过提交的申请放回了队列。")
            return {"requeued": len(freed)}

    @router.post(PREFIX + "/retry")
    def retry(body: RetryRequest, request: Request):
        # A person asks for this on the board; the extension's token cannot.
        if not str(auth(request)).startswith("browser:"):
            raise HTTPException(403, "只能在 8787 看板上发起重跑。")
        with locked_state() as state:
            if state["active_job_id"] is not None:
                raise HTTPException(409, "还有申请在跑，等它结束再重跑。")
            record = state["jobs"].get(body.job_id)
            # Any other outcome may already have reached the employer.
            if not record or record["status"] not in RETRYABLE or not never_clicked or not never_clicked(body.job_id):
                raise HTTPException(409, "只有停下来、而且从没点过提交的申请才能重跑。")
            if one_per_company:
                # The hold the queue obeys. Refusing here tells her why; the claim checks again for a click during the wait.
                try:
                    reached = reached_employer(state, eligible())
                except Exception:
                    raise HTTPException(503, "读不到投递队列，这次没有重跑。过一会儿再点。") from None
                other = reached.get(cfgmod.norm_key(record["job"]["company"]))
                if other:
                    raise HTTPException(409, f"没有重跑：{record['job']['company']} 的另一份（{other['title']}）可能已经投出去了。"
                                             "现在再跑，等于同一家公司投两份。先去看那一份的结果。")
            # A form read over a day ago counts as unread, so read it before any claim can look for this job.
            if on_start is not None:
                on_start()
            state.update(enabled=True, batch_id=secrets.token_hex(12), max_jobs=1, retry_job_id=body.job_id,
                         claimed_count=0, submitted_count=0, pause_reason="", started_at=time.time())
            save(state)
            append_event("system", f"你要求重跑 {record['job']['company']} · {record['job']['title']}。")
            return public(state)

    @router.post(PREFIX + "/pause")
    def pause(request: Request):
        owner(request)
        with locked_state() as state:
            state.update(enabled=False, pause_reason="user_paused", retry_job_id=None)
            save(state)
            append_event("system", "Paused future claims. An active application is not cancelled.")
            return public(state)

    @router.get(PREFIX + "/status")
    def status(request: Request):
        owner(request)
        # Only the extension's runner page sends this; it is the proof it runs the loaded version.
        version = request.headers.get("x-jobscan-extension-version", "")
        if version:
            worker.update(seen=time.time(), version=version[:20],
                          identity=request.headers.get("x-jobscan-extension-identity", "")[:40])
        with locked_state() as state:
            return public(state)

    @router.post(PREFIX + "/claim")
    def claim(body: ClaimRequest, request: Request):
        owner_hash = owner(request)
        with locked_state() as state:
            if not state["enabled"]:
                return {"status": "paused", "reason": state["pause_reason"]}
            if state["active_job_id"] is not None:
                return {"status": "busy"}
            if state["claimed_count"] >= state["max_jobs"]:
                state.update(enabled=False, pause_reason="limit_reached")
                save(state)
                return {"status": "paused", "reason": "limit_reached"}
            # A job in the ledger is never taken again, except the one the owner asked to retry.
            retry_id = state.get("retry_job_id")
            try:
                candidates = eligible()
                # Two roles at one company seconds apart looks like spam, and it was refused at Close.
                taken = ({cfgmod.norm_key(r["job"]["company"]) for r in state["jobs"].values() if r.get("batch_id") == state["batch_id"]} |
                         set(reached_employer(state, candidates))) if one_per_company else set()
                clicked = {job["job_id"] for job in candidates if clicked_before(job["job_id"])}
                # A retry names its job but obeys the same hold: another role at the company may have gone out while it waited.
                job = next((job for job in candidates if job["job_id"] not in clicked and
                            cfgmod.norm_key(job["company"]) not in taken and
                            (job["job_id"] == retry_id if retry_id else
                             job["job_id"] not in state["jobs"] or state["jobs"][job["job_id"]].get("requeued"))), None)
            except Exception:
                raise HTTPException(503, "Eligible job queue unavailable; no application claimed.") from None
            if not job and reading is not None and reading():
                # A form read over a day ago counts as unread until this read reaches it, so nothing ready proves nothing yet.
                if retry_id:
                    # A page is here waiting on the read, so a long read does not use up the retry's wait.
                    state["retry_polled_at"] = time.time()
                    save(state)
                return {"status": "idle"}
            if not job and retry_id:
                state.update(enabled=False, pause_reason="needs_user", retry_job_id=None)
                save(state)
                append_event("system", "要重跑的这一份现在不符合条件（可能已投递、进入冷却、简历不在了、同一家公司另一份可能已投出去，或表单上还有题没答），没有重跑。")
                return {"status": "paused", "reason": "needs_user"}
            if not job:
                if state["claimed_count"]:
                    state.update(enabled=False, pause_reason="queue_empty")
                    save(state)
                    append_event("system", f"这一轮能投的都处理完了，一共处理了 {state['claimed_count']} 份。")
                    if notify:
                        notify(f"这一轮结束：处理 {state['claimed_count']} 份，投出 {state['submitted_count']} 份。")
                    return {"status": "paused", "reason": "queue_empty"}
                return {"status": "idle"}
            state["retry_job_id"] = None
            token = secrets.token_urlsafe(32)
            record = {"job": job, "lease_id": secrets.token_hex(16), "token_hash": _hash(token),
                      "owner_hash": owner_hash, "worker_id": body.worker_id,
                      "status": "claimed", "claimed_at": time.time(),
                      "expires_at": time.time() + LEASE_SECONDS, "batch_id": state["batch_id"]}
            state["jobs"][job["job_id"]] = record
            state["active_job_id"] = job["job_id"]
            state["claimed_count"] += 1
            # Durable reservation is committed before the browser may start any action.
            save(state)
            reason = job.get("selection_reason")
            append_event("system", f"Claimed {job['company']} — {job['title']}." + (f"\n为什么选它：{reason}" if reason else ""),
                         record=record)
            if prepare_claim is not None:
                record["preparation"] = {"status": "started", "at": time.time()}
                save(state)
                try:
                    prepared = prepare_claim(dict(job))
                    if prepared is not None:
                        prepared = _candidate(prepared)
                        if not prepared or prepared["job_id"] != job["job_id"] or prepared["url"] != job["url"]:
                            raise _PostingChanged()
                        job = prepared
                        record["job"] = prepared
                except Exception as exc:
                    record["preparation"]["status"] = "failed"
                    record.update(status="blocked", finished_at=time.time(), evidence=(
                        "Local identity/resume preparation failed before the form was opened; "
                        "the runner stopped without starting an employer application."))
                    state["active_job_id"] = None
                    # One role's resume that will not prepare (Atoms, 2026-11-08) is skipped like any stop before a page
                    # opens. A preparation that changed the posting means the code is wrong, so then the batch waits.
                    if isinstance(exc, _PostingChanged) or stopped_in_a_row(state) >= MAX_STOPS_IN_A_ROW:
                        state.update(enabled=False, pause_reason="blocked")
                        save(state)
                        append_event("system", record["evidence"], record=record)
                        raise HTTPException(503, "Job preparation failed; runner paused before opening the application.") from None
                    save(state)
                    append_event("system", record["evidence"] + " 跳过这一份，接着下一份。", record=record)
                    return {"status": "idle"}
                record["preparation"]["status"] = "completed"
                save(state)
                append_event("system", "Selected role identity and resume preparation completed.", record=record)
            return {"status": "claimed", "job": job, "lease_id": record["lease_id"],
                    "token": token, "expires_at": record["expires_at"]}

    @router.post(PREFIX + "/event")
    def event(body: EventRequest, request: Request):
        owner_hash = owner(request)
        with locked_state() as state:
            record = owned(state, body, owner_hash)
            if state["active_job_id"] != record["job"]["job_id"] or record["status"] != "claimed":
                raise HTTPException(409, "This lease is no longer active.")
            append_event(body.role, body.text, record=record, to=body.to)
            return {"status": "recorded"}

    @router.post(PREFIX + "/finish")
    def finish(body: FinishRequest, request: Request):
        owner_hash = owner(request)
        with locked_state() as state:
            record = owned(state, body, owner_hash)
            outcome, evidence = settled_outcome(record["job"]["job_id"], body)
            if record["status"] != "claimed":
                if record["status"] == outcome:
                    return public(state)  # Network retries do not count or submit twice.
                raise HTTPException(409, "The terminal outcome cannot be changed by this worker.")
            if state["active_job_id"] != record["job"]["job_id"]:
                raise HTTPException(409, "This lease is no longer active.")
            if terminal_callback is not None:
                record.update(reported_outcome=outcome, reported_evidence=evidence,
                              terminal_callback={"status": "started", "at": time.time()})
                save(state)
                try:
                    terminal_callback(dict(record["job"]), outcome, evidence)
                except Exception:
                    record["terminal_callback"]["status"] = "failed"
                    record.update(status="unknown", finished_at=time.time(), evidence=(
                        "Worker reported an outcome, but local terminal recording failed; "
                        "verify it before any retry. No new submission will be attempted."))
                    state.update(active_job_id=None, enabled=False, pause_reason="terminal_callback_failed")
                    save(state)
                    append_event("system", record["evidence"], record=record)
                    raise HTTPException(503, "Terminal recording failed; runner paused without retrying the job.") from None
                record["terminal_callback"]["status"] = "completed"
            record.update(status=outcome, evidence=evidence, finished_at=time.time())
            state["active_job_id"] = None
            if outcome == "submitted":
                state["submitted_count"] += 1
            # A job that stopped before Submit costs nothing, so a batch moves on and shows where the
            # next one stops. "unknown" may have reached the employer and "error" means something broke.
            # Losing the page with nothing clicked is safe for this job, but the next one would meet the same page.
            if body.outcome in {"unknown", "error"}:
                state.update(enabled=False, pause_reason=outcome)
            elif refused(outcome, evidence, record["job"]["job_id"]) and SPAM_FLAG.search(evidence):
                # A spam flag can spread past one employer, so the owner looks before anything else goes out.
                state.update(enabled=False, pause_reason="spam_flag")
            elif stopped_in_a_row(state) >= MAX_STOPS_IN_A_ROW:
                # Preflight should have parked these, so several stops mean something changed. Each one costs tokens.
                state.update(enabled=False, pause_reason="too_many_stops")
                append_event("system", f"连续 {MAX_STOPS_IN_A_ROW} 份都停下了，先不跑了，等你看一眼原因。")
            elif state["claimed_count"] >= state["max_jobs"]:
                # An employer's no is a finished job, like a closed posting: nothing here waits for her.
                done = outcome in {"submitted", "expired"} or refused(outcome, evidence, record["job"]["job_id"])
                state.update(enabled=False, pause_reason="limit_reached" if done else outcome)
            save(state)
            append_event("system", f"Worker reported {outcome}: {evidence}", record=record)
            if state.get("pause_reason") == "spam_flag":
                append_event("system", "雇主的回复提到疑似垃圾申请或申请太多，这一轮先停了，等你看一眼。", record=record)
            if notify:
                if not state["enabled"]:
                    notify(f"这一轮结束：处理 {state['claimed_count']} 份，投出 {state['submitted_count']} 份。")
                elif outcome not in {"submitted", "expired"} and not refused(outcome, evidence, record["job"]["job_id"]):
                    notify(f"{record['job']['company']} 停下了，申请页上标红的题等你看。")
            return public(state)

    return router
