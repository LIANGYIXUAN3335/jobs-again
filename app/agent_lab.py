"""Local review workbench; browser clicks require a reviewed, single-use permit."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from . import config as cfgmod
from . import confirmed_answers, form_preflight, interview_ledger, login_vault
from .agent_review import Snapshot, confirmed_answer, demo_context, digest, normalized, review
from .agent_runner import RETRYABLE, SAME_COMPANY_GAP_DAYS, LeaseRequest


class AnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=3000)
    # The statement under a bare title. An answer covers only this question with this statement.
    description: str = Field(default="", max_length=3000)
    answer: str = Field(min_length=1, max_length=4000)
    job_id: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=300)


class LoginSave(BaseModel):
    site: str = Field(min_length=1, max_length=500)
    username: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=500)


class LoginHost(BaseModel):
    host: str = Field(min_length=1, max_length=300)


class EssaySetting(BaseModel):
    allow_unread_essays: bool


class InterviewOverride(BaseModel):
    company: str = Field(min_length=1, max_length=300)
    action: Literal["block", "allow", "clear"]


class InterviewEventName(BaseModel):
    event_id: str = Field(min_length=1, max_length=300)
    company: str = Field(min_length=1, max_length=300)


class AnswerChange(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)


class ReviewRequest(BaseModel):
    job_id: str = Field(max_length=200)
    apply_profile: str = Field(default="", max_length=100)
    snapshot: Snapshot
    demo: bool = False
    run_id: str | None = None
    submit_authorized: bool = False
    runner_lease: LeaseRequest | None = None


class SubmissionResult(BaseModel):
    run_id: str
    attempt_id: str
    outcome: Literal["submitted", "validation_error", "unknown", "blocked"]
    evidence: str = Field(min_length=1, max_length=3000)


class HandRefusal(BaseModel):
    job_id: str = Field(min_length=1, max_length=200)
    evidence: str = Field(min_length=1, max_length=3000)
    url: str = Field(default="", max_length=3000)


class PairRequest(BaseModel):
    code: str


def build_router(cfg: dict, context_loader=None, reviewer=review, run_dir=None) -> APIRouter:
    router = APIRouter()
    sessions: dict[str, float] = {}
    pair_codes: dict[str, tuple[float, str]] = {}
    runs: dict[str, dict] = {}
    lock = threading.RLock()
    directory = Path(run_dir or cfgmod.ROOT / "data" / "agent_lab")
    stuck_file = directory / "stuck_questions.json"
    workspace = re.sub(r"\W+", "_", cfgmod.workspace_name(cfg)).strip("_").lower()
    # Token hash -> extension origin. Survives a service restart; the token itself is never stored.
    paired_file = directory / (f"paired_extensions.{workspace}.json" if workspace else "paired_extensions.json")
    try:
        extensions: dict[str, str] = json.loads(paired_file.read_text())
    except (OSError, ValueError):
        extensions = {}

    def token_key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def same_origin(request):
        origin = request.headers.get("origin")
        base = str(request.base_url).rstrip("/")
        if origin:
            return origin == base
        # Browsers omit Origin on same-origin GETs. The session token remains required.
        if request.method == "GET" and request.headers.get("sec-fetch-site") == "same-origin":
            referer = urlparse(request.headers.get("referer", ""))
            return f"{referer.scheme}://{referer.netloc}" == base
        return False

    def auth(request: Request):
        token = request.headers.get("x-jobscan-review", "")
        with lock:
            if token in sessions and sessions[token] > time.time() and same_origin(request):
                return "browser:" + token
            origin = request.headers.get("origin", "")
            paired = extensions.get(token_key(token))
            # Chrome sends no Origin on a GET from an extension that has host permission. A web
            # page cannot attach this header without one, and the token is still required.
            if paired and (origin == paired or (not origin and request.method == "GET")):
                return "extension:" + token
        raise HTTPException(403, "连接已失效：请到 8787 看板重新生成连接码并连接；如果正在看板页面，请刷新。")

    def public(run):
        return {k: v for k, v in run.items() if k not in {"owner", "context", "snapshot", "receipt_path"}}

    def read_stuck():
        try:
            return json.loads(stuck_file.read_text())
        except (OSError, ValueError):
            return {}

    def record_stuck(body, result):
        """Keep what stopped this application, so the board can ask the owner even after a restart."""
        fields = {f.id: f for f in body.snapshot.fields}
        # Over the whole form, not the flagged rows: only one of four "Education History" rows may be required.
        copies = Counter(_title_key(f.label, f.description) for f in body.snapshot.fields)
        questions, page = {}, []
        for issue in result["issues"]:
            field = fields.get(issue["field_id"])
            if issue.get("auto_fix"):
                continue
            if field is None or field.kind == "file":
                page.append(f"{field.label}：{issue['reason']}" if field else issue["reason"])
            elif field.id in questions:
                questions[field.id]["reason"] += "；" + issue["reason"]
            else:
                questions[field.id] = {"field_id": field.id, "label": field.label, "description": field.description, "kind": field.kind,
                                       "options": field.options, "value": field.value, "reason": issue["reason"],
                                       "repeated": copies[_title_key(field.label, field.description)]}
        stuck = read_stuck()
        stuck[body.job_id] = {"lease_id": body.runner_lease.lease_id, "at": time.time(),
                              "questions": list(questions.values()), "page_issues": page}
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = stuck_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(stuck, ensure_ascii=False))
            tmp.chmod(0o600)
            tmp.replace(stuck_file)
        except OSError:
            pass  # The review result matters more; the board then shows only the stop reason.

    def board_only(request):
        if not auth(request).startswith("browser:"):
            raise HTTPException(403, "只能在 8787 看板上查看和修改。")

    def answers_view():
        """Everything the board's two answer cards show. A stopped job says what is true of it now, not only what stopped it then."""
        stuck = read_stuck()
        batch_id, records = runner_router.jobs_snapshot()
        stopped = [r for r in records if r["status"] in RETRYABLE and never_clicked(r["job"]["job_id"])]
        facts = stopped_job_facts(cfg, [r["job"] for r in stopped]) if context_loader and stopped else {}
        code_at = code_changed_at()
        paused, notes, refusals = [], {}, {}
        for record in stopped:
            job = record["job"]
            fact = facts.get(job["job_id"]) or {}
            # Finished by hand or closed: nothing waits for her, and "not submitted yet" would be false.
            if fact.get("closed"):
                notes[job["job_id"]] = ("雇主说不收这份，这家公司已经按它写的时间冷却了。不用再管。" if fact.get("refused")
                                        else "这份已经不是待投状态了（比如你自己投完了），不用再管。")
                refusals[job["job_id"]] = fact.get("refused", "")
                continue
            entry = stuck.get(job["job_id"]) or {}
            current = entry.get("lease_id") == record["lease_id"]
            rows = (entry.get("questions") or []) if current else []
            issues = list(entry.get("page_issues") or []) if current else []
            note, can_retry = fact.get("waits", ""), not fact.get("waits")
            if fact.get("why"):
                # It cannot run, so an answer would change nothing for this job and stay on file for every other one.
                rows, issues, note, can_retry = [], [], fact["why"], False
            elif fact.get("verdict") == "ready" and current and entry.get("at", 0) < code_at:
                # Those questions came from older code. A newer stop keeps them: the Reviewer may have seen what preflight cannot.
                rows, issues, note = [], [], "这次停下之后程序修过了，现在这张表没有要你答的题。不用答，直接点重跑。"
            elif fact.get("verdict") == "unread" and current and entry.get("at", 0) < code_at:
                # A form older than a day counts as unread. That is no reason to hand back questions older code asked.
                rows, issues, note = [], [], "这次停下之后程序改过了，这张表还没重新读。点下面「重新读表单」，读完这里会更新。也可以直接点重跑。"
            rows, repeated = _without_repeated_titles(rows)
            known = {"confirmed_answers": confirmed_answers.for_company(cfg, job["company"])}
            questions = [{**q, "answer": (confirmed_answer(q["label"], known, q.get("description") or "") or {}).get("answer", "")} for q in rows]
            notes[job["job_id"]] = note
            paused.append({"job_id": job["job_id"], "company": job["company"], "title": job["title"],
                           "status": record["status"], "evidence": record.get("evidence", ""), "questions": questions,
                           "page_issues": issues + repeated, "note": note, "can_retry": can_retry,
                           "finished_at": record.get("finished_at", 0)})
        paused.sort(key=lambda item: item["finished_at"], reverse=True)
        unanswered = {p["job_id"]: sum(not q["answer"] for q in p["questions"]) for p in paused}
        batch = sorted((r for r in records if r.get("batch_id") == batch_id), key=lambda r: r.get("claimed_at", 0))
        # A row whose click was settled later, by a receipt she got by pressing Submit herself, must not keep sending her back to that page.
        settled = roles_now(cfg, [r["job"]["job_id"] for r in batch if r["status"] != "submitted" and not never_clicked(r["job"]["job_id"])]) if context_loader else {}

        def batch_row(r):
            job_id = r["job"]["job_id"]
            clicked, role = not never_clicked(job_id), settled.get(job_id) or {}
            refusal = refusals.get(job_id) or _refusal_on_file(role)
            return {"job_id": job_id, "company": r["job"]["company"], "title": r["job"]["title"], "status": r["status"],
                    "evidence": REFUSED_PREFIX + refusal if refusal else r.get("evidence", ""), "clicked": clicked,
                    "needs_code": clicked and role.get("status", "new") == "new" and VERIFICATION_STOP.search(r.get("evidence") or "") is not None,
                    "went_out": role.get("status") in WENT_OUT, "note": notes.get(job_id, ""), "unanswered": unanswered.get(job_id, 0)}
        answers = sorted(confirmed_answers.all_answers(cfg), key=lambda item: item.get("updated_at", 0), reverse=True)
        return {"paused": paused,
                "batch": [batch_row(r) for r in batch],
                "answers": [{key: item.get(key, "") for key in ("id", "question", "description", "answer", "company")} for item in answers]}

    def save(run):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        p = directory / (run["id"] + ".json")
        tmp = p.with_suffix(".tmp")
        # No bearer tokens or raw SDK errors are persisted.
        tmp.write_text(json.dumps(public(run), ensure_ascii=False, indent=2))
        tmp.chmod(0o600)
        tmp.replace(p)

    def owned(run_id, owner):
        run = runs.get(run_id)
        if not run or run["owner"] != owner:
            raise HTTPException(404, "本次运行不存在。")
        return run

    def checked_run(body, owner):
        run = owned(body.run_id, owner)
        lease_id = body.runner_lease.lease_id if body.runner_lease else None
        if run.get("runner_lease_id") != lease_id:
            raise HTTPException(409, "执行任务的领取凭据改变，停止处理。")
        return run

    def context(body, owner):
        if body.demo:
            if body.job_id != "demo-001" or urlparse(body.snapshot.url).path != "/agent-lab/fixture":
                raise HTTPException(400, "Invalid demo binding")
            return demo_context()
        if context_loader is None:
            raise HTTPException(409, "这个进程只运行练习页；真实岗位请使用 JobScan 主服务。")
        if body.runner_lease:
            runner_router.require_active_lease(body.runner_lease, owner, body.job_id)
        return context_loader(body.job_id, body.apply_profile, body.snapshot.url)

    def workbench_page(request: Request, template: str):
        if request.url.hostname not in {"localhost", "127.0.0.1", "testserver"}:
            raise HTTPException(403, "Local workbench only")
        token = secrets.token_urlsafe(32)
        with lock:
            now = time.time()
            for key in list(sessions):
                if sessions[key] < now:
                    del sessions[key]
            sessions[token] = now + 86400
        html = (cfgmod.ROOT / "templates" / template).read_text()
        return HTMLResponse(html.replace("__REVIEW_TOKEN__", token), headers={
            "Cache-Control": "no-store", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "frame-ancestors 'none'"})

    @router.get("/agent-lab", response_class=HTMLResponse)
    def page(request: Request):
        return workbench_page(request, "agent_lab.html")

    @router.get("/agent-lab/independent", response_class=HTMLResponse)
    def independent_page(request: Request):
        return workbench_page(request, "agent_independent.html")

    @router.get("/agent-lab/runner", response_class=HTMLResponse)
    def runner_page(request: Request):
        return workbench_page(request, "agent_runner.html")

    @router.get("/agent-lab/fixture", response_class=HTMLResponse)
    def fixture():
        return HTMLResponse((cfgmod.ROOT / "templates" / "agent_fixture.html").read_text(),
                            headers={"Cache-Control": "no-store", "Content-Security-Policy": "frame-ancestors 'self'"})

    @router.get("/agent-lab/live", response_class=HTMLResponse)
    def live_page(request: Request):
        if request.url.hostname not in {"localhost", "127.0.0.1", "testserver"}:
            raise HTTPException(403)
        return HTMLResponse((cfgmod.ROOT / "templates" / "agent_team.html").read_text(), headers={"Cache-Control":"no-store"})

    @router.get("/api/agent-lab/live-team")
    def live_team(request: Request):
        if request.url.hostname not in {"localhost", "127.0.0.1", "testserver"}:
            raise HTTPException(403)
        path = directory / "live_team.jsonl"
        events = []
        if path.exists():
            for line in path.read_text().splitlines()[-150:]:
                try:
                    item = json.loads(line)
                    if item.get("role") not in {"filler", "reviewer", "coordinator"}: continue
                    events.append({k:item.get(k) for k in ("role", "to", "text", "at")})
                except (ValueError, TypeError):
                    continue
        return {"events":events}

    @router.get("/agent-lab/assets/{name}")
    def assets(name: str):
        files = {"form-agent.js": cfgmod.ROOT / "extension" / "form_agent.js",
                 "content.js": cfgmod.ROOT / "extension" / "content.js",
                 "lab.js": cfgmod.ROOT / "static" / "agent_lab.js",
                 "lab.css": cfgmod.ROOT / "static" / "agent_lab.css",
                 "fixture.js": cfgmod.ROOT / "static" / "agent_fixture.js"}
        files["workflow.js"] = cfgmod.ROOT / "extension" / "agent_workflow.js"
        files["team.js"] = cfgmod.ROOT / "static" / "agent_team.js"
        files["independent.js"] = cfgmod.ROOT / "static" / "agent_independent.js"
        files["runner.js"] = cfgmod.ROOT / "static" / "agent_runner.js"
        if name not in files:
            raise HTTPException(404)
        return FileResponse(files[name])

    @router.post("/api/agent-lab/pair-code")
    def pair_code(request: Request):
        owner = auth(request)
        if not owner.startswith("browser:"):
            raise HTTPException(403)
        code = secrets.token_hex(6)
        with lock:
            pair_codes[code] = (time.time() + 600, owner)
        return {"code": code}

    @router.post("/api/agent-lab/pair")
    def pair(body: PairRequest, request: Request):
        origin = request.headers.get("origin", "")
        if not origin.startswith("chrome-extension://"):
            raise HTTPException(403)
        with lock:
            item = pair_codes.pop(body.code.strip(), None)
            if not item or item[0] < time.time():
                raise HTTPException(403, "连接码失效，请重新生成。")
            token = secrets.token_urlsafe(32)
            extensions[token_key(token)] = origin
            while len(extensions) > 32:
                extensions.pop(next(iter(extensions)))
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = paired_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(extensions))
            tmp.chmod(0o600)
            tmp.replace(paired_file)
        return {"token": token}

    @router.post("/api/agent-lab/review")
    def run_review(body: ReviewRequest, request: Request):
        owner = auth(request)
        ctx = context(body, owner)
        with lock:
            if sum(r["status"] == "reviewing" for r in runs.values()) >= 2:
                raise HTTPException(429, "已有检查正在运行。")
            if body.run_id:
                run = checked_run(body, owner)
                if run["job_id"] != body.job_id or run["demo"] != body.demo or run["apply_profile"] != body.apply_profile:
                    raise HTTPException(409, "任务身份改变，请新建运行。")
                if run["document_id"] != body.snapshot.document_id or run["url"] != body.snapshot.url:
                    raise HTTPException(409, "页面已刷新或跳转，请新建运行。")
                if run["status"] == "reviewing":
                    raise HTTPException(409, "Review 正在运行。")
                if run["status"] in {"submitting", "submitted", "unknown", "blocked"}:
                    raise HTTPException(409, "提交已结束或结果不明，不能自动重试。")
                if run["round"] >= 5:
                    raise HTTPException(409, "已达到五轮检查上限，请人工处理。")
            else:
                run = {"id": secrets.token_hex(12), "owner": owner, "job_id": body.job_id,
                       "demo": body.demo, "apply_profile": body.apply_profile, "document_id": body.snapshot.document_id,
                       "url": body.snapshot.url, "round": 0, "events": [], "created": time.time(),
                       "runner_lease_id": body.runner_lease.lease_id if body.runner_lease else None}
                runs[run["id"]] = run
            run.update(status="reviewing", context=ctx, snapshot=body.snapshot, round=run["round"] + 1)
            run["events"].append({"role": "reviewer", "text": "独立读取页面快照，核对规则与事实。", "at": time.time()})
        try:
            result = reviewer(body.snapshot, ctx, cfg)
        except Exception:
            result = {"decision": "needs_user", "model_status": "error", "model": "",
                      "summary": "检查未完成，请重试或人工处理。", "issues": []}
        with lock:
            run.update(result=result, status="awaiting_confirmation" if result["decision"] == "pass" else result["decision"])
            run["events"].append({"role": "reviewer", "text": result["summary"], "at": time.time()})
            save(run)
            if result["decision"] != "pass" and body.runner_lease and not body.demo:
                record_stuck(body, result)
            return public(run)

    @router.get("/api/agent-lab/answers")
    def list_answers(request: Request):
        board_only(request)
        return answers_view()

    def refuse_repeated_title(job_id, question, description=""):
        rows = (read_stuck().get(job_id) or {}).get("questions") or []
        same = [q for q in rows if _title_key(q["label"], q.get("description")) == _title_key(question, description)]
        if len(same) > 1 or any((q.get("repeated") or 0) > 1 for q in same):
            raise HTTPException(409, "这个标题在这张表上出现了好几次，一个答案会填到以后所有同名的题，不能记。请到申请页上手动填。")

    @router.post("/api/agent-lab/answers")
    def add_answer(body: AnswerRequest, request: Request):
        board_only(request)
        refuse_repeated_title(body.job_id, body.question, body.description)
        rows = (read_stuck().get(body.job_id) or {}).get("questions") or []
        # A stopped form showed her the page. Before any run all she has is the title, and the words it signs are not in front of her.
        if not body.description.strip() and confirmed_answers.statement_title(body.question) and not any(q["label"] == body.question for q in rows):
            raise HTTPException(409, "这句声明只写在申请页上，这里看不到，不能在这里答。请打开申请页读完再说。")
        job = next((r["job"] for r in runner_router.jobs_snapshot()[1] if r["job"]["job_id"] == body.job_id), {})
        try:
            confirmed_answers.remember(cfg, body.question, body.answer, description=body.description, company=job.get("company", "") or body.company)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return answers_view()

    def preflight_view():
        from . import ai
        trouble = ai.cli_trouble
        warning = ""
        if trouble:
            warning = ("Claude 命令行的登录过期了，填表现在走付费 API。在终端跑一次 claude 重新登录就好。" if trouble.get("expired")
                       else "Claude 命令行刚才调用失败，填表现在走付费 API。")
        return {**form_preflight.board(cfg, runner_pool(cfg)), "model_warning": warning}

    def interviews_view():
        from . import store
        conn = store.connect_view(cfg)
        try:
            return interview_ledger.view(cfg, conn)
        finally:
            conn.close()

    @router.get("/api/agent-lab/requeue-places")
    def requeue_places(request: Request):
        """Where the stopped jobs land after 把停下的放回队列, counting only the ones that can run."""
        board_only(request)
        paused = answers_view()["paused"]
        places = next_batch_places(runner_candidates(cfg) if context_loader else [], runner_router.jobs_snapshot()[1], never_clicked,
                                   clicked_roles=clicked_new_roles(cfg, directory) if context_loader else ())
        return {"runnable": sorted(({"company": p["company"], "title": p["title"], "place": places[p["job_id"]]}
                                    for p in paused if p["job_id"] in places), key=lambda item: item["place"]),
                "cannot": sum(p["job_id"] not in places for p in paused)}

    @router.get("/api/agent-lab/preflight")
    def preflight(request: Request):
        board_only(request)
        return preflight_view()

    @router.post("/api/agent-lab/preflight/refresh")
    def preflight_refresh(request: Request):
        board_only(request)
        form_preflight.refresh_in_background(cfg, lambda: runner_pool(cfg))
        return preflight_view()

    @router.post("/api/agent-lab/preflight/essays")
    def preflight_essays(body: EssaySetting, request: Request):
        board_only(request)
        form_preflight.set_allow_essays(cfg, body.allow_unread_essays)
        return preflight_view()

    @router.get("/api/agent-lab/logins")
    def logins(request: Request):
        board_only(request)
        return {"logins": login_vault.listing(cfg), "sites": list(login_vault.JOB_SITES)}

    @router.post("/api/agent-lab/logins")
    def login_save(body: LoginSave, request: Request):
        board_only(request)
        try:
            login_vault.save(cfg, body.site, body.username, body.password)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except (RuntimeError, OSError, subprocess.SubprocessError):
            raise HTTPException(503, "钥匙串保存失败，这个账号没有存下。") from None
        return {"logins": login_vault.listing(cfg), "sites": list(login_vault.JOB_SITES)}

    @router.delete("/api/agent-lab/logins/{host}")
    def login_remove(host: str, request: Request):
        board_only(request)
        try:
            login_vault.remove(cfg, host)
        except KeyError:
            raise HTTPException(404, "这个账号已经不在了。") from None
        return {"logins": login_vault.listing(cfg), "sites": list(login_vault.JOB_SITES)}

    @router.post("/api/agent-lab/logins/status")
    def login_status(body: LoginHost, request: Request):
        auth(request)
        return {"username": login_vault.username_for(cfg, body.host)}

    @router.post("/api/agent-lab/logins/fill")
    def login_fill(body: LoginHost, request: Request):
        # The one way a password leaves the Keychain, and only the extension may ask.
        if not auth(request).startswith("extension:"):
            raise HTTPException(403, "只有扩展能取账号。")
        login = login_vault.fill_for(cfg, body.host)
        if not login:
            raise HTTPException(404, "这个网站没有保存账号。")
        return login

    @router.get("/api/agent-lab/sent/{job_id:path}")
    def sent(job_id: str, request: Request):
        board_only(request)
        try:
            receipt = json.loads((directory / ("submission-" + digest([job_id, "real"]) + ".json")).read_text())
        except (OSError, ValueError):
            raise HTTPException(404, "这份没有留底：它没点过提交，或者是加留底之前投的。") from None
        return {"sent": receipt.get("sent") or [], "outcome": receipt.get("outcome"), "evidence": receipt.get("evidence", "")}

    @router.get("/api/agent-lab/interviews")
    def interviews(request: Request):
        board_only(request)
        return interviews_view()

    @router.post("/api/agent-lab/interviews/refresh")
    def interviews_refresh(request: Request):
        board_only(request)
        result = interview_ledger.refresh_calendar(cfg)
        return {**interviews_view(), "refresh_error": result.get("error", "")}

    @router.post("/api/agent-lab/interviews/override")
    def interviews_override(body: InterviewOverride, request: Request):
        board_only(request)
        interview_ledger.override(cfg, body.company, body.action)
        return interviews_view()

    @router.post("/api/agent-lab/interviews/name-event")
    def interviews_name_event(body: InterviewEventName, request: Request):
        board_only(request)
        interview_ledger.name_event(cfg, body.event_id, body.company)
        return interviews_view()

    @router.post("/api/agent-lab/stuck-answer")
    def stuck_answer(body: AnswerRequest, request: Request):
        auth(request)
        # Only a question this stopped application really asked: the panel on an employer's page writes nothing else.
        entry = read_stuck().get(body.job_id) or {}
        rows = [q for q in entry.get("questions", []) if q["label"] == body.question]
        if not rows:
            raise HTTPException(403, "这道题不是这份申请停下时问的。")
        # Two statements can share a title, and the answer covers the one she read.
        rows = [q for q in rows if (q.get("description") or "") == body.description] or rows
        asked = rows[0]
        refuse_repeated_title(body.job_id, body.question, asked.get("description") or "")
        if len(rows) > 1:
            raise HTTPException(409, "这个标题下有不止一句声明，分不清你答的是哪一句。请到 8787 看板回答。")
        job = next((r["job"] for r in runner_router.jobs_snapshot()[1] if r["job"]["job_id"] == body.job_id), {})
        try:
            # The statement comes from what the form showed, whatever the panel sends, so the answer covers exactly that.
            confirmed_answers.remember(cfg, body.question, body.answer, description=asked.get("description") or "", company=job.get("company", ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"saved": True}

    @router.put("/api/agent-lab/answers/{answer_id}")
    def change_answer(answer_id: str, body: AnswerChange, request: Request):
        board_only(request)
        try:
            confirmed_answers.change(cfg, answer_id, body.answer)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except KeyError:
            raise HTTPException(404, "这条答案已经不在了。") from None
        return answers_view()

    @router.delete("/api/agent-lab/answers/{answer_id}")
    def delete_answer(answer_id: str, request: Request):
        board_only(request)
        try:
            confirmed_answers.forget(cfg, answer_id)
        except KeyError:
            raise HTTPException(404, "这条答案已经不在了。") from None
        return answers_view()

    @router.post("/api/agent-lab/confirm")
    def confirm(body: ReviewRequest, request: Request):
        owner = auth(request)
        ctx = context(body, owner)
        with lock:
            run = checked_run(body, owner)
            if (run["job_id"], run["demo"], run["apply_profile"]) != (body.job_id, body.demo, body.apply_profile):
                raise HTTPException(409, "任务身份改变，请新建运行。")
            if run["status"] not in {"awaiting_confirmation", "review_passed"}:
                raise HTTPException(409, "没有可确认的通过结论。")
            if body.snapshot.fingerprint() != run["result"].get("fingerprint") or digest(ctx) != run["result"].get("facts_version"):
                run["status"] = "stale"
                save(run)
                raise HTTPException(409, "页面或确认事实已改变，需要重新检查。")
            run["status"] = "review_passed"
            save(run)
            return public(run)

    @router.post("/api/agent-lab/repair-plan")
    def repair_plan(body: ReviewRequest, request: Request):
        from .agent_filler import plan_repairs
        owner = auth(request)
        ctx = context(body, owner)
        with lock:
            run = checked_run(body, owner)
            if (run["job_id"], run["apply_profile"], run["demo"]) != (body.job_id,body.apply_profile,body.demo):
                raise HTTPException(409, "修正目标或身份改变。")
            if run["status"] not in {"needs_fix", "needs_user"}:
                raise HTTPException(409, "当前任务不能生成修正。")
            if body.snapshot.fingerprint() != run["result"].get("fingerprint") or digest(ctx) != run["result"].get("facts_version"):
                raise HTTPException(409, "修正前表单或事实已改变，需要重新审查。")
            run["status"] = "repair_planning"
            original = run["result"]
        plan = plan_repairs(body.snapshot, ctx, original, cfg)
        with lock:
            run["status"] = "needs_fix" if plan["status"] == "planned" else "needs_user"
            run["events"].append({"role":"filler", "text":plan["summary"], "at":time.time()})
            save(run)
        return plan

    @router.post("/api/agent-lab/submit-permit")
    def submit_permit(body: ReviewRequest, request: Request):
        owner = auth(request)
        ctx = context(body, owner)
        with lock:
            run = checked_run(body, owner)
            if not body.submit_authorized:
                raise HTTPException(409, "本份申请尚未获得提交授权。")
            if (run["job_id"], run["demo"], run["apply_profile"], run["url"], run["document_id"]) != (body.job_id, body.demo, body.apply_profile, body.snapshot.url, body.snapshot.document_id):
                raise HTTPException(409, "提交目标或身份改变。")
            if run["status"] != "review_passed" or run["result"]["model_status"] != "completed":
                raise HTTPException(409, "必须先通过独立检查。")
            if body.snapshot.fingerprint() != run["result"]["fingerprint"] or digest(ctx) != run["result"]["facts_version"]:
                run["status"] = "stale"
                save(run)
                raise HTTPException(409, "页面或共享事实改变，必须重新审查。")
            if not body.demo and ctx.get("application_status") != "new":
                raise HTTPException(409, "该岗位不是待投的新申请，不能自动重复投递。")
            if not body.demo and ctx.get("submission_blocker"):
                raise HTTPException(409, ctx["submission_blocker"])
            # A persisted receipt survives restart and blocks a second tab from re-clicking.
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            key = digest([body.job_id, body.snapshot.document_id if body.demo else "real"])
            path = directory / ("submission-" + key + ".json")
            receipt = json.loads(path.read_text()) if path.exists() else {"attempts": 0}
            if receipt.get("outcome") not in {None, "validation_error"} or receipt["attempts"] >= 2:
                raise HTTPException(409, "已提交、结果不明或已达两次尝试上限；请核对原申请。")
            attempt = secrets.token_hex(12)
            # The copy of what goes out under the owner's name; the event log only keeps a shortened version.
            receipt.update(outcome="submitting", attempts=receipt["attempts"] + 1, attempt_id=attempt,
                           sent=[{"label": f.label, "value": f.value} for f in body.snapshot.fields if f.kind != "file"])
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(receipt, ensure_ascii=False)); tmp.chmod(0o600); tmp.replace(path)
            run.update(status="submitting", attempt_id=attempt, receipt_path=str(path))
            run["events"].append({"role":"reviewer", "text":"检查通过，获得本份申请的一次提交许可。", "at":time.time()})
            save(run)
            return public(run)

    @router.post("/api/agent-lab/submission-result")
    def submission_result(body: SubmissionResult, request: Request):
        with lock:
            run = owned(body.run_id, auth(request))
            if run["status"] != "submitting" or run.get("attempt_id") != body.attempt_id:
                raise HTTPException(409, "提交许可已使用或不匹配。")
            path = Path(run["receipt_path"])
            receipt = json.loads(path.read_text())
            if receipt.get("attempt_id") != body.attempt_id or receipt.get("outcome") != "submitting":
                raise HTTPException(409, "提交回执不匹配。")
            receipt.update(outcome=body.outcome, evidence=body.evidence)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(receipt, ensure_ascii=False)); tmp.chmod(0o600); tmp.replace(path)
            run.update(status=body.outcome, submission_evidence=body.evidence)
            run["events"].append({"role":"reviewer", "text":body.evidence, "at":time.time()})
            save(run)
        # A batch reports the same words to /finish, which records them once. Only the panel's own Submit ends here.
        if body.outcome == "blocked" and body.evidence.startswith(REFUSED_PREFIX) and not run["demo"] and not run.get("runner_lease_id"):
            try:
                record_refused(cfg, {"job_id": run["job_id"], "company": run["context"].get("company"), "url": run["url"]}, body.evidence)
            except Exception as exc:  # The receipt is on file and refuses a second send, so the panel still gets its answer.
                with lock:
                    run["events"].append({"role":"reviewer", "text":f"雇主拒收已记在回执里，但没能设冷却（{type(exc).__name__}）。", "at":time.time()})
                    save(run)
        with lock:
            return public(run)

    @router.post("/api/agent-lab/hand-refusal")
    def hand_refusal(body: HandRefusal, request: Request):
        # She pressed Submit herself and the employer said no on the page: the same closure and freeze as a batch gets.
        if not auth(request).startswith("extension:"):
            raise HTTPException(403, "只有扩展能报告雇主拒收。")
        if context_loader is None:
            raise HTTPException(409, "这个进程只运行练习页；真实岗位请使用 JobScan 主服务。")
        from . import store
        conn = store.connect_view(cfg)
        try:
            role = store.get_role(conn, body.job_id)
        finally:
            conn.close()
        if not role:
            raise HTTPException(404, "这个岗位不在库里。")
        evidence = body.evidence if body.evidence.startswith(REFUSED_PREFIX) else REFUSED_PREFIX + body.evidence
        try:
            # The page's own address when it sends one, so a report about another role fails the check in record_refused.
            result = record_refused(cfg, {"job_id": role["job_id"], "company": role["company"], "url": body.url or role.get("apply_url")}, evidence[:3000])
        except ValueError as exc:
            raise HTTPException(409, f"没有记下这次拒收：{exc}") from None
        return {"status": result["status"], "cooldown_until": (result.get("cooldown") or {}).get("until", "")}

    @router.post("/api/agent-lab/invalidate/{run_id}")
    def invalidate(run_id: str, request: Request):
        with lock:
            run = owned(run_id, auth(request))
            if run["status"] in {"awaiting_confirmation", "review_passed"}:
                run["status"] = "stale"
                save(run)
            return public(run)

    from .agent_runner import build_runner_router
    from .application_gatekeeper import REFUSED_PREFIX, record_expired, record_refused
    from .application_identity import prepare_casey_claim
    def record_terminal(job, outcome, evidence):
        if outcome == "expired":
            record_expired(cfg, job, evidence)
        # Only an answer to a real click closes the role and freezes the company. With no permit on
        # file nothing was sent, so those words were page text and the owner decides what they mean.
        elif outcome == "blocked" and evidence.startswith(REFUSED_PREFIX) and not never_clicked(job["job_id"]):
            record_refused(cfg, job, evidence)
    def never_clicked(job_id):
        # submit-permit writes this receipt before any click, and it survives a restart.
        return not (directory / ("submission-" + digest([job_id, "real"]) + ".json")).exists()
    def refresh_before_batch():
        # Both only read. A failed calendar read leaves the last copy, and the board says how old it is.
        form_preflight.refresh_in_background(cfg, lambda: runner_pool(cfg))
        threading.Thread(target=interview_ledger.refresh_calendar, args=(cfg,), daemon=True).start()

    runner_router = build_runner_router(cfg, auth,
        (lambda: runner_candidates(cfg)) if context_loader else None, directory / "runner",
        terminal_callback=record_terminal if context_loader else None,
        prepare_claim=(lambda job: prepare_casey_claim(cfg, job)) if context_loader else None,
        expected_extension_version=_extension_version(), never_clicked=never_clicked, runner_url=_runner_url(),
        one_per_company=True, stay_awake=bool((cfg.get("agent_runner") or {}).get("keep_awake")),
        notify=_notify if (cfg.get("agent_runner") or {}).get("keep_awake") else None,
        on_start=refresh_before_batch if form_preflight.enabled(cfg) else None, reading=form_preflight.reading,
        clicked_roles=(lambda: clicked_new_roles(cfg, directory)) if context_loader else None)
    router.include_router(runner_router)
    # This isolated queue uses fictional data, even inside the real JobScan process.
    demo_job = "fixture-" + secrets.token_hex(8)
    demo_candidates = lambda: [{"job_id": demo_job, "company": "Northstar Labs（虚构）",
        "title": "独立运行测试", "url": "http://127.0.0.1/agent-lab/fixture",
        "apply_profile": "casey", "resume_ready": True, "status": "new"}]
    router.include_router(build_runner_router(cfg, auth, demo_candidates,
        directory / "independent_demo"), prefix="/demo")
    return router


def _notify(message: str) -> None:
    """A Mac notification, so a stop or the end of a batch reaches the owner away from this page."""
    script = f"display notification {json.dumps(message, ensure_ascii=False)} with title \"JobScan 投递\""
    try:
        subprocess.Popen(["/usr/bin/osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _runner_url() -> str:
    """Chrome derives an unpacked extension's ID from its folder, so the board can link to its runner page."""
    digest_ = hashlib.sha256(str(cfgmod.ROOT / "extension").encode()).hexdigest()[:32]
    return "chrome-extension://" + "".join(chr(ord("a") + int(c, 16)) for c in digest_) + "/runner.html"


def _extension_version() -> str:
    """Version on disk; a different one reported by Chrome means it needs a reload."""
    try:
        return str(json.loads((cfgmod.ROOT / "extension" / "manifest.json").read_text())["version"])
    except (OSError, ValueError, KeyError):
        return ""


# A code step named after Submit (extension/page_gate.js, extension/runner.js). A CAPTCHA is worded differently and stays a plain stop.
VERIFICATION_STOP = re.compile(r"验证码已发送|验证步骤|申请需要验证码")
# A role in one of these was sent. Skip and expired close a role without saying that.
WENT_OUT = {"applied", "interviewing", "rejected"}


def code_changed_at() -> float:
    """When the code that decides a stop last changed. A stop older than this was judged by older code."""
    stamps = []
    for path in (cfgmod.ROOT / "extension" / "content.js", cfgmod.ROOT / "app" / "agent_review.py"):
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(stamps, default=0.0)


def _title_key(label: str, description: str | None = "") -> tuple[str, str]:
    return normalized(label), normalized(description or "")


def _without_repeated_titles(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """A title repeated on one form heads a section, like four rows of "Education History".

    One answer would be kept for every later form with that title, so those rows get no answer box.
    Two statements under one title are two questions, and each keeps its box.
    """
    flagged = Counter(_title_key(q["label"], q.get("description")) for q in rows)

    def times(q):
        return max(q.get("repeated") or 0, flagged[_title_key(q["label"], q.get("description"))])
    keep = [q for q in rows if times(q) <= 1]
    spoken = {q["label"]: times(q) for q in rows if times(q) > 1}
    return keep, [f"{label}：这个标题在表上出现了 {count} 次，答案不能记，要到申请页上手动填。" for label, count in spoken.items()]


def roles_now(cfg: dict, job_ids: list[str]) -> dict[str, dict]:
    """Each role as it is now. A row the database cannot speak for says what it said before."""
    if not job_ids:
        return {}
    from . import store
    try:
        conn = store.connect_view(cfg)
        try:
            roles = {job_id: store.get_role(conn, job_id) for job_id in job_ids}
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001  A locked database must not take the board's answer cards down.
        print(f"roles unreadable: {type(exc).__name__}", file=sys.stderr)
        return {}
    return {job_id: role for job_id, role in roles.items() if role}


def clicked_new_roles(cfg: dict, directory: Path, now: float | None = None) -> list[dict]:
    """New roles with a permit on file from the last 30 days, wherever they are. Nobody has settled what those clicks did.

    The queue's candidates leave out a form preflight will not run, a title that is not engineering and more, and she can still press
    Submit on the panel for any of them. The hold on the company has to see those too, or a sibling goes out seconds after one of them.
    """
    from . import store
    floor = (now or time.time()) - SAME_COMPANY_GAP_DAYS * 86400
    receipts = {path.name for path in directory.glob("submission-*.json") if path.stat().st_mtime >= floor}
    if not receipts:
        return []
    conn = store.connect_view(cfg)
    try:
        rows = conn.execute("SELECT r.job_id, r.company, r.title FROM jobs.roles r LEFT JOIN role_state s ON s.job_id = r.job_id "
                            "WHERE COALESCE(s.status, 'new') = 'new'").fetchall()
    finally:
        conn.close()
    return [{"job_id": row["job_id"], "company": row["company"], "title": row["title"]} for row in rows
            if "submission-" + digest([row["job_id"], "real"]) + ".json" in receipts]


def stopped_job_facts(cfg: dict, jobs: list[dict]) -> dict[str, dict]:
    """What stands between each stopped job and another run, read fresh: closed, why (it cannot run), waits (not yet), verdict.

    A job left out here is simply not known. The queue checks all of this again before anything runs.
    """
    from . import store
    try:
        pool = {job["job_id"] for job in runner_pool(cfg)}
        conn = store.connect_view(cfg)
        try:
            why_out = _queue_gate(cfg, conn)[0]
            roles = {job["job_id"]: store.get_role(conn, job["job_id"]) for job in jobs}
            gates = {job_id: why_out(role) for job_id, role in roles.items() if role and role["status"] == "new" and job_id not in pool}
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001  A locked database must not take the board's answer cards down.
        print(f"stopped job facts unreadable: {type(exc).__name__}", file=sys.stderr)
        return {}
    checked = form_preflight.enabled(cfg)
    try:
        verdicts = form_preflight.assess(cfg) if checked else {}
    except Exception:  # noqa: BLE001  The preflight card already says the rules cannot run.
        verdicts, checked = {}, False
    essays = form_preflight.allow_essays(cfg)
    facts = {}
    for job in jobs:
        job_id, role = job["job_id"], roles.get(job["job_id"])
        if role and role["status"] != "new":
            words = _refusal_on_file(role)
            facts[job_id] = {"closed": True, **({"refused": words} if words else {})}
        elif job_id not in pool:
            facts[job_id] = {"why": gates.get(job_id) or "这份现在不在自动队列里，不会自动跑。"}
        else:
            verdict = verdicts.get(job_id) or {}
            kind = verdict.get("verdict") or ("unread" if checked else None)
            fact = {"verdict": kind}
            if kind == "never":
                fact["why"] = "投不了：" + "；".join(verdict.get("blocks") or [])[:600]
            elif kind == "gone":
                fact["why"] = "这个岗位的表单已经读不到了，多半下架了。"
            elif kind == "ask":
                fact["waits"] = "开跑前检查说这张表还有题没答，在下面「开跑前要你答的题」。答完才能重跑。"
            elif kind == "essay" and not essays:
                fact["waits"] = "这张表有短答题。打开下面「由模型写」那个开关，才能重跑。"
            facts[job_id] = fact
    return facts


def _refusal_on_file(role: dict) -> str:
    """The employer's words, when this role was closed by a refusal. application_gatekeeper writes them into the notes."""
    found = re.search(r"雇主拒收（[^）]*）：([\s\S]*)$", role.get("notes") or "") if role.get("status") == "skip" else None
    return found[1] if found else ""


def next_batch_places(candidates: list[dict], records: list[dict], never_clicked, now: float | None = None, clicked_roles=()) -> dict[str, int]:
    """Where each stopped job lands in the next batch, walking the queue the way a claim does.

    A batch takes one role per company and skips a company an application may already have reached.
    """
    floor = (now or time.time()) - SAME_COMPANY_GAP_DAYS * 86400
    ledger = {r["job"]["job_id"]: r for r in records}
    taken = {cfgmod.norm_key(r["job"]["company"]) for r in records if (r.get("finished_at") or floor) >= floor and
             (r["status"] in {"submitted", "unknown"} or not never_clicked(r["job"]["job_id"]))}
    taken |= {cfgmod.norm_key(job["company"]) for job in [*candidates, *clicked_roles] if not never_clicked(job["job_id"])}
    places, slot = {}, 0
    for job in candidates:
        company, record = cfgmod.norm_key(job["company"]), ledger.get(job["job_id"])
        if company in taken or (record and record["status"] not in RETRYABLE):
            continue
        slot += 1
        taken.add(company)
        if record:
            places[job["job_id"]] = slot
    return places


# Boards whose real form the executor has been verified to read end to end. Greenhouse and Lever
# stay out until they are: their selects are not readable yet, and both can demand a code or a
# CAPTCHA after Submit.
GREENHOUSE_HOSTS = {"job-boards.greenhouse.io", "boards.greenhouse.io"}
# Greenhouse joined on 2026-11-08 after dry runs on its real forms (tools/runner_dry_batch.py --board greenhouse).
RUNNER_HOSTS = {"jobs.ashbyhq.com"} | GREENHOUSE_HOSTS
# A board with its own careers site sends /<board>/jobs/<id> there (Block → block.xyz), where the runner cannot go.
# The embed page shows the same form on Greenhouse itself.
GREENHOUSE_JOB_PATH = re.compile(r"^/([^/]+)/jobs/(\d+)/?$")
# Filler and Reviewer know engineering forms; a coordinator or policy role is not worth a slot.
ABROAD_TITLE = re.compile(r"\b(?:canada|toronto|vancouver|montreal|uk|united kingdom|london|ireland|dublin|emea|apac|latam|europe|germany|berlin|france|paris|netherlands|amsterdam|spain|poland|india|bangalore|bengaluru|hyderabad|singapore|japan|tokyo|australia|sydney|brazil|mexico|israel|tel aviv)\b", re.I)
ENGINEERING_TITLE = re.compile(r"\bengineers?\b|\bdeveloper\b|\bswe\b|\bsde\b|programmer|\bsre\b|devops", re.I)


def runner_candidates(cfg: dict) -> list[dict]:
    """The pool, less every form the preflight says would stop or has not read."""
    pool = runner_pool(cfg)
    if not form_preflight.enabled(cfg):
        return pool
    try:
        verdicts = form_preflight.assess(cfg)
    except Exception:  # A form nobody has judged must not run.
        return []
    essays = form_preflight.allow_essays(cfg)
    return [job for job in pool if form_preflight.runnable(cfg, verdicts.get(job["job_id"]), essays)]


def _queue_gate(cfg: dict, conn) -> tuple:
    """One rule for each way a role stays out of the queue, so the queue and the board give the same reason.

    Returns why_out(row), which is "" when the role may go, and the last applied date of each company.
    """
    from . import store
    from .application_identity import companies_under_other_cards, routed_elsewhere
    from .scoring import location_kind
    other_cards = companies_under_other_cards(conn)
    cooled = store.active_cooldowns(conn, cfg)
    interviewing = interview_ledger.blocked_keys(cfg, conn)
    applied = store.last_applied_by_company(conn)
    gap_floor = (datetime.date.today() - datetime.timedelta(days=SAME_COMPANY_GAP_DAYS)).isoformat()

    def why_out(row: dict) -> str:
        company = (row.get("company_norm") or "").lower()
        # A technical interview is under way here, and a new application would only confuse it.
        if interview_ledger.is_blocked(company, interviewing):
            return "正在技术面，这家公司先不投。想投就到下面「正在面的公司」放行。"
        if company in cooled:
            until = (cooled[company] or {}).get("until")
            return "这家公司在冷却期" + (f"，到 {until} 才能再投。" if until else "。")
        if routed_elsewhere(cfg, row) or company in other_cards:
            return "这家公司用的是另一张名片，不走自动投递。"
        # Applying again this soon is what Close refused; a date we cannot read counts as old.
        last = str(applied.get(company) or "")
        if re.match(r"\d{4}-", last) and last >= gap_floor:
            return f"{last[:10]} 刚投过这家公司，隔 {SAME_COMPANY_GAP_DAYS} 天才能再投。"
        if not ENGINEERING_TITLE.search(row.get("title") or ""):
            return "职位名不是工程岗，不在自动队列里。"
        # A same-title role at this company is already applied/closed: likely the same posting.
        if row.get("badge_ref"):
            return "这家公司有同名岗位已经投过或关了，多半是同一个岗位。"
        # The owner will not leave the Bay Area, so an automatic application stays where no move is needed.
        if not location_kind(row.get("location"), row.get("is_remote")):
            return "地点不在湾区，也不是远程。"
        # A "remote" row can still name another country in its title (Grafana Labs "... | Canada", 2026-11-08).
        if ABROAD_TITLE.search(row.get("title") or ""):
            return "职位名写着美国以外的地点。"
        if row.get("tailor_status", "ready") != "ready" or not Path(row.get("tailored_resume_path") or "").is_file():
            return "定制简历还没好，不在自动队列里。"
        parsed = urlparse(row.get("apply_url") or row.get("url") or "")
        if parsed.scheme != "https" or parsed.hostname not in RUNNER_HOSTS:
            return "这个招聘网站还不能自动填。"
        return ""
    return why_out, applied


def runner_pool(cfg: dict) -> list[dict]:
    """Ready, no-account Review roles across cards; prepare Casey only after leasing."""
    from . import store
    conn = store.connect_view(cfg)
    try:
        # No "easy" filter: it reads the feed a role came from, so it hides every Ashby form LinkedIn found.
        # The host check in _queue_gate is what decides whether the runner can fill the form.
        rows = store.fetch_view(conn, cfg, status="new", seen="all", identity="",
                                tailored="ready", pace=False)["rows"]
        why_out, applied = _queue_gate(cfg, conn)
        result = []
        for row in rows:
            if why_out(row):
                continue
            company = (row.get("company_norm") or "").lower()
            url = row.get("apply_url") or row.get("url") or ""
            parsed = urlparse(url)
            if parsed.hostname == "jobs.ashbyhq.com" and not parsed.path.rstrip("/").endswith("/application"):
                url = parsed._replace(path=parsed.path.rstrip("/") + "/application").geturl()
            board = GREENHOUSE_JOB_PATH.match(parsed.path) if parsed.hostname in GREENHOUSE_HOSTS & RUNNER_HOSTS else None
            if board:
                url = f"https://job-boards.greenhouse.io/embed/job_app?for={board[1]}&token={board[2]}"
            result.append({"job_id": row["job_id"], "company": row["company"], "title": row["title"],
                "url": url, "apply_profile": "casey", "resume_ready": True, "status": "new",
                "selection_reason": _selection_reason(row, applied.get(company))})
        result = _one_row_per_url(conn, result)
        # A company that never heard from us comes first; the feed's order holds within each group.
        result.sort(key=lambda job: cfgmod.norm_key(job["company"]) in applied)
        return result
    finally:
        conn.close()


def _one_row_per_url(conn, jobs: list[dict]) -> list[dict]:
    """Several feeds can list one Ashby posting, and the page resolves to only one of those rows.

    Any other row would stop on "the page is a different role" before filling, which wastes the company's slot in the batch.
    """
    from .application_gatekeeper import _role_url
    groups: dict[tuple, list[dict]] = {}
    for job in jobs:
        groups.setdefault(_role_url(job["url"]), []).append(job)
    keep = []
    for rows in groups.values():
        if len(rows) == 1:
            keep.extend(rows)
            continue
        from . import manual, store
        from .web import url_candidates
        url = rows[0]["url"]
        role = store.lookup_role_by_ids(conn, url_candidates(url)) or store.lookup_role_by_apply_url(conn, [url, manual.canonical_capture_url(url)])
        # The page resolves to a row this pool left out, or to none: every row here would stop, so none runs.
        keep.extend(job for job in rows if role and job["job_id"] == role["job_id"])
    return keep


def _selection_reason(row: dict, last_applied: str | None = None) -> str:
    """Why the Gatekeeper may take this role, from the row's own values."""
    scores = [f"岗位分 {row['weighted_score']:.0f}" if isinstance(row.get("weighted_score"), (int, float)) else "",
              f"简历匹配 {row['resume_score']}" if row.get("resume_score") is not None else "",
              f"判定 {row['verdict']}" if row.get("verdict") else ""]
    history = f"上次投这家公司是 {str(last_applied)[:10]}" if last_applied else "这家公司从没投过"
    return (f"未投递；{history}；定制简历已就绪且文件存在；单页表单、无需账号；地点在湾区或远程；无冷却、无同名岗位记录、"
            "公司未指定其他名片、没有进行中的技术面；" + " · ".join(s for s in scores if s) + "；从没投过的公司排在最前。")


# The owner's standing answers behind the extension's commonAnswer rules (extension/content.js).
# Reviewer must not flag them as invented, and must not stretch them to other topics.
STANDING_ANSWERS = (
    "The candidate's own standing answers, confirmed for every application: "
    "how or where did you hear about this job = Job board; "
    "ever worked at or for this company before = No; "
    "employed by or affiliated with a university, government or public-sector body = No; "
    "citizen or resident of a sanctioned or export-controlled country = No; "
    "related to an employee of this company = No; "
    "may we contact your current employer = No; "
    "marketing, newsletter or job-alert opt-ins = No; "
    "interview recording = opt out; "
    "at least 18 years old = Yes; "
    "any working arrangement inside the San Francisco Bay Area (remote, hybrid, on-site, any number of days "
    "in the office, commuting, moving within the Bay Area, on-call) = Yes; "
    "generally willing to travel for work within the US = Yes; "
    "uses or has experience with this company's own product = Yes. "
    "These do not cover working outside the Bay Area, international travel, whether anything prevents "
    "travel, immigration proceedings, agreements with other employers, or criminal history."
)


def real_context(cfg: dict, job_id: str, profile_key: str, url: str) -> dict:
    """Reuse JobScan matching without mutating identity or re-rendering resumes."""
    from . import ai, store, tailoring, manual
    from .web import view_conn, url_candidates, _profile_for_role, outbound_pdf_name
    gen = view_conn()
    conn = next(gen)
    try:
        role = store.lookup_role_by_ids(conn, url_candidates(url))
        if role is None:
            role = store.lookup_role_by_apply_url(conn, [url, manual.canonical_capture_url(url)])
        if not role or role["job_id"] != job_id:
            raise HTTPException(409, "页面无法明确匹配这个岗位，不能依靠公司猜测进行审查。")
        from .application_gatekeeper import _role_url
        try:
            same_target = _role_url(url) == _role_url(role.get("apply_url") or role.get("url"))
        except ValueError:
            same_target = False
        if not same_target:
            raise HTTPException(409, "页面域名或岗位地址已改变，不能沿用原岗位的审核与提交许可。")
        effective = store.effective_profile_key(cfg, role) or ""
        if effective != profile_key:
            raise HTTPException(409, "申请身份已改变，请刷新页面后重试。")
        profile = _profile_for_role(role)
        base = tailoring.load_base(cfg)
        facts = []
        for key, labels in [("first_name", ["First name"]), ("last_name", ["Last name"]),
                            ("email", ["Email", "Email address"]), ("phone", ["Phone", "Phone number"])]:
            if profile.get(key):
                facts.append({"id": key, "labels": labels, "value": str(profile[key])})
        # Facts belong to the selected workspace; never copy the owner's work status to another candidate.
        for key, labels in [("work_authorized", ["Are you authorized to work in the United States?"]),
                            ("needs_sponsorship", ["Will you now or in the future require sponsorship?"])]:
            if isinstance(profile.get(key), bool):
                facts.append({"id": key, "labels": labels, "value": "Yes" if profile[key] else "No"})
        for key in ["legal_first_name", "preferred_name", "location", "country", "linkedin", "github",
                    "education", "years_of_experience", "relocation", "ethnicity", "veteran_status", "disability_status"]:
            if profile.get(key) is not None:
                facts.append({"id": key, "labels": [], "value": str(profile[key])})
        facts.extend([
            {"id": "resume-education", "labels": [], "value": json.dumps(base.get("education", []), ensure_ascii=False)},
            {"id": "resume-experience", "labels": [], "value": json.dumps(base.get("experience", []), ensure_ascii=False)},
            {"id": "resume-projects", "labels": [], "value": json.dumps(base.get("projects", []), ensure_ascii=False)},
            # The same confirmed facts Filler drafts from, so truthful prose is not flagged.
            {"id": "candidate-facts", "labels": [], "value": ai._facts(cfg, role)},
            {"id": "standing-answers", "labels": [], "value": STANDING_ANSWERS},
        ])
        confirmed = profile.get("confirmed_answers") or []
        facts.extend({"id": a["id"], "labels": [], "value": confirmed_answers.fact_text(a)} for a in confirmed)
        from .resume_evidence import inspect_resume
        expected_resume = None
        if role.get("tailor_status") == "ready" and role.get("tailored_resume_path"):
            expected_resume = inspect_resume(Path(role["tailored_resume_path"]),
                outbound_pdf_name(role["tailored_resume_path"]), profile)
        if expected_resume:
            facts.append({"id":"resume-uploaded", "labels":[], "value":expected_resume["text"]})
        cooldown = store.active_cooldowns(conn, cfg).get((role.get("company_norm") or "").lower())
        blocker = "公司仍在投递冷却期内。" if cooldown else ""
        if role.get("tailor_status") != "ready" or not expected_resume or not expected_resume["identity_verified"]:
            blocker = "当前定制简历的身份、联系方式或文件内容尚未核对通过。"
        from .application_memory import memory_pack
        return {"job_id": job_id, "company": role["company"], "title": role["title"],
                "apply_profile": effective, "facts": facts, "confirmed_answers": confirmed, "requires_attachment_review": True,
                "job_description": role.get("jd_text") or "",
                "expected_resume": expected_resume, "submission_blocker":blocker,
                "application_status": role.get("status"), "shared_memory": memory_pack(profile, base)}
    finally:
        gen.close()


def create_demo_app():
    app = FastAPI(title="JobScan Agent Lab")
    app.include_router(build_router(cfgmod.load_config()))
    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_demo_app(), host="127.0.0.1", port=int(os.getenv("JOBSCAN_LAB_PORT", "8793")))
