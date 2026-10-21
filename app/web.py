"""Local review UI + JSON API. Writes ONLY to user_state.db.
Usage: python -m app.web  →  http://127.0.0.1:8787"""
from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import ai as aimod
from . import company_board as boardmod
from . import config as cfgmod
from . import confirmed_answers
from . import followup as followupmod
from . import gcal as gcalmod
from . import google_auth as gauthmod
from . import heartbeats as hbmod
from . import ideas as ideasmod
from . import ingest as ingestmod
from . import pace as pacemod
from . import mail as sendmail
from . import mailcode as mailmod
from . import outreach as outreachmod
from . import recite as recitemod
from . import recite_asr
from . import store, tailoring
from .platform import router as platform_router

cfg = cfgmod.load_config()
# A workspace keeps its own autofill answer log (config `answers_log`); the owner's default
# stays data/logs/answers.jsonl. answer_log has no cfg in hand, so the path is set here once.
if cfg.get("answers_log"):
    from . import answer_log as _alog
    _alog.LOG_REL = cfg["answers_log"]
app = FastAPI(title="JobScan")
# The browser extension's service worker calls this local API. Employer pages share that browser
# and can reach localhost, so only the extension and pages on this machine may read the answers.
app.add_middleware(
    CORSMiddleware, allow_origin_regex=r"^(chrome-extension://[a-p]{32}|http://(127\.0\.0\.1|localhost)(:\d+)?)$",
    allow_methods=["*"], allow_headers=["*"],
)
app.include_router(platform_router)

# Local fill/review workbench; no final submission or business-status writes.
from .agent_lab import build_router as build_agent_router, real_context as agent_context
app.include_router(build_agent_router(cfg, lambda job_id, identity, url: agent_context(cfg, job_id, identity, url)))


@app.middleware("http")
async def refuse_cross_site_writes(request: Request, call_next):
    """Any web page can POST at this loopback service with no-cors: no body, no Origin it can be judged by.

    A route with no body to validate would just run, as /api/google/auth/revoke did. A page cannot hide Sec-Fetch-Site.
    The extension declares itself by its Origin; curl and the service's own tools send no such header at all.
    """
    if (request.method not in {"GET", "HEAD", "OPTIONS"} and os.environ.get("JOBSCAN_SAAS_ONLY") != "1"
            and request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}
            and not request.headers.get("origin", "").startswith("chrome-extension://")):
        return JSONResponse({"detail": "This service only takes changes from its own pages and the extension."}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def cloud_pilot_surface(request: Request, call_next):
    """Cloud pilot exposes only the tenant control plane.

    The legacy owner UI remains available locally, but its routes were designed for a trusted
    loopback process and must never become an accidental public API on Cloud Run.
    """
    if os.environ.get("JOBSCAN_SAAS_ONLY") == "1":
        path = request.url.path
        allowed = path.startswith("/portal") or path.startswith("/static/") or path == "/api/platform/health"
        if not allowed:
            return RedirectResponse("/portal", status_code=307)
    return await call_next(request)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def url_candidates(url: str) -> list[str]:
    """Raw ATS ids that might appear in an application-page URL."""
    c: list[str] = []
    for pat in (r"gh_jid=(\d+)", r"[?&]token=(\d+)", r"/jobs/(\d+)",
                # LinkedIn puts a path segment between /jobs/ and the id, so the plain
                # `/jobs/(\d+)` above never fired and every li: role was invisible to the
                # extension: /jobs/view/<id>/, and the id also rides as a query param on
                # the search/collections pages where the JD opens in a side panel.
                r"/jobs/view/(\d+)", r"[?&]currentJobId=(\d+)",
                r"[?&]pid=(\d+)",                       # Microsoft careers: apply?pid=1970393556941507
                # Gem-hosted boards: jobs.gem.com/<company>/<id> — bare trailing number, and the
                # ids are Greenhouse-shaped when the company migrated (Retool, 2026-08-01)
                r"gem\.com/[^/?#]+/(\d{6,})",
                # Apple: details page /en-us/details/200673880-0836/<slug> and apply flow
                # /app/en-us/apply/200673880-0836 share the req id — the join between them.
                r"/(?:details|apply)/(\d{6,}(?:-\d{2,5})?)",
                # Workday req ids: .../Software-Engineer-II_R-279240, .../job/R-273932/...,
                # .../Staff-Software-Engineer_REF078668W  → job_id ends ':R-279240'
                r"[_/]([A-Z]{1,4}-?\d{3,}[A-Za-z0-9]*)",
                # …and the ones that lead with DIGITS, which the letters-first pattern above
                # cannot see at all: Autodesk stamps the fiscal year on the front
                # (…_26WD99399-1 → wd:autodesk:wd1:Ext:26WD99399). That blind spot cost 78
                # straight lookup 404s on 2026-08-09 for a role that WAS in the queue — the
                # panel never opened, so a scanned role with a tailored PDF got filled by
                # hand. The trailing -1/-3 is Workday's repost counter, never part of the
                # stored id, so stop before it.
                r"[_/](\d{2,4}[A-Z]{1,4}\d{3,})"):
        c += re.findall(pat, url)
    c += _UUID.findall(url)
    seen, out = set(), []
    for x in c:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], 1)}


def _parse_dates(s: str) -> dict:
    """'April 2025 - Present' → start 04/2025 + current; 'Jan 2024 - Apr 2025' → both ends."""
    out = {"start_month": "", "start_year": "", "end_month": "", "end_year": "", "current": False}
    parts = re.split(r"\s*[-–—]\s*", s or "")

    def one(t: str) -> tuple[str, str]:
        m = re.search(r"([A-Za-z]+)\s+(\d{4})", t or "")
        if not m:
            return ("", "")
        num = _MONTHS.get(m.group(1).lower()[:3] and
                          next((k for k in _MONTHS if k.startswith(m.group(1).lower()[:3])), ""), 0)
        return (f"{num:02d}" if num else "", m.group(2))

    if parts:
        out["start_month"], out["start_year"] = one(parts[0])
    if len(parts) > 1:
        if re.search(r"present|current", parts[1], re.I):
            out["current"] = True
        else:
            out["end_month"], out["end_year"] = one(parts[1])
    return out


def profile_skills(role: dict | None = None) -> list[str]:
    """Flat skill list for Workday's "Type to Add Skills". Prefer this role's TAILORED résumé
    skills (already JD-aligned by the tailoring pass); fall back to the base skill pool."""
    import yaml  # local: only needed here
    try:
        base = tailoring.load_base(cfg)
    except Exception:
        return []
    if role:
        try:
            p = tailoring.resume_yaml_path(cfg, role["company"], role["job_id"])
            if p.exists():
                t = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                out = [s for g in (t.get("skills") or []) for s in (g.get("items") or [])]
                if out:
                    return out
        except Exception:
            pass
    return [s for items in (base.get("skills_master") or {}).values() for s in items]


def profile_experience(role: dict | None = None) -> list[dict]:
    """Work history for the extension's Workday filler, from the LOCKED base résumé.
    `bullets` feed Workday's "Role Description" box. When this role already has a tailored
    résumé, use ITS bullets so the typed-in history matches the PDF being attached;
    otherwise fall back to the base (ground-truth) bullets."""
    import yaml  # local: only needed here
    try:
        base = tailoring.load_base(cfg)
    except Exception:
        return []
    tailored: dict[str, list[str]] = {}
    if role:
        try:
            p = tailoring.resume_yaml_path(cfg, role["company"], role["job_id"])
            if p.exists():
                t = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                tailored = {j.get("company"): (j.get("bullets") or [])
                            for j in (t.get("experience") or [])}
        except Exception:
            tailored = {}
    out = []
    for j in (base.get("experience") or []):
        co = j.get("company", "")
        out.append({
            "company": co, "title": j.get("title", ""), "location": j.get("location", ""),
            "bullets": tailored.get(co) or j.get("bullets_master") or [],
            **_parse_dates(j.get("dates", "")),
        })
    return out
app.mount("/static", StaticFiles(directory=str(cfgmod.ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(cfgmod.ROOT / "templates"))


def _asset_v() -> int:
    """Cache-buster for /static assets: newest mtime of app.js/style.css. Chrome held a
    long-lived tab's cached app.js across a deploy (2026-09-09: the 身份开关 rendered but its
    click handler lived in the stale JS), so templates append ?v=<mtime> to every asset URL."""
    d = cfgmod.ROOT / "static"
    return int(max((d / f).stat().st_mtime for f in ("app.js", "style.css")))


templates.env.globals["asset_v"] = _asset_v

store.seed_applied_companies(cfg)  # one-way merge of config's applied_companies list

_gcal_auth_lock = threading.Lock()
_gcal_auth_proc: subprocess.Popen | None = None
_gcal_auth_started = 0.0
_gcal_auth_log = cfgmod.ROOT / "logs" / "gcal_auth_ui.log"


_schema_ready: set[str] = set()   # user-DB paths whose schema + migrations have been applied


def view_conn():
    """One fresh connection per request, usable from whichever worker thread gets it.

    check_same_thread=False is not laziness. FastAPI submits a sync generator dependency's
    setup, the endpoint body, and the `finally` teardown to the anyio worker pool as three
    SEPARATE jobs, and the pool makes no promise they land on the same thread. sqlite3's
    default same-thread guard then raises ProgrammingError partway through a request that was
    otherwise fine. web.log recorded 11 of these on 2026-08-09, and one of them ate a real
    application: POST /api/roles/ab:illumio:4d98de4e…/status → 500 at 18:59, so a form she
    had already submitted was never marked applied and the role sat in the queue.

    Safe because the sharing is sequential, not concurrent: the connection belongs to exactly
    one request, only one of those three jobs runs at a time, and anyio's handoff orders the
    memory between them. The guard was protecting nothing here and only breaking writes.

    The schema bootstrap + migrations still go through store.connect_view, but once per DB
    file instead of on every request — they are idempotent DDL, not per-request work. Keyed
    by path rather than done at import because the tests monkeypatch `cfg` onto throwaway
    DBs after this module is loaded, and those need bootstrapping too.
    """
    user_db = str(cfgmod.user_db_path(cfg))
    if user_db not in _schema_ready:
        store.connect_view(cfg).close()
        _schema_ready.add(user_db)
    conn = sqlite3.connect(user_db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")   # same as store._connect — see the note there
    conn.execute("ATTACH DATABASE ? AS jobs", (str(cfgmod.db_path(cfg)),))
    try:
        yield conn
    finally:
        conn.close()


def resume_variants() -> list[str]:
    return ["base"] + sorted((cfg.get("resume_angles") or {}).keys())


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    status: str = "new",
    q: str = "",
    company: str = "",
    min_score: str = "",   # str, not float: an empty <input type="number"> submits "", which 422s a float param
    verdict: str = "",
    seen: str = "all",
    remote: str = "",
    seniority: str = "",
    tag: str = "",         # onsite / startup / onsite_or_startup — company flags from company_notes.yaml
    tailored: str = "",    # '' all · 'ready' has a validated résumé PDF · 'none' needs one
    ease: str = "",        # '' all · 'easy' one-page no-account boards · 'account' Workday/Avature wizards
    # 投递身份「登录」：URL 显式给了就听 URL 的（?identity= 空串 = 明确要看全部）；
    # 没给（None）就读 cookie —— 顶栏开关切一次全站生效、刷新不丢，像登录邮箱。
    identity: str | None = None,
    # 'diversity' = keep select_daily's company interleave (each company's lead role first,
    # then everyone's #2, …). Default in the review tab because it is the order she works
    # top-down in; the other options re-sort those same rows.
    sort: str = "diversity",
    rot: str = "",
    # 投递节奏（2026-09-28）：review 页默认只显示今天冻结的 N 家；?showall=1 = 这一次看全部
    showall: str = "",
    conn=Depends(view_conn),
):
    if identity is None:
        identity = request.cookies.get("apply_identity", "")
    try:
        ms = float(min_score)
    except (TypeError, ValueError):
        ms = 0.0
    # Rotation cursor for the review queue. Lives ONLY in the URL — nothing is stored, so
    # refresh/back never move the page and there is no frozen "today's batch" to go stale.
    try:
        rot_i = max(0, int(rot))
    except (TypeError, ValueError):
        rot_i = 0
    data = store.fetch_view(
        conn, cfg, status=status, q=q, company=company, min_score=ms,
        verdict=verdict, seen=seen, remote=remote, seniority=seniority, sort=sort,
        rot=rot_i, tag=tag, identity=identity, tailored=tailored, ease=ease,
        pace=not showall,
    )
    if showall and status == "new":
        # fetch_view skipped pace on purpose; the banner still needs the level so it can offer
        # the way back to today's list
        data["pace"] = {"pace": pacemod.effective(conn, cfg)}
    params = {
        "status": status, "q": q, "company": company, "min_score": min_score if ms else "",
        "verdict": verdict, "seen": seen, "remote": remote, "seniority": seniority,
        "tag": tag, "sort": sort, "rot": rot_i or "", "identity": identity,
        "tailored": tailored, "ease": ease, "showall": "1" if showall else "",
    }
    return templates.TemplateResponse(request, "list.html", {
        **data,
        "params": params,
        "statuses": store.STATUSES,
        "resume_variants": resume_variants(),
        "apply_profiles": cfg.get("apply_profiles") or {},
    })


class StatusIn(BaseModel):
    status: str
    resume_variant: str | None = None


class NotesIn(BaseModel):
    notes: str


class CompanyIn(BaseModel):
    name: str


class QueueIn(BaseModel):
    angle: str | None = None


@app.post("/api/roles/{job_id}/status")
def api_set_status(job_id: str, body: StatusIn, conn=Depends(view_conn)):
    if body.status not in store.STATUSES:
        raise HTTPException(400, f"invalid status {body.status!r}")
    role = store.get_role(conn, job_id)
    if not role:
        raise HTTPException(404, "unknown job_id")
    if body.status == "applied" and not role.get("apply_profile"):
        # Freeze WHICH name card this application went out under (owner 2026-09-25). Until now
        # the company-level 小号 map was only consulted while a role was still open — once it
        # flipped to applied, effective_profile_key fell back to the default card and the record
        # said "Casey" for applications that were filled as Alex. 672 applied rows carried no
        # card at all. The effective card at the moment of applying is the historical truth.
        store.upsert_state(conn, job_id, apply_profile=store.effective_profile_key(cfg, role))
    state = store.set_status(conn, job_id, body.status, body.resume_variant)
    return {"ok": True, "state": state}


class IdentityIn(BaseModel):
    identity: str


@app.post("/api/identity")
def api_set_identity(body: IdentityIn):
    """全局身份开关（顶栏）：把当前投递身份写进 cookie，之后每个页面都按它过滤。
    空串 = 退出登录、看全部。"""
    profiles = (cfg.get("apply_profiles") or {}).get("profiles") or {}
    if body.identity and body.identity not in profiles:
        raise HTTPException(400, f"unknown identity {body.identity!r}")
    resp = JSONResponse({"ok": True, "identity": body.identity})
    if body.identity:
        resp.set_cookie("apply_identity", body.identity, max_age=365 * 24 * 3600)
    else:
        resp.delete_cookie("apply_identity")
    return resp


class ApplyProfileIn(BaseModel):
    profile: str


@app.post("/api/roles/{job_id}/apply-profile")
def api_set_apply_profile(job_id: str, body: ApplyProfileIn, conn=Depends(view_conn)):
    """Switch which 投递身份 (name card) this role is applied with. If the résumé is already
    tailored, re-ingest it on the spot: content untouched, but the PDF is re-rendered with
    the new header name/email/phone and re-named accordingly (秒级, no LLM)."""
    ap = cfg.get("apply_profiles") or {}
    if body.profile not in (ap.get("profiles") or {}):
        raise HTTPException(400, f"unknown apply profile {body.profile!r}")
    role = store.get_role(conn, job_id)
    if not role:
        raise HTTPException(404, "unknown job_id")
    # 已投出/流程中的岗位锁死：申请早已用当时的抬头发出，事后换身份只会让本地 PDF 和
    # 真实提交对不上（发现于 2026-09-08：切换测试恰好撞上 owner 刚点 applied 的岗位）。
    # 真要换：先把状态改回 new（显式动作，留下痕迹），再切。
    if (role.get("status") or "new") in ("applied", "interviewing"):
        raise HTTPException(409, f"role is {role['status']} — the application already went out "
                            "under the old identity; set it back to 'new' first if you must")
    state = store.upsert_state(conn, job_id, apply_profile=body.profile)
    rerendered = False
    ypath = tailoring.resume_yaml_path(cfg, role["company"], job_id)
    if ypath.exists():
        try:
            tailoring.ingest_file(cfg, conn, ypath)
            rerendered = True
        except tailoring.TailorError as e:
            # the identity switch is saved either way; a YAML that no longer validates
            # will be regenerated by the daily autotailor and pick the new identity up then
            return {"ok": True, "state": state, "rerendered": False, "note": str(e)}
    return {"ok": True, "state": state, "rerendered": rerendered}


@app.post("/api/roles/{job_id}/notes")
def api_set_notes(job_id: str, body: NotesIn, conn=Depends(view_conn)):
    if not store.get_role(conn, job_id):
        raise HTTPException(404, "unknown job_id")
    state = store.set_notes(conn, job_id, body.notes)
    return {"ok": True, "state": state}


@app.get("/api/roles/{job_id}")
def api_get_role(job_id: str, conn=Depends(view_conn)):
    role = store.get_role(conn, job_id)
    if not role:
        raise HTTPException(404, "unknown job_id")
    return role


@app.post("/api/roles/{job_id}/queue-tailoring")
def api_queue_tailoring(job_id: str, body: QueueIn, conn=Depends(view_conn)):
    if not store.get_role(conn, job_id):
        raise HTTPException(404, "unknown job_id")
    try:
        path = tailoring.write_prompt(cfg, conn, job_id, body.angle)
    except FileNotFoundError:
        raise HTTPException(500, f"base résumé missing ({tailoring.base_path(cfg)})")
    run = _start_queued(conn, "✎ queue")
    return {"ok": True, "prompt_path": str(path), **run,
            "hint": ("生成已开始（后台 claude，约 2-3 分钟一份，完成自动入库）" if run.get("started")
                     else "已排队；正在跑的批次结束后自动开始" if run.get("waiting")
                     else "prompt written")}


@app.post("/api/ingest")
def api_ingest(conn=Depends(view_conn)):
    seen: dict = {}
    n = ingestmod._scan_once(cfg, seen, force=True)
    return {"ok": True, "ingested": n}


# ---------- one-click batch tailoring (owner 2026-09-23: 「不需要一个一个地去点」) ----------
# Same engine as the nightly run — tools/auto_tailor.sh → 4 headless claude workers (Max
# subscription, no API key) → ONE ingest pass — but the manifest is built HERE from the review
# page's current filters, and the script is told to use it (env JOBSCAN_MANIFEST) instead of
# running prep_autotailor.py. Lock / log / manifest are per workspace, same suffix rule as the
# script, so two candidates' batches never collide.
_AUTOTAILOR_LOGS = cfgmod.ROOT / "data" / "logs"   # module-level so tests can aim it at a tmp dir
_AUTOTAILOR_DEFAULT_MANIFEST = "scratch_priors/autotailor_manifest.json"


class _TailorBatchIn(BaseModel):
    """Mirror of index()'s filter params: the button posts the page's query string as-is, so
    the batch is exactly the rows she is looking at. Strings rather than numbers for the same
    reason as index() — an empty <input type=number> submits ''."""
    status: str = "new"
    q: str = ""
    company: str = ""
    min_score: str = ""
    verdict: str = ""
    seen: str = "all"
    remote: str = ""
    seniority: str = ""
    tag: str = ""
    identity: str | None = None
    tailored: str = ""
    ease: str = ""
    sort: str = "diversity"
    rot: str = ""
    showall: str = ""        # mirror of index(): '' = the pace-limited page, '1' = 看全部
    dry_run: bool = False


def _workspace_suffix() -> str:
    """JOBSCAN_WS for this process: 'robin' under config.robin.yaml, '' for the owner.
    Same rule as tools/daily_scan.sh, which derives it from the config FILENAME. The display
    name (workspace.name: Taylor) is trusted only when config.<name>.yaml really exists — it
    usually does not match the file stem — otherwise fall back to $JOBSCAN_CONFIG's name."""
    name = cfgmod.workspace_name(cfg).lower()
    if name and (cfgmod.ROOT / f"config.{name}.yaml").exists():
        return name
    m = re.fullmatch(r"config\.(.+)\.yaml", Path(os.environ.get(cfgmod.ENV_CONFIG) or "").name)
    return m.group(1) if m else ""


def _tailor_batch_paths() -> dict:
    """Where a manual batch lives. The lock is THE SAME directory auto_tailor.sh takes, so a
    manual batch and the nightly one can never run claude workers over the same YAMLs."""
    ws = _workspace_suffix()
    sfx = f".{ws}" if ws else ""
    nightly = str((cfg.get("auto_tailor") or {}).get("manifest") or _AUTOTAILOR_DEFAULT_MANIFEST)
    manual = nightly[:-5] + ".manual.json" if nightly.endswith(".json") else nightly + ".manual.json"
    return {
        "ws": ws,
        "manifest": cfgmod.resolve_path(manual),
        "lock": _AUTOTAILOR_LOGS / f".autotailor{sfx}.lock",
        "log": _AUTOTAILOR_LOGS / f"autotailor{sfx}.manual.log",
    }


def _launch_manifest(manifest: list[dict], note: str) -> dict:
    """Write `manifest` as this workspace's .manual.json and hand it to tools/auto_tailor.sh
    detached (JOBSCAN_MANIFEST override; the child inherits JOBSCAN_CONFIG so it stays in this
    workspace). Returns {manifest, started}. Callers check the lock BEFORE writing prompts."""
    paths = _tailor_batch_paths()
    paths["manifest"].parent.mkdir(parents=True, exist_ok=True)
    paths["manifest"].write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    res = {"manifest": str(paths["manifest"]), "started": False}
    if not manifest or paths["lock"].exists():
        return res
    env = os.environ.copy()          # carries JOBSCAN_CONFIG → the child stays in THIS workspace
    env["JOBSCAN_MANIFEST"] = str(paths["manifest"])
    if paths["ws"]:
        env["JOBSCAN_WS"] = paths["ws"]
    else:
        env.pop("JOBSCAN_WS", None)  # owner: no suffix, exactly like the launchd nightly
    # launchd gives the web server a bare PATH; the script needs sed/env/bash from the usual places
    env["PATH"] = env.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    paths["log"].parent.mkdir(parents=True, exist_ok=True)
    with open(paths["log"], "ab") as lf:
        lf.write(("\n===== %s %s (ws=%s) =====\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), note, paths["ws"] or "owner")).encode())
        lf.flush()
        subprocess.Popen(["bash", "tools/auto_tailor.sh"], cwd=str(cfgmod.ROOT), env=env,
                         stdout=lf, stderr=lf, start_new_session=True)
    res["started"] = True
    return res


_queue_attempts: dict[str, int] = {}


def _queued_manifest(conn) -> list[dict]:
    """Every role in this workspace at tailor_status=queued (prompt written, no résumé yet) as
    manifest items — what the ✎ button and the queue runner hand to the workers."""
    rows = conn.execute(
        "SELECT s.job_id, s.prompt_path, r.company, r.title, r.weighted_score"
        " FROM role_state s JOIN jobs.roles r ON r.job_id = s.job_id"
        " WHERE s.tailor_status = 'queued' AND s.prompt_path IS NOT NULL").fetchall()
    out = []
    for r in rows:
        if not Path(r["prompt_path"] or "").exists():
            continue
        if _queue_attempts.get(r["job_id"], 0) >= 2:
            continue                 # tried twice this session and still queued → leave it to the nightly
        out.append({"job_id": r["job_id"], "company": r["company"], "title": r["title"],
                    "prompt": r["prompt_path"],
                    "resume_out": str(tailoring.resume_yaml_path(cfg, r["company"], r["job_id"])),
                    "score": r["weighted_score"]})
    return out


def _start_queued(conn, note: str) -> dict:
    """Owner 2026-09-23: a role queued with ✎ starts tailoring NOW, not at the nightly run. If a
    batch is already running (lock), the role stays queued and the queue runner picks it up as
    soon as the lock clears."""
    items = _queued_manifest(conn)
    if not items:
        return {"queued": 0, "started": False}
    if _tailor_batch_paths()["lock"].exists():
        return {"queued": len(items), "started": False, "waiting": True}
    for it in items:
        _queue_attempts[it["job_id"]] = _queue_attempts.get(it["job_id"], 0) + 1
    return {"queued": len(items), **_launch_manifest(items, f"{note}: {len(items)} queued roles")}


def _queue_runner() -> None:
    """Background poller: every 60s, if no batch is running and roles are queued, launch them.
    This is what makes 'queued while a batch was running' start by itself afterwards."""
    while True:
        time.sleep(60)
        try:
            conn = store.connect_view(cfg)
            try:
                _start_queued(conn, "queue runner")
            finally:
                conn.close()
        except Exception:      # noqa: BLE001 — a poller must never die
            pass


if "pytest" not in sys.modules and os.environ.get("JOBSCAN_QUEUE_RUNNER", "1") == "1":
    threading.Thread(target=_queue_runner, name="jobscan-queue-runner", daemon=True).start()


@app.post("/api/tailor-batch")
def api_tailor_batch(body: _TailorBatchIn, request: Request, conn=Depends(view_conn)):
    """Queue a résumé for every role the review page is showing that has none yet.

    Reads the list exactly as index() does (same fetch_view call, same identity-cookie
    fallback), keeps rows that are neither `ready` nor `drop`, caps at
    auto_tailor.manual_batch_cap by rank_score, writes their prompts + a .manual.json
    manifest, and hands it to tools/auto_tailor.sh detached. dry_run only counts."""
    identity = body.identity
    if identity is None:
        identity = request.cookies.get("apply_identity", "")
    try:
        ms = float(body.min_score)
    except (TypeError, ValueError):
        ms = 0.0
    try:
        rot_i = max(0, int(body.rot))
    except (TypeError, ValueError):
        rot_i = 0
    data = store.fetch_view(
        conn, cfg, status=body.status, q=body.q, company=body.company, min_score=ms,
        verdict=body.verdict, seen=body.seen, remote=body.remote, seniority=body.seniority,
        sort=body.sort, rot=rot_i, tag=body.tag, identity=identity, tailored=body.tailored,
        ease=body.ease, pace=not body.showall,
    )
    todo = [r for r in data["rows"]
            if (r.get("tailor_status") or "") != "ready" and r.get("verdict") != "drop"]
    # Highest rank first, so when the cap bites it drops the tail, not what she'd apply to next.
    todo.sort(key=lambda r: -(r.get("rank_score") or r.get("weighted_score") or 0))
    cap = int((cfg.get("auto_tailor") or {}).get("manual_batch_cap", 150))
    capped = max(0, len(todo) - cap)
    todo = todo[:cap]
    if body.dry_run:
        return {"queued": len(todo), "capped": capped,
                "sample": [(r["company"], r["title"]) for r in todo[:5]]}

    paths = _tailor_batch_paths()
    # Checked BEFORE writing anything: write_prompt flips each role to tailor_status=queued,
    # and a 409 after that would leave roles marked queued with nobody working them. The
    # script re-checks the same lock itself, so a race here only makes it exit quietly.
    if paths["lock"].exists():
        raise HTTPException(409, "已有一批在跑（夜间或上一批），等它跑完")
    manifest, skipped = [], []
    for r in todo:
        try:
            p = tailoring.write_prompt(cfg, conn, r["job_id"])
            out = tailoring.resume_yaml_path(cfg, r["company"], r["job_id"])
        except Exception as e:  # noqa: BLE001 — one bad role must not sink the whole batch
            skipped.append(f"{r['job_id']}: {e}")
            continue
        manifest.append({"job_id": r["job_id"], "company": r["company"], "title": r["title"],
                         "prompt": str(p), "resume_out": str(out), "score": r["weighted_score"]})
    res = {"queued": len(manifest), "capped": capped, "skipped": skipped}
    res.update(_launch_manifest(manifest, f"manual batch from review page: {len(manifest)} roles"))
    return res


@app.get("/api/tailor-batch/status")
def api_tailor_batch_status(conn=Depends(view_conn)):
    """Progress of the last manual batch. written = worker YAMLs on disk; ingested = of those,
    how many role_state already says `ready` (the single ingest pass at the END of the script
    flips them all at once, so ingested lags written until then); running = the shared lock."""
    paths = _tailor_batch_paths()
    items: list = []
    mtime = None
    if paths["manifest"].exists():
        try:
            items = json.loads(paths["manifest"].read_text(encoding="utf-8")) or []
        except (OSError, ValueError):
            items = []
        mtime = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(paths["manifest"].stat().st_mtime))
    written_ids = [it["job_id"] for it in items
                   if it.get("resume_out") and Path(it["resume_out"]).exists()]
    ingested = 0
    for i in range(0, len(written_ids), 500):        # stay under SQLite's bound-variable cap
        chunk = written_ids[i:i + 500]
        ingested += conn.execute(
            "SELECT COUNT(*) FROM role_state WHERE tailor_status='ready'"
            f" AND job_id IN ({','.join('?' * len(chunk))})", chunk).fetchone()[0]
    log_tail: list[str] = []
    if paths["log"].exists():
        try:
            with open(paths["log"], "rb") as f:
                f.seek(max(0, paths["log"].stat().st_size - 8192))   # tail, not the whole file
                text = f.read().decode("utf-8", errors="replace")
            log_tail = [ln for ln in text.splitlines() if ln.strip()][-3:]
        except OSError:
            pass
    return {"running": paths["lock"].exists(), "total": len(items), "written": len(written_ids),
            "ingested": ingested, "log_tail": log_tail, "manifest_mtime": mtime}


def _profiles_summary() -> dict:
    """名片夹摘要 for the extension panel: every card carries CONCRETE fill values. The
    default card (config 里只有 label/display_name) is completed from cfg profile so the
    panel can switch back to it purely client-side when the URL can't re-identify the role."""
    ap = cfg.get("apply_profiles") or {}
    base = cfg.get("profile") or {}
    default = ap.get("default") or "casey"
    out = {}
    for k, c in (ap.get("profiles") or {}).items():
        fallback = base if k == default else {}
        out[k] = {
            "label": c.get("label", k[:1].upper()),
            "display_name": c.get("display_name", k),
            "email": c.get("email") or base.get("email"),
            "first_name": c.get("first_name") or fallback.get("first_name"),
            "preferred_name": c.get("preferred_name") or fallback.get("preferred_name"),
            "phone_national": c.get("phone_national") or fallback.get("phone"),
        }
    return {"default": default, "profiles": out}


def _apply_identity(conn, role: dict, identity: str) -> dict:
    """The extension's workspace selector can pin an apply profile (name card) for this Chrome
    profile — e.g. the owner's 小号 `alex`. Same effect as clicking the card badge on the
    review page: the role is switched to that identity (persisted, résumé re-lettered), so
    everything downstream — header name, email, phone, PDF name — agrees. Roles already
    applied/interviewing are left alone (the application went out under the old identity)."""
    identity = (identity or "").strip()
    profiles = (cfg.get("apply_profiles") or {}).get("profiles") or {}
    if not identity or identity not in profiles or not role:
        return role
    if store.effective_profile_key(cfg, role) == identity:
        return role
    if (role.get("status") or "new") in ("applied", "interviewing"):
        # The application already went out under another card: keep that history on the row,
        # but the person at the keyboard picked THIS workspace, so the form she is filling now
        # (a re-apply, or the same posting reached twice) gets this card — overlay, don't persist.
        # 2026-09-25: returning the untouched role here is how a Alex-profile Chrome filled
        # Casey + candidate on an applied Fluidstack posting.
        r = dict(role); r["apply_profile"] = identity
        return r
    store.upsert_state(conn, role["job_id"], apply_profile=identity)
    ypath = tailoring.resume_yaml_path(cfg, role["company"], role["job_id"])
    if ypath.exists():
        try:
            tailoring.ingest_file(cfg, conn, ypath)
        except tailoring.TailorError:
            pass    # identity saved; the nightly autotailor will regenerate and pick it up
    return store.get_role(conn, role["job_id"]) or role


def _ensure_letterhead(conn, role: dict) -> dict:
    """Make the on-disk PDF carry the name of this role's EFFECTIVE card before the extension
    attaches it. Owner 2026-09-25: Nvidia/Nuro/Cohere were mapped to the 法名 card, the form
    fields filled as Alex + candidate, but the attached résumé still said "Casey Example" —
    the PDF had been rendered before the company was mapped, and nothing re-lettered it (ingest
    only touches changed YAMLs). The ATS parsed the résumé name and the application landed as
    Casey in Alex's mailbox. Re-render whenever the filename's name prefix disagrees."""
    p = role.get("tailored_resume_path")
    if not p or not Path(p).exists():
        return role
    card = tailoring.apply_profile_identity(cfg, store.effective_profile_key(cfg, role))
    want = (card or {}).get("display_name") or tailoring.candidate_name(cfg)
    if Path(p).name.startswith(want):
        return role
    ypath = tailoring.resume_yaml_path(cfg, role["company"], role["job_id"])
    if not ypath.exists():
        return role
    try:
        tailoring.ingest_file(cfg, conn, ypath)
    except tailoring.TailorError:
        return role
    return store.get_role(conn, role["job_id"]) or role


def _cooldown_for(conn, role: dict) -> dict | None:
    """Active cooldown for this role's company, for the extension panel (owner 2026-09-25:
    「投的时候才知道不让投」— the panel is where she learns it, so it shows and sets it there)."""
    cd = store.active_cooldowns(conn, cfg).get((role.get("company_norm") or "").lower())
    if not cd:
        return None
    try:
        days_left = (datetime.date.fromisoformat(cd["until"]) - datetime.date.fromisoformat(store.la_today())).days
    except (KeyError, ValueError):
        days_left = None
    return {"until": cd.get("until"), "days_left": days_left, "reason": cd.get("reason", ""),
            "source": cd.get("source", "manual")}


def _profile_for_role(role: dict) -> dict:
    """The form-fill profile, with this role's 投递身份 (apply profile) overlaid. A role
    switched to the 法名 card must autofill Alex + candidate — a form filled under one
    name with the other's email is exactly the mix-up the feature exists to prevent."""
    prof = dict(cfg.get("profile") or {})
    card = tailoring.apply_profile_identity(cfg, store.effective_profile_key(cfg, role))
    if card:
        for src, dst in (("first_name", "first_name"), ("preferred_name", "preferred_name"),
                         ("email", "email"), ("phone_national", "phone")):
            if card.get(src):
                prof[dst] = card[src]
    from .application_memory import validate_identity
    try:
        validate_identity(prof)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    prof["confirmed_answers"] = confirmed_answers.for_company(cfg, role.get("company"))
    # A batch reads the board's essay switch here: with it off, model text stays out of optional boxes.
    from .form_preflight import allow_essays
    prof["allow_unread_essays"] = allow_essays(cfg)
    return prof


def _lookup_payload(conn, role: dict, top_role_default: bool = False) -> dict:
    """Build the role+profile response the extension consumes (shared by /api/lookup and
    /api/top-role). `top_role_default` flags that this role was chosen as the company's best
    guess, not matched from the URL, so the panel can say so."""
    has_pdf = bool(role.get("tailored_resume_path")
                   and Path(role["tailored_resume_path"]).exists())
    # Have I applied to THIS company before? (the "I forget I already applied" guard, shown as a
    # warning on the panel.) Count other roles at the same company already applied/interviewing.
    prior = conn.execute(
        "SELECT r.title, s.status, s.applied_date FROM jobs.roles r JOIN role_state s"
        " ON s.job_id=r.job_id WHERE r.company_norm=(SELECT company_norm FROM jobs.roles"
        " WHERE job_id=?) AND s.status IN ('applied','interviewing') AND r.job_id!=?"
        " ORDER BY s.applied_date DESC",
        (role["job_id"], role["job_id"]),
    ).fetchall()
    ai_cfg = cfg.get("ai") or {}
    from .application_memory import memory_pack
    return {
        "job_id": role["job_id"], "company": role["company"], "title": role["title"],
        "status": role["status"], "tailor_status": role.get("tailor_status") or "none",
        "has_tailored_pdf": has_pdf,
        "resume_variant": "tailored" if has_pdf else "base",
        "ai_enabled": aimod.available(cfg),
        "auto_dropdowns": bool(ai_cfg.get("auto_dropdowns", True)),
        "auto_cover_letter": bool(ai_cfg.get("auto_cover_letter", False)),
        "field_button": bool(ai_cfg.get("field_button", True)),   # ✨ per-field AI draft chips
        "this_status": role.get("status") or "new",     # THIS role's own status
        "cooldown": _cooldown_for(conn, role),          # {until, days_left, reason, source} or None
        "company_applied": [                             # OTHER roles at this company already done
            {"title": r["title"], "status": r["status"], "date": r["applied_date"]}
            for r in prior[:6]
        ],
        "mailcode_enabled": mailmod.available(cfg),
        "eeo_decline": bool(cfg.get("eeo_decline", True)),
        "eeo": cfg.get("eeo") or {},          # per-field self-ID preference; unset → decline
        "profile": _profile_for_role(role),   # this role's apply profile overlaid (法名 roles fill 法名)
        "shared_memory": memory_pack(_profile_for_role(role), tailoring.load_base(cfg)),
        "apply_profile": store.effective_profile_key(cfg, role),
        # 名片夹摘要：面板用来显示「这份要用哪个身份填」+ 渲染切换按钮，不用发第二个请求
        "apply_profiles": _profiles_summary(),
        "experience": profile_experience(role),   # work history + bullets for Workday's My Experience
        "skills": profile_skills(role),           # for Workday's "Type to Add Skills" typeahead
        "top_role_default": top_role_default,      # true → chosen as the company's best guess
    }


@app.get("/api/lookup")
def api_lookup(url: str, identity: str = "", conn=Depends(view_conn)):
    """The extension asks 'what role is this application page?' Returns role + profile.

    Two strategies: scanner-ATS URLs carry a job id (gh_jid=, /jobs/<n>, pid=, workday req) —
    match on that. Captured roles (Tesla/Apple/any careers site) don't, but their stored
    apply_url IS this page — match on the URL itself, raw and canonicalized."""
    from . import manual as manualmod
    role = store.lookup_role_by_ids(conn, url_candidates(url))
    if not role:
        role = store.lookup_role_by_apply_url(
            conn, [url, manualmod.canonical_capture_url(url)])
    if not role:
        raise HTTPException(404, "no matching role for this URL")
    return _lookup_payload(conn, _ensure_letterhead(conn, _apply_identity(conn, role, identity)))


class PinIn(BaseModel):
    job_id: str


@app.post("/api/pin")
def api_pin(body: PinIn, conn=Depends(view_conn)):
    """'I'm applying to this role right now.' Set by the ↗ link in the UI, just before the
    apply page opens. This is the ONLY join back to a role whose apply_url can't be matched
    from the landing page — a LinkedIn role's apply_url is the LinkedIn page, but Apply there
    hands you off to the employer's own ATS, whose URL shares no id with our li:<id>."""
    if not store.get_role(conn, body.job_id):
        raise HTTPException(404, "unknown job_id")
    store.set_apply_pin(conn, body.job_id)
    return {"ok": True, "job_id": body.job_id}


@app.get("/api/pins")
def api_pins(conn=Depends(view_conn)):
    """EVERY live pin, newest first, each in the same shape as /api/lookup.

    The extension asks for this only after its URL lookup fails, and then picks the one whose
    company actually appears on the page. Returning the whole set (rather than the server
    picking) is the point: only the extension can see the page, and a server-side pick is
    exactly what mislabelled a Precisely form with a Temporal role."""
    out = []
    for pin in store.get_apply_pins(conn):
        role = store.get_role(conn, pin["job_id"])
        if not role:
            continue                  # role deleted/delisted since the ↗ — skip, don't 500
        payload = _lookup_payload(conn, role)
        payload["from_pin"] = True    # the panel says so — this was intent, not a URL match
        payload["pinned_at"] = pin["pinned_at"]
        out.append(payload)
    return {"pins": out}


@app.get("/api/pinned")
def api_pinned(conn=Depends(view_conn)):
    """The most recent live pin. Kept for older extension builds — current ones call /api/pins
    and choose by page. 404 when there is none (or it went stale)."""
    job_id = store.get_apply_pin(conn)
    if not job_id:
        raise HTTPException(404, "no active pin")
    role = store.get_role(conn, job_id)
    if not role:
        raise HTTPException(404, "pinned role no longer exists")
    payload = _lookup_payload(conn, role)
    payload["from_pin"] = True        # the panel says so — this was intent, not a URL match
    return payload


@app.delete("/api/pin")
def api_unpin(job_id: str | None = None, conn=Depends(view_conn)):
    """Clear one pin (the frame that claimed it releases just its own) or, with no job_id,
    every pin. Per-role is what multi-pin needs — clearing all on one claim would put us back
    to losing the other tabs' roles."""
    store.clear_apply_pin(conn, job_id)
    return {"ok": True, "cleared": job_id or "all"}


_PAY = re.compile(r"\$\s*([0-9]{2,3}(?:,[0-9]{3})+|[0-9]{5,6})(?!\s*%)", re.I)


@app.get("/api/salary")
def api_salary(job_id: str, conn=Depends(view_conn)):
    """A desired-salary figure for ONE role. Truth order: the JD's own published range →
    an AI market estimate → the configured fallback. The response always says which one it
    used, so a guessed number never masquerades as a published one."""
    role = store.get_role(conn, job_id)
    if not role:
        raise HTTPException(404, "unknown job_id")
    rule = cfg.get("salary") or {}

    # 1) the JD's published range (filter to plausible annual salaries — skips "10%" bonuses,
    #    "401k", years, and stray ids that also look like numbers)
    nums = sorted({int(m.group(1).replace(",", "")) for m in _PAY.finditer(role.get("jd_text") or "")})
    nums = [n for n in nums if 40_000 <= n <= 900_000]
    if nums:
        which = str(rule.get("use_jd_range", "low")).lower()
        amount = nums[-1] if which == "high" else nums[len(nums) // 2] if which == "mid" else nums[0]
        return {"amount": amount, "source": f"JD range ${nums[0]:,}–${nums[-1]:,} ({which} end)"}

    # 2) AI market estimate. Uses ai.estimate_salary(), NOT answer_questions() — the latter
    #    refuses compensation questions by design (the model must never answer a salary
    #    question TO an employer); this is research FOR the candidate, who still reviews it.
    if aimod.available(cfg):
        try:
            est = aimod.estimate_salary(cfg, role)
            if est:
                return {"amount": est, "source": "AI market estimate"}
        except Exception:
            pass

    # 3) fallback
    return {"amount": int(rule.get("fallback", 180_000)), "source": "configured fallback"}


class PickIn(BaseModel):
    job_id: str
    question: str
    options: list[str]


@app.post("/api/pick-option")
def api_pick_option(body: PickIn, conn=Depends(view_conn)):
    """Choose the best dropdown option index for a sentence-option question (work auth,
    relocation…), grounded in the candidate facts. -1 means 'leave it for the user'."""
    if not aimod.available(cfg):
        return {"index": -1}
    role = store.get_role(conn, body.job_id)
    if not role:
        raise HTTPException(404, "unknown job_id")
    try:
        return {"index": aimod.pick_option(cfg, role, body.question, body.options)}
    except Exception:
        return {"index": -1}


@app.get("/api/top-role")
def api_top_role(company: str, identity: str = "", conn=Depends(view_conn)):
    """For a form whose URL carries no job id (Google/Microsoft), pick the company's HIGHEST-
    scored role that has a tailored résumé — the owner applies top-score-first, so the top role
    is the best default. Returns the same shape as /api/lookup so the extension treats it
    identically. Falls back to profile-only (404) if the company has no tailored role."""
    cn = cfgmod.norm_key(company)
    # Skip roles that are already done. Without this the guess kept landing on the company's
    # single best role — which, for 79 of 210 companies with résumés (38%), is one already
    # applied to. The panel then said "⚠ you already marked THIS role applied" about a role
    # the owner was NOT looking at, and offered ITS tailored résumé for the form on screen.
    # You are standing on an application form, so the only useful guess is an open role.
    row = conn.execute(
        "SELECT r.job_id FROM jobs.roles r LEFT JOIN role_state s ON s.job_id=r.job_id"
        " WHERE r.company_norm=? AND s.tailor_status='ready' AND s.tailored_resume_path IS NOT NULL"
        " AND COALESCE(s.status,'new') NOT IN ('applied','interviewing','rejected','expired')"
        " ORDER BY r.weighted_score DESC LIMIT 1", (cn,)).fetchone()
    if not row:
        raise HTTPException(404, f"no un-applied tailored role for {company!r}")
    role = store.get_role(conn, row["job_id"])
    return _lookup_payload(conn, _apply_identity(conn, role, identity), top_role_default=True)


@app.get("/api/profile")
def api_profile(identity: str = ""):
    """Role-independent profile for forms whose URL carries no job id (Google/Microsoft careers).
    The extension fills name/email/phone/eligibility/consent from this — no résumé, no
    role-specific questions, since we can't tell which posting it is. `identity` (from the
    extension's workspace selector) overlays that name card's name/email/phone."""
    ai_cfg = cfg.get("ai") or {}
    prof = dict(cfg.get("profile") or {})
    card = tailoring.apply_profile_identity(cfg, identity) if identity else None
    if card:
        for src, dst in (("first_name", "first_name"), ("preferred_name", "preferred_name"),
                         ("email", "email"), ("phone_national", "phone")):
            if card.get(src):
                prof[dst] = card[src]
    prof["confirmed_answers"] = confirmed_answers.for_company(cfg)
    return {
        "profile_only": True,
        "profile": prof,
        "eeo": cfg.get("eeo") or {},
        "eeo_decline": bool(cfg.get("eeo_decline", True)),
        "experience": profile_experience(),
        "ai_enabled": aimod.available(cfg),
        "auto_dropdowns": bool(ai_cfg.get("auto_dropdowns", True)),
    }


@app.get("/api/applied-scan")
def api_applied_scan(days: int = 90, conn=Depends(view_conn)):
    """Review queue of roles the inbox suggests you already applied to. Read-only — never
    changes status; the UI confirms each match one at a time via the status endpoint."""
    from . import applied_scan
    if not applied_scan.available(cfg):
        raise HTTPException(503, "email scan disabled (set mailcode.email + gmail_app_password, or authorize Google on /settings)")
    try:
        return applied_scan.scan_all(cfg, days=days)
    except Exception as e:
        raise HTTPException(502, f"applied-scan failed: {type(e).__name__}: {e}")


@app.get("/api/cover-letter")
def api_cover_letter(job_id: str, words: int = 180, conn=Depends(view_conn)):
    """A short cover-letter paragraph for the 'Additional information / anything else you'd
    like us to know' boxes. Same voice method as the cover-letter skill."""
    if not aimod.available(cfg):
        raise HTTPException(503, "AI disabled (set ai.enabled + a key in secrets.yaml)")
    role = store.get_role(conn, job_id)
    if not role:
        raise HTTPException(404, "unknown job_id")
    try:
        text = aimod.cover_letter(cfg, role, max_words=words)
    except Exception as e:
        raise HTTPException(502, f"cover letter failed: {type(e).__name__}: {e}")
    if not text:
        raise HTTPException(502, "model returned nothing")
    if text.startswith("MISMATCH:"):
        # mismatch fuse (owner 2026-09-09): the model judged this a different-field specialist
        # role it cannot honestly sell. 409 (outside the try above, so it isn't re-wrapped as
        # 502) — the extension shows the reason and leaves the box empty.
        raise HTTPException(409, text)
    return {"text": text}


_DASHES = dict.fromkeys("‐‑‒–—―−⁃", "-")


def outbound_pdf_name(path: str) -> str:
    """The filename a RECRUITER should see. On disk the tailored PDFs carry the internal
    bookkeeping tail — "… [bb44fbf66595bca5] - 88.pdf" — and the extension uploads whatever
    name this endpoint hands it, so applications were going out branded with our own match
    score. Strip the [id] and the score; keep the human part ("Casey Example - Apple - Apple
    Data Platform.pdf").

    The result must be pure ASCII: this name also rides out as the raw `x-filename` header,
    and Starlette encodes custom headers as latin-1, so ONE en-dash in a job title (xAI posts
    "Software Engineer– X Core Product") raised UnicodeEncodeError and 500'd the whole
    download — the extension could never attach a résumé for 22 roles across 12 companies.
    Folding also tidies the separator wreckage those titles leave ("- – X Core" → "- X Core")."""
    name = Path(path).name
    name = re.sub(r"\s*\[[^\]]*\]\s*(?:-\s*\d{1,3}\s*)?(?=\.pdf$)", "", name, flags=re.I)
    name = re.sub(r"\s+(?=\.pdf$)", "", name, flags=re.I)
    name = name.translate(str.maketrans(_DASHES))
    # NFKD + ignore turns é into e and drops anything with no ASCII form (│, ®, emoji).
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    # Tidy the STEM only — the separator collapse below would otherwise eat the dot of
    # ".pdf" on the degenerate names an empty title leaves behind ("… - ..pdf").
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    # A dropped/folded char can leave a dangling separator run: "- – X" → "- - X" → "- X",
    # "- . – Cloud" → "- Cloud". Keep collapsing until it stops changing.
    while True:
        collapsed = re.sub(r"\s*-\s*(?:[-.,;:|·]+\s*)+", " - ", stem)
        if collapsed == stem:
            break
        stem = collapsed
    stem = re.sub(r"\s{2,}", " ", stem).strip()
    stem = re.sub(r"^[-.,;:|·\s]+|[-.,;:|·\s]+$", "", stem)   # no leading/trailing separator
    return f"{stem}.pdf" if stem else "resume.pdf"


@app.get("/api/roles/{job_id}/resume.pdf")
def api_resume_pdf(job_id: str, conn=Depends(view_conn)):
    role = store.get_role(conn, job_id)
    if not role:
        raise HTTPException(404, "unknown job_id")
    role = _ensure_letterhead(conn, role)         # the file the extension attaches must match the card
    p = role.get("tailored_resume_path")
    if not p or not Path(p).exists():
        raise HTTPException(404, "no tailored PDF yet")
    clean = outbound_pdf_name(p)
    # x-filename is what the extension names the attached file — the recruiter-facing name.
    return FileResponse(p, media_type="application/pdf", filename=clean,
                        headers={"x-filename": clean})


class AnswerIn(BaseModel):
    job_id: str
    questions: list[str]


@app.post("/api/answer")
def api_answer(body: AnswerIn, conn=Depends(view_conn)):
    """Draft AI answers for open-ended application questions of one role. Opt-in; needs a key."""
    if not aimod.available(cfg):
        raise HTTPException(503, "AI autofill disabled (set ai.enabled + secrets.yaml key)")
    role = store.get_role(conn, body.job_id)
    if not role:
        raise HTTPException(404, "unknown job_id")
    try:
        answers = aimod.answer_questions(cfg, role, body.questions)
    except Exception as e:  # surface a clean message to the extension panel
        raise HTTPException(502, f"AI answer failed: {type(e).__name__}: {e}")
    return {"answers": answers}


@app.get("/api/email-code")
def api_email_code(request: Request, company: str = ""):
    """Fetch the newest email verification code (opt-in; reads inbox over IMAP)."""
    # A page that rebinds its own domain to this machine counts as same-origin and sends no Origin
    # on a GET. Only its Host gives it away. Same check as workbench_page in agent_lab.py.
    if request.url.hostname not in {"localhost", "127.0.0.1", "testserver"}:
        raise HTTPException(403, "email-code is local only")
    # A web page's no-cors request sends no Origin, but it cannot hide Sec-Fetch-Site.
    origin = request.headers.get("origin", "")
    if (origin and not origin.startswith("chrome-extension://")) or request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
        raise HTTPException(403, "email-code only answers the extension")
    if not mailmod.available(cfg):
        raise HTTPException(503, "email-code disabled (set mailcode.enabled + gmail_app_password, or authorize Google on /settings)")
    try:
        if company:
            after = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=mailmod.within_minutes(cfg))
            return mailmod.fetch_code_for(cfg, company, after)
        return mailmod.fetch_latest_code_any(cfg)
    except Exception as e:
        raise HTTPException(502, f"email-code failed: {type(e).__name__}: {e}")


@app.get("/api/applied-companies")
def api_list_applied(conn=Depends(view_conn)):
    return store.list_applied_companies(conn)


@app.post("/api/applied-companies")
def api_add_applied(body: CompanyIn, conn=Depends(view_conn)):
    if not body.name.strip():
        raise HTTPException(400, "empty name")
    store.add_applied_company(conn, body.name)
    return {"ok": True}


@app.delete("/api/applied-companies/{name}")
def api_remove_applied(name: str, conn=Depends(view_conn)):
    store.remove_applied_company(conn, name)
    return {"ok": True}


@app.get("/replies", response_class=HTMLResponse)
def replies_page(request: Request, conn=Depends(view_conn)):
    armed = store.queue_list(conn, "armed")
    pending = store.queue_list(conn, "pending")
    undo = int((cfg.get("inbox") or {}).get("undo_minutes", 30))
    return templates.TemplateResponse(request, "replies.html", {
        "armed": armed, "pending": pending,
        "undo_minutes": undo, "calendar_on": gcalmod.available(cfg),
        "mode": (cfg.get("inbox") or {}).get("autosend_mode", "gated"),
    })


@app.get("/api/replies")
def api_replies(conn=Depends(view_conn)):
    return {"armed": store.queue_list(conn, "armed"),
            "pending": store.queue_list(conn, "pending"),
            "calendar_on": gcalmod.available(cfg)}


class ReplyIn(BaseModel):
    draft: str | None = None


@app.post("/api/replies/{msg_id}/send")
def api_reply_send(msg_id: str, body: ReplyIn, conn=Depends(view_conn)):
    it = store.queue_get(conn, msg_id)
    if not it:
        raise HTTPException(404, "not in queue")
    if not it.get("reply_to") or not it.get("repliable"):
        raise HTTPException(400, "no direct reply address — reply on the platform")
    draft = (body.draft if body.draft is not None else it["draft"]) or ""
    if "[[AVAILABILITY]]" in draft:
        raise HTTPException(400, "draft still has an [[AVAILABILITY]] placeholder — fill it first")
    try:
        sendmail.send_reply(cfg, it["reply_to"], it["subject"], draft,
                            in_reply_to=it["in_reply_to"], references=it["references_hdr"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"send failed: {e}")
    store.queue_mark(conn, msg_id, "sent", store.la_now_iso())
    return {"ok": True}


@app.post("/api/replies/{msg_id}/hold")
def api_reply_hold(msg_id: str, conn=Depends(view_conn)):
    """Cancel a queued auto-send: move ARMED → pending so it won't fire, keeps the draft."""
    store.queue_mark(conn, msg_id, "pending")
    return {"ok": True}


@app.post("/api/replies/{msg_id}/dismiss")
def api_reply_dismiss(msg_id: str, conn=Depends(view_conn)):
    store.queue_mark(conn, msg_id, "dismissed")
    return {"ok": True}


class CaptureIn(BaseModel):
    url: str
    title: str
    jd: str
    company: str | None = None
    location: str | None = None


_DOMAIN_CO = {
    "apple": "Apple", "microsoft": "Microsoft", "google": "Google", "atlassian": "Atlassian",
    "wise": "Wise", "linkedin": "LinkedIn", "tesla": "Tesla", "circle": "Circle",
    "hashicorp": "HashiCorp", "retool": "Retool", "deel": "Deel", "netflix": "Netflix",
    "uber": "Uber", "amazon": "Amazon", "meta": "Meta", "nvidia": "Nvidia",
}


def _company_from_url(url: str) -> str:
    host = re.sub(r"^https?://", "", url or "").split("/")[0].lower()
    for key, name in _DOMAIN_CO.items():
        if key in host:
            return name
    parts = [p for p in host.split(".") if p not in ("www", "jobs", "careers", "boards", "com", "io", "co", "ai", "net", "org")]
    return (parts[0].capitalize() if parts else "Captured")


@app.post("/api/capture")
def api_capture(body: CaptureIn):
    """Ingest a job the browser extension read off ANY careers page (Apple/MS/Google/anywhere).
    Adds it to jobscan.db via the same pipeline, scores it, and reports back."""
    from . import manual as manualmod
    from . import scoring as scoringmod
    from types import SimpleNamespace
    import hashlib
    jd = (body.jd or "").strip()
    if len(jd) < 80:
        raise HTTPException(400, "couldn't read a real job description on this page")
    if manualmod.looks_blocked(jd):
        raise HTTPException(400, "the site's bot protection served an 'Access Denied' page "
                                 "instead of the job — reload the job page and capture again")
    company = (body.company or _company_from_url(body.url)).strip() or "Captured"
    # Hash the CANONICAL url — the raw one changes with every search that led to the job,
    # and each variant used to mint a fresh duplicate role.
    canon = manualmod.canonical_capture_url(body.url)
    # Site chrome is not a title (Tesla's first <h1> is its header nav; 67 roles landed as
    # "…Skip to main content" and the family gate read 'content' as a content-marketing role).
    # The URL slug is the reliable source when the page's own title looks like chrome.
    title = (body.title or "").strip()
    if not title or len(title) > 100 or manualmod.BAD_TITLE.search(title):
        title = manualmod.title_from_url(canon) or "Software Engineer"
    loc = (body.location or "").strip()
    jid = "cap:" + hashlib.sha1(canon.encode()).hexdigest()[:16]
    jobs = store.connect_jobs(cfg)
    manualmod.add_role(cfg, jobs, company, title, jd, apply_url=canon, location=loc, job_id=jid)
    notes = cfgmod.load_company_notes(cfg).get(cfgmod.norm_key(company), {})
    p = SimpleNamespace(title=title, jd_text=jd, is_remote="remote" in loc.lower(),
                        company=company, location=loc, apply_url=body.url)
    s = scoringmod.score(p, notes, cfg)
    jobs.execute("UPDATE roles SET s_stability=?, s_culture=?, s_depth=?, s_wlb=?, weighted_score=?,"
                 " verdict=?, why=?, caution=? WHERE job_id=?",
                 (s.s_stability, s.s_culture, s.s_depth, s.s_wlb, s.weighted, s.verdict, s.why, s.caution, jid))
    jobs.commit()
    return {"ok": True, "job_id": jid, "company": company, "title": title,
            "score": s.weighted, "verdict": s.verdict}


# Careers pages for the companies the daily scanner can't reach — bookmark /browse and skim daily.
# Trimmed 2026-07-30 to the boards that have ACTUALLY produced a role. Dropped Atlassian,
# Circle and HashiCorp: each sat here since the list was written and has never yielded a
# single row in the DB (0 captured, 0 manual, 0 via LinkedIn), so they were pure daily noise.
# The rest have earned their place — browser capture is where ~160 of these roles came from
# (Tesla 65, Microsoft 56, Apple 16, Retool 11, Deel 7, Google 7, Wise 4), and LinkedIn's
# keyword sweep does NOT cover them: it reaches Microsoft/Google/LinkedIn only.
_UNSUPPORTED_LINKS = {
    "Apple": "https://jobs.apple.com/en-us/search?search=software%20engineer&location=united-states-USA&sort=newest",
    "Microsoft": "https://jobs.careers.microsoft.com/global/en/search?q=software%20engineer&lc=United%20States&exp=Experienced%20professionals",
    "Google": "https://www.google.com/about/careers/applications/jobs/results?q=software%20engineer&location=United%20States",
    "Tesla": "https://www.tesla.com/careers/search/?query=software%20engineer&region=5",
    "Wise": "https://wise.jobs/jobs/?search=software+engineer",
    "LinkedIn": "https://careers.linkedin.com/jobs?keywords=software%20engineer",
    "Retool": "https://retool.com/careers#jobs",
    "Deel": "https://www.deel.com/careers/",
}


@app.get("/browse", response_class=HTMLResponse)
def browse_unsupported():
    rows = "".join(
        f'<li><a href="{u}" target="_blank" rel="noopener">{co}</a> '
        f'<span class="d">— open careers page</span></li>' for co, u in _UNSUPPORTED_LINKS.items())
    return HTMLResponse(
        '<meta charset="utf-8"><title>JobScan · Browse</title>'
        '<style>body{font:15px/1.6 -apple-system,system-ui,sans-serif;max-width:640px;margin:32px auto;padding:0 20px;color:#0f172a}'
        'h1{font-size:20px}a.b{color:#12314f;font-weight:600;text-decoration:none}'
        'ul{list-style:none;padding:0}li{padding:8px 0;border-bottom:1px solid #eef2f6}'
        'li a{font-weight:600;color:#12314f}.d{color:#64748b;font-size:13px}'
        '.tip{background:#eff6ff;border:1px solid #bfdbfe;padding:10px 12px;border-radius:9px;font-size:13px;color:#1e40af}</style>'
        '<a class="b" href="/">← back to JobScan</a><h1>🌐 Browse unsupported companies</h1>'
        '<p class="tip">These companies can\'t be auto-scanned. Skim them here daily — when you find a role you like, '
        'open it and hit <b>📥 Capture</b> in the JobScan extension to pull it into JobScan (scored + tailorable).</p>'
        f'<ul>{rows}</ul>')


@app.get("/apply", response_class=HTMLResponse)
def apply_queue(conn=Depends(view_conn)):
    """One-click apply queue: review roles that have a tailored résumé ready, newest-highest first.
    'Open all' opens each application page in its own tab so you apply straight through."""
    data = store.fetch_view(conn, cfg, status="new", sort="score")
    ready = [r for r in data["rows"] if r.get("tailor_status") == "ready" and r.get("apply_url")][:25]
    items = "".join(
        f'<li><a class="j" href="{r["apply_url"]}" target="_blank" rel="noopener">{r["company"]} — {r["title"]}</a>'
        f' <span class="s">{r["weighted_score"]:.0f}</span>'
        f' <a class="r" href="/api/roles/{r["job_id"]}/resume.pdf" target="_blank">📄</a></li>'
        for r in ready)
    urls = ",".join(f'"{r["apply_url"]}"' for r in ready)
    return HTMLResponse(
        '<meta charset="utf-8"><title>JobScan · Apply queue</title>'
        '<style>body{font:15px/1.6 -apple-system,system-ui,sans-serif;max-width:720px;margin:32px auto;padding:0 20px;color:#0f172a}'
        'h1{font-size:20px}a.b{color:#12314f;font-weight:600;text-decoration:none}ul{list-style:none;padding:0}'
        'li{padding:8px 0;border-bottom:1px solid #eef2f6;display:flex;align-items:center;gap:8px}'
        'a.j{font-weight:600;color:#12314f;text-decoration:none;flex:1}a.j:hover{text-decoration:underline}'
        '.s{background:#e7f6ec;color:#15803d;font-weight:700;border-radius:6px;padding:1px 7px;font-size:13px}'
        'a.r{text-decoration:none}button{font:inherit;font-weight:700;background:#12314f;color:#fff;border:0;'
        'border-radius:9px;padding:10px 16px;cursor:pointer;margin:8px 0}'
        '.tip{background:#fff7ed;border:1px solid #fed7aa;padding:10px 12px;border-radius:9px;font-size:13px;color:#9a3412}</style>'
        '<a class="b" href="/">← back to JobScan</a><h1>🚀 Apply queue '
        f'<span style="color:#64748b;font-weight:400">({len(ready)} ready)</span></h1>'
        '<p class="tip">Roles with a tailored résumé ready. <b>Open all</b> pops each application page in its own tab — '
        'then apply straight through. (First time, allow pop-ups for this site.)</p>'
        f'<button onclick=\'[{urls}].forEach(u=>window.open(u,"_blank"))\'>Open all {len(ready)} tabs</button>'
        f'<ul>{items}</ul>')


# ---------------------------------------------------------------- outcome / funnel / follow-up
#
# The half of the loop that used to end at status='applied'. All three pages share the same
# rule as the rest of the app: the scanner and the mailbox propose, the owner disposes. Nothing
# below writes a status without an explicit click, and nothing below sends mail.

_PAGE_CSS = (
    'body{font:14px/1.55 -apple-system,system-ui,sans-serif;max-width:1000px;margin:28px auto;'
    'padding:0 20px;color:#0f172a}h1{font-size:20px;margin:.4em 0}h2{font-size:15px;margin:1.6em 0 .5em}'
    'a.b{color:#12314f;font-weight:600;text-decoration:none}table{border-collapse:collapse;width:100%;'
    'margin:6px 0 18px}th,td{text-align:left;padding:5px 9px;border-bottom:1px solid #eef2f6}'
    'th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#64748b;font-weight:600}'
    'td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}'
    '.tip{background:#eff6ff;border:1px solid #bfdbfe;padding:9px 12px;border-radius:9px;'
    'font-size:13px;color:#1e40af;margin:10px 0}'
    '.warn{background:#fff7ed;border-color:#fed7aa;color:#9a3412}'
    '.muted{color:#94a3b8}.small{font-size:12px}'
    'button{font:inherit;font-size:13px;font-weight:600;background:#12314f;color:#fff;border:0;'
    'border-radius:7px;padding:5px 11px;cursor:pointer}button.g{background:#e2e8f0;color:#0f172a}'
    'button:disabled{opacity:.5;cursor:default}pre{white-space:pre-wrap;background:#f8fafc;'
    'border:1px solid #e2e8f0;border-radius:8px;padding:10px;font:13px/1.5 ui-monospace,monospace}'
    '.pill{border-radius:5px;padding:1px 6px;font-size:11px;font-weight:700}'
    '.hi{background:#dcfce7;color:#15803d}.md{background:#fef9c3;color:#854d0e}'
    '.lo{background:#f1f5f9;color:#475569}'
)


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f'<meta charset="utf-8"><title>JobScan · {title}</title>'
                        f'<style>{_PAGE_CSS}</style>'
                        f'<a class="b" href="/">← back to JobScan</a>{body}')


def _streak(it: dict) -> str:
    """'在榜 N 天' badge — sustained presence is the cheap stand-in for a star slope, so a
    6-day repo reads differently from a one-day spike. Silent for single-day items."""
    days = it.get("days_seen") or 1
    if days <= 1:
        return ""
    peak, score = it.get("peak_score") or 0, it.get("score") or 0
    tail = f" · 峰值 {peak:,}" if peak > score else ""
    return f' <span class="streak">在榜 {days} 天{tail}</span>'


@app.get("/ideas", response_class=HTMLResponse)
def ideas_page(days: int = 7):
    """Daily idea/project radar (Show HN · GH Trending · Product Hunt · HN front).
    Reads ideas.db; writes to it only through app.ideas (its single writer)."""
    from html import escape as esc
    conn = ideasmod.connect_ideas(cfg)
    try:
        groups = ideasmod.recent_days(conn, days=days)
        run = ideasmod.last_fetch(conn)
        dig = ideasmod.latest_digest(conn)
    finally:
        conn.close()
    digest_html = ""
    if dig:
        try:
            d = json.loads(dig["content"])
            secs = "".join(
                f'<h3>{esc(str(g["label"]))}</h3><ul class="ideas">' + "".join(
                    f'<li><a href="{esc(it["url"])}" target="_blank" rel="noopener">{esc(it["title"])}</a>'
                    f'<br><span class="muted small">{esc(it["why"])}</span></li>'
                    for it in g["items"]) + "</ul>"
                for g in d["groups"])
            digest_html = (
                f'<div class="digest"><h2 style="margin-top:0">📋 晨报'
                f' <span class="muted small">{esc(dig["day"])} · sonnet</span></h2>'
                f'<p><b>{esc(str(d["tldr"]))}</b></p>{secs}</div>'
                '<h2 class="muted" style="font-size:15px">原始抓取</h2>')
        except Exception:
            digest_html = ""       # malformed digest → page still works on the raw list
    runinfo = (f'last fetch {run["run_at"][:16]} · new {run["n_new"]}'
               + (f' · <span style="color:#b91c1c">{run["errors"]}</span>' if run["errors"] else "")
               if run else "no fetch yet")
    day_html = ""
    for g in groups:
        secs = ""
        for s in g["sources"]:
            rows = "".join(
                f'<li><a href="{it["url"]}" target="_blank" rel="noopener">{it["title"]}</a>'
                + (f' <span class="muted small">— {it["meta"]}</span>' if it["meta"] else "")
                + _streak(it)
                + "</li>"
                for it in s["items"])
            secs += f'<h2>{s["label"]} <span class="muted small">{len(s["items"])}</span></h2><ul class="ideas">{rows}</ul>'
        day_html += f'<details {"open" if g is groups[0] else ""}><summary><b>{g["day"]}</b></summary>{secs}</details>'
    if not groups:
        day_html = '<p class="tip">Nothing yet — hit fetch, or wait for the 8:30 launchd run.</p>'
    return _page("Ideas", f"""<h1>💡 Ideas</h1>
<p class="muted small">{runinfo} ·
<button onclick="this.disabled=true;this.textContent='fetching…';fetch('/api/ideas/fetch',{{method:'POST'}}).then(()=>location.reload())">fetch now</button></p>
<style>.ideas{{margin:6px 0 14px;padding-left:20px}}.ideas li{{margin:3px 0}}
.streak{{background:#ecfdf5;color:#047857;border:1px solid #a7f3d0;border-radius:9px;
padding:0 6px;font-size:11px;white-space:nowrap;margin-left:4px}}
details{{margin:10px 0}}summary{{cursor:pointer}}
.digest{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:14px 18px;margin:14px 0}}
.digest h3{{margin:12px 0 2px;font-size:14px}}.digest li{{margin:7px 0}}</style>
{digest_html}{day_html}""")


@app.post("/api/ideas/fetch")
def api_ideas_fetch():
    conn = ideasmod.connect_ideas(cfg)
    try:
        return ideasmod.fetch_all(conn)
    finally:
        conn.close()


class SignalIn(BaseModel):
    stage: str | None = None
    outcome: str | None = None
    note: str = ""
    when: str | None = None


@app.post("/api/roles/{job_id}/signal")
def api_record_signal(job_id: str, body: SignalIn, conn=Depends(view_conn)):
    """Record what happened to an application (stage reached / final outcome). Monotonic —
    see store.record_signal; a later signal can advance a row but never walk it backwards."""
    if not store.get_role(conn, job_id):
        raise HTTPException(404, "unknown job_id")
    try:
        state = store.record_signal(conn, job_id, stage=body.stage, outcome=body.outcome,
                                    note=body.note, when=body.when)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "state": state}


@app.get("/api/outcomes")
def api_outcomes(days: int = 120, conn=Depends(view_conn)):
    """Proposals only — what the mailbox suggests happened. Writes nothing (see app/outcomes.py)."""
    from . import outcomes as outcomesmod
    if not outcomesmod.available(cfg):
        raise HTTPException(503, "mailbox not configured")
    try:
        return outcomesmod.scan(cfg, days=days)
    except Exception as e:
        raise HTTPException(502, f"outcome scan failed: {type(e).__name__}: {e}")


_trends_cache: dict = {"at": 0.0, "html": ""}


def _trends_details_html(conn, data: dict) -> str:
    """落地层（owner 2026-09-10）：缺人主题 → 高频技能(✅有/📚缺) + 该补清单 + 具体岗位。"""
    from html import escape as esc
    from . import trends as trendsmod
    blocks = []
    for d in trendsmod.theme_details(cfg, conn, data):
        chips = " · ".join(
            f"{'✅' if s['have'] else '📚'}{esc(s['skill'])} <span class='muted'>{s['share_pct']}%({s['lift']}×)</span>"
            for s in d["skills"])
        roles = "".join(
            f"<li>{'📄' if p['resume_ready'] else '⭘'} {esc(p['company'])} — {esc(p['title'])}"
            f"（{p['score']} 分 · 挂 {p['age']} 天）</li>" for p in d["roles"]) or "<li class='muted'>暂无 title 命中的未投岗</li>"
        blocks.append(f"<h3>▸ {esc(d['theme'])} <span class='muted small'>({d['n_jds']} 份 JD)</span></h3>"
                      f"<p>{chips}</p><ul>{roles}</ul>")
    gap_date, gaps = trendsmod.latest_gaps()
    gap_html = (f"<h3>▸ 该补什么（模拟·快照 {esc(gap_date)}）</h3><ul>" + "".join(
        f"<li><b>{esc(g['skill'])}</b> → +{g['jobs']} 岗 / {g['companies']} 家</li>" for g in gaps) + "</ul>") if gaps else ""
    return f"<h2>落地：岗位与技能</h2>{''.join(blocks)}{gap_html}" if blocks else ""


@app.get("/trends", response_class=HTMLResponse)
def trends_page(conn=Depends(view_conn)):
    """鸭先知：市场趋势页。纯本地统计（app/trends.py），15 分钟缓存 —— 9k 份 JD 的
    正则扫描要几秒，不该每次刷新都付一遍。数据每日随扫描更新，周报周一另发邮箱。"""
    import time as _time
    from html import escape as esc
    if _time.time() - _trends_cache["at"] < 900 and _trends_cache["html"]:
        return _page("Trends", _trends_cache["html"])
    from . import trends as trendsmod
    data = trendsmod.compute(cfg, conn)
    sug = trendsmod.suggestions(cfg, conn, data)
    _conf = {"high": "高", "medium": "中", "low": "⚠低"}
    _hn = (data.get("hn_hiring") or {}).get("shares", {})
    rows_html = "".join(
        f"<tr><td>{esc(n)}</td><td class='num'>{v['count']}</td><td class='num'>{v['companies']}</td>"
        f"<td class='num'>{v['top3_share_pct']}%</td><td class='num'>{_conf[v.get('confidence','medium')]}</td>"
        f"<td class='num'><b>{v['median_fill'] or '—'}</b>{'天(' + str(v['n_closed_cos']) + '家)' if v['median_fill'] else ''}</td>"
        f"<td class='num'>{v['median_age']}天</td><td class='num'>{v['ghost_pct']}%</td>"
        f"<td class='num'>{v['new7']}</td><td class='num'>{v['new_share_delta_pp']:+.1f}pp</td>"
        f"<td class='num'>{str(_hn.get(n)) + '%' if _hn.get(n) is not None else '—'}</td></tr>"
        for n, v in sorted(data["themes"].items(),
                           key=lambda kv: -(kv[1]["median_fill"] or kv[1]["median_age"] or 0)))
    novel = "".join(
        f"<tr><td><code>{esc(t['term'])}</code></td><td class='num'>{t['new_docs']}</td>"
        f"<td class='num'>{t['companies']}</td><td class='num'>{t['ratio'] or '∞'}×</td></tr>"
        for t in data["novel_terms"]) or "<tr><td colspan=4 class='muted'>本期无达标新词</td></tr>"
    velo = "".join(
        f"<tr><td>{esc(c['company'])}</td><td class='num'>{c['new7']}</td><td class='num'>{c['weekly_avg']}</td></tr>"
        for c in data["company_velocity"]) or "<tr><td colspan=3 class='muted'>无显著突增（新接入观察期公司不计）</td></tr>"
    rank = "".join(
        f"<li><b>{esc(r['theme'])}</b> · 指数 {r['undersupply_index']}（挂龄 {r['median_age']}天，"
        f"环比 {r['delta_pp']:+.1f}pp）{('→ 主打 <b>' + esc(r['angle']) + '</b> angle') if r['angle'] else ''}</li>"
        for r in sug["ranking"][:5])
    sentinels = "".join(
        f"<li><b>{esc(o['company'])}</b> 首招「{esc(o['title'])}」</li>"
        for o in data.get("sentinels", [])) or "<li class='muted'>本周无</li>"
    vendors = "".join(
        f"<tr><td>{esc(x['vendor'])}</td><td class='num'>{x['mentions']}</td>"
        f"<td class='num'>{x['companies']}</td><td class='num'>{x['new14']}</td></tr>"
        for x in data.get("vendors", []))
    hot = "".join(
        f"<li>{'📄' if p['resume_ready'] else '⭘'} {esc(p['company'])} — {esc(p['title'])}（{p['score']} 分）</li>"
        for p in sug["hot_roles"])
    body = f"""<h1>🦆 鸭先知 · {data['date']}</h1>
      <p class="tip">语料 {data['corpus']} 条在库岗位。<b>挂龄</b>越长越招不到人（供不应求）；
      <b>环比</b> = 近14天新岗中该主题份额对比前14天。数据每日随 09:00 扫描更新；周报每周一寄邮箱。</p>
      <h2>主题热度与供需</h2>
      <table><tr><th>主题</th><th>在库</th><th>公司数</th><th>top3集中</th><th>置信度</th><th>完结耗时</th><th>挂龄中位</th><th>幽灵岗</th><th>近7新增</th><th>环比</th><th>HN帖</th></tr>{rows_html}</table>
      <p class="muted small">完结耗时按公司平衡（每家先取中位再跨公司中位，大板子刷不动权重）；置信度 ⚠低 = &lt;15家公司或 top3&gt;60%，代表少数公司而非市场；HN帖 = 当月 Who is hiring 帖里该主题占招聘评论的比例（创业公司侧全市场对照）。</p>
      <h2>新词雷达（近14天，≥4家公司）</h2>
      <table><tr><th>词</th><th>新岗提及</th><th>公司数</th><th>倍率</th></tr>{novel}</table>
      <h2>公司发岗突变（近7天 ≥3× 周均）</h2>
      <table><tr><th>公司</th><th>近7天</th><th>此前周均</th></tr>{velo}</table>
      <h2>哨兵岗位（公司首招此类 title）</h2><ul>{sentinels}</ul>
      <h2>厂商提及榜（JD 点名 = 采用意图）</h2>
      <table><tr><th>产品</th><th>提及</th><th>公司数</th><th>近14天新岗</th></tr>{vendors}</table>
      <h2>行动建议</h2><ul>{rank}</ul>
      {f'<p><b>最缺人主题「{esc(sug["hot_theme"])}」现成可投：</b></p><ul>{hot}</ul>' if hot else ''}
      {_trends_details_html(conn, data)}
      <p class="muted small">词典在 config.yaml trends.themes，手改即生效。</p>"""
    _trends_cache.update(at=_time.time(), html=body)
    return _page("Trends", body)


@app.get("/outcomes", response_class=HTMLResponse)
def outcomes_page():
    """Client-rendered: the scan is an IMAP round-trip, so it runs on demand, not on page load."""
    return _page("Outcomes", """<h1>📥 Outcomes from the inbox</h1>
      <p class="tip">Reads the mailbox for rejections, interview invites, assessments and offers,
      and matches each against your open applications. <b>Nothing is written until you click.</b>
      A rejection email names a company but rarely says which of its roles, so anything
      ambiguous asks you to pick.</p>
      <p><button onclick="scan()">Scan inbox</button>
         <select id="days"><option>60</option><option selected>120</option><option>180</option></select>
         <span class="small muted">days back</span></p>
      <div id="out"></div>
      <script>
      async function scan(){
        const o=document.getElementById('out'); o.innerHTML='<p class="muted">scanning…</p>';
        const r=await fetch('/api/outcomes?days='+document.getElementById('days').value);
        if(!r.ok){o.innerHTML='<p class="tip warn">'+await r.text()+'</p>';return}
        const d=await r.json();
        if(!d.proposals.length){o.innerHTML='<p class="muted">scanned '+d.scanned+' emails, no outcome signals.</p>';return}
        const cls={high:'hi',medium:'md',low:'lo'};
        const rank={high:0,medium:1,low:2};
        d.proposals.sort((a,b)=>rank[a.confidence]-rank[b.confidence]||a.date.localeCompare(b.date));
        o.innerHTML='<p class="small muted">scanned '+d.scanned+' emails · '+d.proposals.length+' signals</p>'+
          d.proposals.map((p,i)=>'<div style="border-bottom:1px solid #eef2f6;padding:9px 0">'+
            '<span class="pill '+cls[p.confidence]+'">'+p.confidence+'</span> '+
            '<b>'+p.company+'</b> · '+p.signal+' · '+p.date+
            '<div class="small muted">'+esc(p.subject)+'</div>'+
            p.roles.map(r=>'<div style="margin-top:4px">'+
              '<button onclick=\\'commit('+i+',"'+r.job_id+'",this)\\'>record</button> '+
              '<span class="small">'+esc(r.title)+'</span> '+
              '<span class="small muted">'+r.job_id+'</span></div>').join('')+
          '</div>').join('');
        window._props=d.proposals;
      }
      function esc(s){const e=document.createElement('div');e.textContent=s;return e.innerHTML}
      async function commit(i,job_id,btn){
        const p=window._props[i];
        const body={stage:p.stage||(p.signal==='rejection'?null:'responded'),
                    outcome:p.outcome||null,note:p.signal+': '+p.subject,when:p.date};
        const r=await fetch('/api/roles/'+encodeURIComponent(job_id)+'/signal',
          {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        btn.textContent=r.ok?'recorded ✓':'failed'; btn.disabled=true;
      }
      </script>""")


@app.get("/api/followups")
def api_followups(conn=Depends(view_conn)):
    return {"quiet": followupmod.quiet(cfg, conn), "tuning": followupmod.tuning(cfg)}


@app.post("/api/followups/{job_id}/draft")
def api_followup_draft(job_id: str, channel: str = "email", conn=Depends(view_conn)):
    """Draft a follow-up note. Drafting only — this endpoint cannot send, and no endpoint here
    sends a follow-up; the owner copies it out or mails it herself."""
    try:
        return followupmod.draft(cfg, conn, job_id, channel=channel)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"draft failed: {type(e).__name__}: {e}")


class FollowupLogIn(BaseModel):
    channel: str = "email"
    to_addr: str | None = None
    subject: str = ""
    body: str


@app.post("/api/followups/{job_id}/log")
def api_followup_log(job_id: str, body: FollowupLogIn, conn=Depends(view_conn)):
    """Record that a nudge went out — this is what keeps the two-per-application ceiling and the
    days-quiet arithmetic honest. Call it when you actually send, not when you draft."""
    if not store.get_role(conn, job_id):
        raise HTTPException(404, "unknown job_id")
    seq = store.followup_add(conn, job_id, channel=body.channel, to_addr=body.to_addr,
                             subject=body.subject, body=body.body)
    store.followup_mark_sent(conn, job_id, seq)
    return {"ok": True, "seq": seq}


# Kept out of the page f-string below — JS braces and f-strings don't mix.
_FOLLOWUP_JS = """
<script>
let cur=null;
async function draft(job_id,btn){
  const d=document.getElementById('draft');
  btn.disabled=true; btn.textContent='drafting…';
  const r=await fetch('/api/followups/'+encodeURIComponent(job_id)+'/draft',{method:'POST'});
  btn.disabled=false; btn.textContent='draft';
  if(!r.ok){d.innerHTML='<p class="tip warn">'+esc(await r.text())+'</p>';return}
  cur=await r.json();
  const to=cur.contact?(cur.contact.addr+'  ['+cur.contact.source+']')
                      :('no reply address — '+cur.note);
  d.innerHTML='<h2>'+esc(cur.company)+' — '+esc(cur.title)+' (nudge #'+cur.seq_would_be+')</h2>'+
    '<p class="small">to: '+esc(to)+'<br>subject: '+esc(cur.subject)+'</p>'+
    '<pre id="body">'+esc(cur.body)+'</pre>'+
    '<button onclick="copyIt()">copy</button> '+
    '<button class="g" onclick="logIt(this)">I sent this</button> '+
    '<span class="small muted">logging it is what keeps the 2-nudge cap correct</span>';
  d.scrollIntoView({behavior:'smooth'});
}
function esc(s){const e=document.createElement('div');e.textContent=s||'';return e.innerHTML}
function copyIt(){navigator.clipboard.writeText(document.getElementById('body').textContent)}
async function logIt(btn){
  const r=await fetch('/api/followups/'+encodeURIComponent(cur.job_id)+'/log',
    {method:'POST',headers:{'Content-Type':'application/json'},
     body:JSON.stringify({channel:cur.channel,to_addr:cur.contact?cur.contact.addr:null,
                          subject:cur.subject,body:document.getElementById('body').textContent})});
  btn.textContent=r.ok?'logged ✓':'failed'; btn.disabled=true;
}
</script>"""


@app.get("/followups", response_class=HTMLResponse)
def followups_page(conn=Depends(view_conn)):
    rows = followupmod.quiet(cfg, conn)
    t = followupmod.tuning(cfg)
    if not rows:
        return _page("Follow-ups", f'<h1>⏳ Gone quiet</h1><p class="tip">Nothing has been quiet '
                                   f'for {t["after_days"]}+ days yet.</p>')
    items = "".join(
        f'<tr><td class="n">{r["weighted_score"] or 0:.0f}</td>'
        f'<td class="n">{r["days_quiet"]}d</td><td class="n">{r["followups_sent"]}</td>'
        f'<td><b>{r["company"]}</b><div class="small muted">{r["title"][:70]}</div></td>'
        f'<td><button onclick=\'draft("{r["job_id"]}",this)\'>draft</button></td></tr>'
        for r in rows)
    return _page("Follow-ups", f"""<h1>⏳ Gone quiet</h1>
      <p class="tip">Applied {t['after_days']}+ days ago, still no reply. The note is drafted from
      the résumé you actually submitted and the posting — nothing else, so it can't introduce a
      claim they haven't already read. <b>Nothing sends automatically.</b> Max
      {t['max_per_app']} per application; after the second silence, record <code>no_response</code>
      instead of chasing a third time.</p>
      <table><tr><th class="n">score</th><th class="n">quiet</th><th class="n">sent</th>
      <th>role</th><th></th></tr>{items}</table>
      <div id="draft"></div>""" + _FOLLOWUP_JS)


# ---------------------------------------------------------------- interviews (复盘 ⇄ pipeline)
#
# The markdown under 面试复盘/ stays the system of record (the interview-review skill writes
# it; the atoms cron watches it). This page is the INDEX: every interview, its questions and
# open action items, linked into the role it belongs to. Read-only on the files.

_IV_BASE = None


def _iv_base(cfg_) -> Path:
    global _IV_BASE
    if _IV_BASE is None:
        iv = cfg_.get("interviews") or {}
        _IV_BASE = Path(iv.get("dir") or (cfgmod.ROOT.parent / "面试复盘")).resolve()
    return _IV_BASE


@app.post("/api/interviews/ingest")
def api_interviews_ingest(conn=Depends(view_conn)):
    from . import interview_ingest as ivmod
    return {"ok": True, **ivmod.ingest(cfg, conn)}


class LinkIn(BaseModel):
    job_id: str | None = None            # null unlinks


@app.post("/api/interviews/{marker_id}/link")
def api_interview_link(marker_id: str, body: LinkIn, conn=Depends(view_conn)):
    if body.job_id and not store.get_role(conn, body.job_id):
        raise HTTPException(404, "unknown job_id")
    store.interview_link(conn, marker_id, body.job_id)
    if body.job_id:
        st = conn.execute("SELECT status FROM role_state WHERE job_id=?", (body.job_id,)).fetchone()
        if st and st["status"] in ("applied", "interviewing"):
            store.record_signal(conn, body.job_id, stage="interview")
    return {"ok": True}


@app.get("/interviews/file", response_class=HTMLResponse)
def interviews_file(f: str):
    """Serve one review markdown as plain text. Confined to the 面试复盘 tree — resolve() then
    require the base as prefix, so ../ and symlink tricks can't read outside it."""
    base = _iv_base(cfg)
    p = (base / f).resolve()
    if not str(p).startswith(str(base) + "/") or not p.is_file():
        raise HTTPException(404, "no such review file")
    return HTMLResponse(f"<meta charset='utf-8'><pre style='white-space:pre-wrap;font:13px/1.6 "
                        f"ui-monospace,monospace;max-width:900px;margin:24px auto;padding:0 16px'>"
                        f"{p.read_text(encoding='utf-8').replace('&', '&amp;').replace('<', '&lt;')}</pre>")


# ---- 复盘定性抽取（渲染时读 md，不落库）-------------------------------------
# 为什么不在 ingest 里解析成一个字段：定性的写法还在演化（本轮定性 / 一句话总结 /
# 一句话结论 / 内联加粗，见 42 个复盘文件的实际分布），落库就等于把当下的写法冻进
# schema，以后改一次格式要跑一次迁移。渲染时读几十个小 md 对本地单人工具毫无成本。
_GIST_H = re.compile(r"^>?\s*#{1,4}\s*(?:📌\s*)?(?:本轮定性|一句话总结|一句话结论|结论先行)")
_GIST_H0 = re.compile(r"^>?\s*#{1,4}\s*\d+\.\s*(?:一句话结论|一句话总结)")
_GIST_INLINE = re.compile(r"^>?\s*\*\*本轮定性[:：]\*\*\s*(?P<t>.+?)\s*$")
def _plain(s: str) -> str:
    """Strip leading blockquote/heading markers (repeatedly — `> ### x` needs two passes,
    and ^-anchored subs only fire at position 0) plus inline emphasis."""
    s = s.strip()
    while True:
        t = re.sub(r"^#{1,6}\s*", "", re.sub(r"^>+\s*", "", s))
        if t == s:
            break
        s = t
    return re.sub(r"[*_`]", "", s).strip()


def _iv_index(base: Path) -> dict[str, str]:
    """basename -> path RELATIVE TO base, for every recap md outside _archive.

    Needed because `interviews.sources` stores bare filenames while the files actually live in
    per-company subfolders (`Coach_Sessions/2026-08-16_….md`). Linking the bare name 404s.
    """
    out: dict[str, str] = {}
    try:
        for p in sorted(base.rglob("*.md")):
            if "_archive" in p.parts:
                continue
            out.setdefault(p.name, str(p.relative_to(base)))
    except OSError:
        pass
    return out


def _central_files(cfg: dict) -> set[str]:
    """The legacy card-source files. They are NOT a recap — linking 读复盘 at
    `Interview Action Items.md` is how the button used to open a 3000-line action dump."""
    iv = cfg.get("interviews") or {}
    names = iv.get("files") or {"action": "Interview Action Items.md",
                                "tech_q": "Technical_Interview_Questions.md",
                                "ai_q": "AI_Agent_Interview_Questions.md"}
    return set(names.values())


def _recap_target(base: Path, index: dict[str, str], review_path: str,
                  sources, central: set[str]) -> str:
    """Best EXISTING relative path to this interview's recap, or '' if there isn't one."""
    for c in [review_path] + [s for s in sources if s not in central]:
        if not c:
            continue
        if (base / c).is_file():
            return c
        hit = index.get(Path(c).name)          # bare name -> real subfolder path
        if hit:
            return hit
    return ""


def _gist(base: Path, fnames) -> str:
    """First substantive line of a recap's verdict block, as plain text ('' if none).

    Reads only files under the 面试复盘 tree (same containment rule as /interviews/file).
    """
    for fn in fnames:
        if not fn:
            continue
        try:
            p = (base / fn).resolve()
            if not str(p).startswith(str(base) + "/") or not p.is_file():
                continue
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            m = _GIST_INLINE.match(line)
            if m:
                return _plain(m.group("t"))[:240]
            if not (_GIST_H.match(line) or _GIST_H0.match(line)):
                continue
            # walk forward to the first line with real content under that heading.
            # A `> ### ...` line is CONTENT, not the next section — she writes the verdict
            # itself as a blockquoted sub-heading (see Freshworks 复盘). Only a heading at
            # column 0 ends the block.
            for nxt in lines[i + 1:i + 12]:
                if re.match(r"^#{1,4}\s", nxt):               # ran into the next section
                    break
                t = _plain(nxt)
                if not t or t.startswith("---") or t.startswith("|"):
                    continue
                return t[:240]
    return ""


@app.get("/interviews", response_class=HTMLResponse)
def interviews_page(conn=Depends(view_conn)):
    rows = store.interviews_overview(conn)
    if not rows:
        return _page("Interviews", '<h1>🎤 Interviews</h1><p class="tip">Nothing indexed yet — '
                     'run <code>python -m app.interview_ingest</code> after a 复盘.</p>')
    # candidates for the link dropdowns: her active pipeline, newest first
    cands = [dict(r) for r in conn.execute(
        "SELECT r.job_id, r.company, r.title FROM jobs.roles r JOIN role_state s"
        " ON s.job_id=r.job_id WHERE s.status IN ('applied','interviewing')"
        " ORDER BY s.applied_date DESC LIMIT 400")]
    opts = "".join(f'<option value="{c["job_id"]}">{c["company"]} — {c["title"][:60]}</option>'
                   for c in cands)
    base = _iv_base(cfg)
    index, central = _iv_index(base), _central_files(cfg)
    n_q = sum(r["n_questions"] for r in rows)

    blocks = []
    for r in rows:
        items = store.interview_items_for(conn, r["marker_id"])
        acts = [i for i in items if i["kind"] == "action"]
        qs = [i for i in items if i["kind"] in ("tech_q", "ai_q")]
        act_html = "".join(
            f'<li class="{ "done" if i["done"] else "" }">{i["text"]}</li>' for i in acts)
        q_html = "".join(f'<li>{"🤖 " if i["kind"] == "ai_q" else ""}{i["text"]}</li>' for i in qs)
        src = list(json.loads(r["sources"] or "{}"))
        # 复盘正文是这一页的主角。只在文件真的存在时才给按钮 —— 死链比没有按钮更糟。
        recap_fn = _recap_target(base, index, r["review_path"] or "", src, central)
        gist = _gist(base, [recap_fn]) if recap_fn else ""
        read = (f'<a class="read" href="/interviews/file?f={quote(recap_fn)}" target="_blank">'
                f'📄 读复盘</a>' if recap_fn else '')
        others = [index.get(Path(fn).name, fn) for fn in src]
        extra = " · ".join(
            f'<a href="/interviews/file?f={quote(fn)}" target="_blank">{fn.split("/")[-1][:24]}</a>'
            for fn in dict.fromkeys(others) if fn != recap_fn and (base / fn).is_file())
        if r["transcript_url"]:
            extra = (f'<a href="{r["transcript_url"]}" target="_blank" rel="noopener">🎙 Otter</a>'
                     + (" · " + extra if extra else ""))
        role_tag = (f'<span class="pill hi">{r["role_company"]} — {(r["role_title"] or "")[:40]}</span>'
                    if r["job_id"] else
                    f'<select class="lk" data-m="{r["marker_id"]}"><option value="">link to a role…</option>{opts}</select>')
        blocks.append(f"""
        <div class="iv">
          <div class="ivh"><b>{r['company']}</b> · {r['happened_on']} · {r['round_type'] or '—'}
            {role_tag} {read}
            <span class="small muted">{extra}</span></div>
          {f'<div class="gist">{gist}</div>' if gist else ''}
          {f'<details open><summary class="small">他问了什么（{len(qs)}）</summary><ul class="ivl">{q_html}</ul></details>' if qs else ''}
          {f'<details><summary class="small muted">当时记的 action items（{len(acts)}）</summary><ul class="ivl dim">{act_html}</ul></details>' if acts else ''}
        </div>""")

    return _page("Interviews", f"""<h1>🎤 面试复盘</h1>
      <p class="tip">{len(rows)} 场面试 · {n_q} 道被问过的题已入库。索引自 <code>面试复盘/</code>
      （markdown 是唯一事实源，这里只读）。<br>
      这一页回答两个问题：<b>那场发生了什么</b>（定性 + 读复盘），和 <b>他们问过什么</b>（可复用的题库）。
      跑一次 ingest 同步。</p>
      <p><button onclick="ing(this)">re-ingest from 面试复盘/</button></p>
      <style>.iv{{border-bottom:1px solid #eef2f6;padding:12px 0}}.ivh{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
      .ivl{{margin:6px 0 2px;padding-left:18px}}.ivl li{{margin:3px 0}}.ivl li.done{{color:#94a3b8}}
      .ivl.dim li{{color:#94a3b8}}
      /* 定性：这一页真正要被读到的一行 */
      .gist{{margin:7px 0 4px;padding:7px 11px;border-left:3px solid #cbd5e1;background:#f8fafc;
        border-radius:0 5px 5px 0;font-size:.93em;line-height:1.55;color:#334155}}
      a.read{{font-size:12px;text-decoration:none;background:#eef2ff;color:#4338ca;
        padding:2px 9px;border-radius:999px;white-space:nowrap}}
      a.read:hover{{background:#e0e7ff}}
      select.lk{{font:12px -apple-system,sans-serif;max-width:260px}}</style>
      {''.join(blocks)}
      <script>
      document.querySelectorAll('select.lk').forEach(s=>s.onchange=async()=>{{
        if(!s.value) return;
        const r=await fetch('/api/interviews/'+encodeURIComponent(s.dataset.m)+'/link',
          {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{job_id:s.value}})}});
        if(r.ok) location.reload(); else alert(await r.text());
      }});
      async function ing(b){{b.disabled=true;b.textContent='ingesting…';
        const r=await fetch('/api/interviews/ingest',{{method:'POST'}});
        if(r.ok) location.reload(); else {{alert(await r.text());b.disabled=false;}}}}
      </script>""")


@app.get("/api/companies")
def api_companies(refresh: bool = False):
    """Per-company board off the mail threads. `refresh` costs an IMAP pass (~2 min); without
    it this reads the cache the labelling run leaves behind, which is what makes it page-load
    cheap. Read-only."""
    try:
        return boardmod.load(cfg, refresh=refresh)
    except Exception as e:
        raise HTTPException(502, f"board failed: {type(e).__name__}: {e}")


class _OverrideIn(BaseModel):
    company: str
    action: str | None = None      # closed | hidden | booked | None = 撤销


@app.post("/api/companies/override")
def companies_override(inp: _OverrideIn, conn=Depends(view_conn)):
    """人工改写公司行。机器从邮件读出的只是提议——这里是人拍板的地方。"""
    if inp.action not in (None, "closed", "hidden", "booked", "acked"):
        raise HTTPException(422, "action must be closed / hidden / booked / acked / null")
    store.set_company_override(conn, inp.company, inp.action)
    return {"ok": True}


class _CooldownIn(BaseModel):
    company: str
    days: int | None = None        # 30 / 60 / 90 / 180 … (1..365); ignored when `until` is given
    until: str | None = None       # ISO date, inclusive
    reason: str = ""
    skip_job_id: str | None = None # extension panel (owner 2026-09-25): also mark THIS role skip


@app.get("/api/companies/cooldowns")
def api_cooldowns(conn=Depends(view_conn)):
    """Active cooldowns — config.yaml ones and UI-set ones merged (store.active_cooldowns)."""
    cds = store.active_cooldowns(conn, cfg)
    rows = [{"company_norm": cn, **cd} for cn, cd in cds.items()]
    rows.sort(key=lambda r: (r["until"], r["company"].lower()))
    return {"today": store.la_today(), "rows": rows}


@app.post("/api/companies/cooldown")
def api_cooldown_set(inp: _CooldownIn, conn=Depends(view_conn)):
    """「这家不让投了」(owner 2026-09-22). Same effect as a config.yaml `cooldowns:` entry —
    the scanner skips the company and review hides its roles — without editing YAML, and it
    expires by itself. Returns the row plus how many review-status roles just went hidden."""
    try:
        row = store.set_company_cooldown(conn, inp.company, days=inp.days, until=inp.until,
                                         reason=inp.reason)
    except ValueError as e:
        raise HTTPException(422, str(e))
    hidden = conn.execute(
        "SELECT COUNT(*) FROM jobs.roles r LEFT JOIN role_state s ON s.job_id=r.job_id"
        " WHERE r.company_norm=? AND COALESCE(s.status,'new')='new'",
        (row["company_norm"],)).fetchone()[0]
    skipped = False
    if inp.skip_job_id:
        cur = store.get_role(conn, inp.skip_job_id)
        if cur and (cur.get("status") or "new") == "new":     # never overwrite applied/interviewing history
            store.set_status(conn, inp.skip_job_id, "skip")
            skipped = True
    return {"ok": True, **row, "hidden_roles": hidden, "skipped_role": skipped}


@app.delete("/api/companies/cooldown/{company}")
def api_cooldown_clear(company: str, conn=Depends(view_conn)):
    if store.clear_company_cooldown(conn, company):
        return {"ok": True}
    if cfgmod.norm_key(company) in cfgmod.active_cooldowns(cfg):
        raise HTTPException(409, f"{company} 的 cooldown 写在 config.yaml 里 — 要解除请改那里")
    raise HTTPException(404, "no manual cooldown for that company")


# ── Outreach 自荐信（owner 2026-09-23）：联系人 → 匹配岗位 → 草稿 → Gmail 草稿箱 ───────────────

class _OutreachGen(BaseModel):
    email: str


class _OutreachEdit(BaseModel):
    subject: str | None = None
    body: str | None = None


@app.get("/api/outreach")
def api_outreach(all: bool = False, conn=Depends(view_conn)):
    """Contacts (list A/B tiers + mailbox recruiters), each with the company's matching open roles
    for this workspace and its draft (if any)."""
    tiers = set((cfg.get("outreach") or {}).get("tiers") or ["A", "B"]) | {"mailbox"}
    by = outreachmod.roles_by_company(conn, cfg)
    drafts = {d["contact_email"]: d for d in store.outreach_list(conn)}
    rows = []
    for c in outreachmod.all_contacts(cfg):
        if not all and c.get("tier") not in tiers:
            continue
        _, roles = outreachmod.match_company(by, c["company"])
        d = drafts.get(c["email"])
        rows.append({**c, "roles": [outreachmod.role_brief(r) for r in roles[:3]], "n_roles": len(roles),
                     "draft": {k: d[k] for k in ("id", "status", "subject", "updated_at", "saved_at", "error")} if d else None})
    rows.sort(key=lambda r: (r["source"] != "mailbox", -r["n_roles"], r.get("tier", ""), r["company"].lower()))
    return {"workspace": cfgmod.workspace_name(cfg), "mailbox_on": mailmod.available(cfg),
            "ai_on": aimod.available(cfg), "rows": rows}


@app.get("/api/contacts")
def api_contacts():
    """A plain address book view of every saved hiring contact."""
    rows = outreachmod.all_contacts(cfg)
    rows.sort(key=lambda c: (c.get("company", "").lower(), c.get("name", "").lower(), c["email"]))
    return {"workspace": cfgmod.workspace_name(cfg), "rows": rows}


@app.post("/api/outreach/generate")
def api_outreach_generate(inp: _OutreachGen, conn=Depends(view_conn)):
    contact = next((c for c in outreachmod.all_contacts(cfg) if c["email"] == inp.email.strip().lower()), None)
    if not contact:
        raise HTTPException(404, "unknown contact")
    if not aimod.available(cfg):
        raise HTTPException(503, "AI disabled (ai.enabled + secrets.yaml key)")
    rec = outreachmod.generate(conn, cfg, contact)
    if rec["status"] == "error":
        raise HTTPException(502, rec["error"])
    return rec


@app.get("/api/outreach/{draft_id}")
def api_outreach_get(draft_id: int, conn=Depends(view_conn)):
    rec = store.outreach_get(conn, draft_id)
    if not rec:
        raise HTTPException(404, "no such draft")
    return rec


@app.post("/api/outreach/{draft_id}")
def api_outreach_edit(draft_id: int, inp: _OutreachEdit, conn=Depends(view_conn)):
    rec = store.outreach_update(conn, draft_id, **{k: v for k, v in inp.model_dump().items() if v is not None})
    if not rec:
        raise HTTPException(404, "no such draft")
    return rec


@app.post("/api/outreach/{draft_id}/gmail")
def api_outreach_gmail(draft_id: int, conn=Depends(view_conn)):
    """Put the draft in the mailbox's Gmail Drafts folder (résumé attached). Never sends."""
    rec = store.outreach_get(conn, draft_id)
    if not rec:
        raise HTTPException(404, "no such draft")
    if not (rec.get("subject") and rec.get("body")):
        raise HTTPException(400, "draft has no subject/body yet")
    # Re-resolve the attachment now: tailored filenames change on re-score, and a stale stored
    # path silently produced attachment-less drafts (2026-09-23).
    fresh = outreachmod.current_resume(conn, cfg, rec)
    if fresh:
        rec["resume_path"] = fresh
        store.outreach_update(conn, draft_id, resume_path=fresh)
    try:
        mid = outreachmod.save_gmail_draft(cfg, rec)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"could not save to Gmail Drafts: {type(e).__name__}: {e}")
    return store.outreach_update(conn, draft_id, status="saved", saved_at=store.la_now_iso(), error="") | {"message_id": mid}


@app.post("/api/outreach/{draft_id}/dismiss")
def api_outreach_dismiss(draft_id: int, conn=Depends(view_conn)):
    rec = store.outreach_update(conn, draft_id, status="dismissed")
    if not rec:
        raise HTTPException(404, "no such draft")
    return rec


@app.get("/outreach", response_class=HTMLResponse)
def outreach_page():
    return _page("Outreach", """<h1>✉️ 自荐信草稿</h1>
      <p><a class="b" href="/contacts">👥 打开联系人通讯录</a></p>
      <p class="tip">联系人来自两处：<b>邮箱里聊过的 recruiter</b>（/companies 看板里活着的线程）和
      <b>outreach/contacts.yaml</b>（ex-Meta 名单，A = 500+ 人、B = 50-500 人）。每个人对应这家公司现在对我合格的岗位。
      「生成」写一封四段式草稿（为什么是这家 · 我是谁 · 附简历+点名岗位 · 约 15 分钟），你改完点「存进 Gmail 草稿箱」，
      然后<b>在 Gmail 里自己发</b>——这页永远不发信。</p>
      <p><button onclick="load()">Load</button>
         <button onclick="genAll()" id="genall">生成所有还没草稿的（勾选的）</button>
         <button onclick="gmailAll()" id="gmailall">全部存进 Gmail 草稿箱</button>
         <label class="small muted"><input type="checkbox" id="showall" onchange="load()"> 显示 skip 档（中介 / 非美国 / 只招 PM / 太小）</label>
         <span class="small muted" id="prog"></span></p>
      <div id="out"><p class="muted">loading…</p></div>
      <script>
      function esc(s){const e=document.createElement('div');e.textContent=s||'';return e.innerHTML}
      async function load(){
        const r=await fetch('/api/outreach?all='+(document.getElementById('showall').checked?'true':'false'));
        if(!r.ok){document.getElementById('out').innerHTML='<p class="tip warn">'+esc(await r.text())+'</p>';return}
        window._o=await r.json(); render();
      }
      function stBadge(d){ if(!d) return '<span class="muted">—</span>';
        const zh={draft:'📝 草稿',saved:'📬 已在 Gmail 草稿箱',dismissed:'✕ 不发',error:'⚠ 失败'};
        return '<span title="'+esc(d.error||'')+'">'+(zh[d.status]||d.status)+'</span>'; }
      function render(){
        const o=window._o; if(!o) return;
        const head='<p class="small muted">工作台 <b>'+esc(o.workspace||'default')+'</b> · 邮箱 '+(o.mailbox_on?'✅':'关（存草稿箱不可用）')+' · AI '+(o.ai_on?'✅':'关')+' · '+o.rows.length+' 位联系人</p>';
        const rows=o.rows.map((c,i)=>{
          const roles=c.roles.length?c.roles.map(r=>'<div class="small">· <a href="'+esc(r.apply_url)+'" target="_blank">'+esc(r.title)+'</a> <span class="muted">'+esc(r.location||'')+(r.tailor_status==='ready'?' · 📄 有定制简历':'')+'</span></div>').join('')+(c.n_roles>3?'<div class="small muted">…共 '+c.n_roles+' 个</div>':'')
                                     :'<div class="small muted">JobScan 里没有这家的合格岗位（草稿会改成问「现在开什么工程岗」）</div>';
          const d=c.draft;
          // data-email instead of a quoted literal: this template is a plain Python string, so a JS
          // \' here reached the browser as '' and the whole script died with "Unexpected string" —
          // the page sat on "loading…" forever, on both workspaces (2026-09-23 night).
          const act=[ '<a href="#" data-email="'+esc(c.email)+'" onclick="return gen(this.dataset.email,'+i+')">'+(d?'重新生成':'生成')+'</a>' ];
          if(d&&d.status!=='error'){ act.push('<a href="#" onclick="return openDraft('+d.id+')">查看/改</a>'); if(d.status!=='saved') act.push('<a href="#" onclick="return toGmail('+d.id+')">存进 Gmail 草稿箱</a>'); act.push('<a href="#" onclick="return dismiss('+d.id+')">不发</a>'); }
          return '<div id="row'+i+'" style="border-bottom:1px solid #eef2f6;padding:10px 0">'+
            '<label><input type="checkbox" class="pick" data-email="'+esc(c.email)+'" '+(d&&d.status!=='error'?'':'checked')+'> <b style="font-size:15px">'+esc(c.company)+'</b></label> '+
            '<span class="pill '+(c.source==='mailbox'?'hi':(c.tier==='A'?'md':'lo'))+'">'+esc(c.source==='mailbox'?'邮箱聊过':'档 '+c.tier)+'</span> '+
            '<span class="small muted">'+esc(c.name)+' &lt;'+esc(c.email)+'&gt;'+(c.size?' · '+esc(c.size):'')+(c.industry?' · '+esc(c.industry):'')+'</span>'+
            '<div class="small muted" style="margin:3px 0 4px">'+esc((c.note||'').slice(0,220))+'</div>'+roles+
            '<div class="small" style="margin-top:4px">'+stBadge(d)+' · '+act.join(' · ')+'</div>'+
            '<div id="draft'+(d?d.id:'x')+'" class="draftbox"></div></div>';
        }).join('');
        document.getElementById('out').innerHTML=head+rows;
      }
      async function gen(email,i){
        const row=document.getElementById('row'+i); if(row) row.style.opacity=.5;
        const r=await fetch('/api/outreach/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})});
        if(!r.ok) alert('生成失败：'+await r.text());
        await load(); return false;
      }
      async function genAll(){
        const picks=[...document.querySelectorAll('.pick:checked')].map(x=>x.dataset.email);
        if(!picks.length){alert('没有勾选的联系人');return}
        if(!confirm('给 '+picks.length+' 位联系人各写一封草稿？每封约 10-20 秒。')) return;
        const b=document.getElementById('genall'); b.disabled=true; let ok=0,bad=0;
        for(const [k,email] of picks.entries()){
          document.getElementById('prog').textContent=(k+1)+'/'+picks.length+' '+email;
          const r=await fetch('/api/outreach/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})});
          if(r.ok) ok++; else bad++;
        }
        b.disabled=false; document.getElementById('prog').textContent='完成：'+ok+' 封草稿'+(bad?'，'+bad+' 封失败（行里有原因）':''); await load();
      }
      async function openDraft(id){
        const r=await fetch('/api/outreach/'+id); if(!r.ok){alert(await r.text());return false}
        const d=await r.json(); const box=document.getElementById('draft'+id); if(!box) return false;
        box.innerHTML='<div style="margin:8px 0;padding:10px;border:1px solid #dde3ea;border-radius:8px;background:#fafbfc">'+
          '<div class="small muted">To: '+esc(d.contact_name)+' &lt;'+esc(d.contact_email)+'&gt;'+(d.resume_path?' · 附件：'+esc(d.resume_path.split('/').pop()):' · 无附件')+'</div>'+
          '<input id="sub'+id+'" value="'+esc(d.subject).replace(/"/g,'&quot;')+'" style="width:100%;margin:6px 0;font:inherit;padding:4px">'+
          '<textarea id="body'+id+'" rows="14" style="width:100%;font:inherit;padding:6px">'+esc(d.body)+'</textarea>'+
          '<div style="margin-top:6px"><button onclick="saveDraft('+id+')">保存修改</button> <button onclick="toGmail('+id+')">存进 Gmail 草稿箱</button> <button onclick="collapse('+id+')">收起</button></div></div>';
        return false;
      }
      async function saveDraft(id){
        const r=await fetch('/api/outreach/'+id,{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({subject:document.getElementById('sub'+id).value,body:document.getElementById('body'+id).value})});
        if(!r.ok) alert(await r.text()); else document.getElementById('prog').textContent='已保存';
      }
      async function gmailAll(){
        // owner 2026-09-23 night: one click puts every finished draft (status 草稿) in the mailbox's
        // Gmail Drafts folder, one by one so a single IMAP hiccup fails one email, not the batch.
        const o=window._o; if(!o) return;
        const ids=o.rows.filter(c=>c.draft&&c.draft.status==='draft').map(c=>c.draft.id);
        if(!ids.length){ document.getElementById('prog').textContent='没有待存的草稿'; return; }
        if(!confirm('把 '+ids.length+' 封草稿全部放进 Gmail 草稿箱？（不会发送）')) return;
        let ok=0,bad=0; const errs=[];
        for(const id of ids){
          document.getElementById('prog').textContent='存进草稿箱 '+(ok+bad+1)+'/'+ids.length+'…';
          const r=await fetch('/api/outreach/'+id+'/gmail',{method:'POST'});
          if(r.ok) ok++; else { bad++; errs.push(await r.text()); }
        }
        document.getElementById('prog').textContent='已进 Gmail 草稿箱 '+ok+' 封'+(bad?'，失败 '+bad+' 封：'+errs.slice(0,3).join(' | '):'')+'。去 Gmail 一封封点发送。';
        await load();
      }
      async function toGmail(id){
        const sub=document.getElementById('sub'+id); if(sub) await saveDraft(id);
        const r=await fetch('/api/outreach/'+id+'/gmail',{method:'POST'});
        if(!r.ok) alert('存草稿箱失败：'+await r.text()); else { document.getElementById('prog').textContent='已进 Gmail 草稿箱，去 Gmail 发'; await load(); }
        return false;
      }
      function collapse(id){ const b=document.getElementById('draft'+id); if(b) b.innerHTML=''; }
      async function dismiss(id){ await fetch('/api/outreach/'+id+'/dismiss',{method:'POST'}); await load(); return false; }
      load();
      </script>""")


@app.get("/contacts", response_class=HTMLResponse)
def contacts_page():
    return _page("Contacts", """<h1>👥 Recruiter 联系人</h1>
      <p class="tip">这里是单纯的联系人通讯录。数据保存在 <code>outreach/contacts.yaml</code>，
      邮箱里已经聊过的 recruiter 也会自动合并进来。写信和生成草稿仍然去
      <a href="/outreach">外联页面</a>。</p>
      <p style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <input id="q" placeholder="搜索姓名、邮箱或公司" oninput="render()"
          style="min-width:280px;padding:7px 9px;border:1px solid #cbd5e1;border-radius:6px;font:inherit">
        <select id="company" onchange="render()"
          style="padding:7px 9px;border:1px solid #cbd5e1;border-radius:6px;font:inherit">
          <option value="">所有公司</option>
        </select>
        <span class="small muted" id="count"></span>
      </p>
      <div id="out"><p class="muted">loading…</p></div>
      <script>
      function esc(s){const e=document.createElement('div');e.textContent=s||'';return e.innerHTML}
      function kind(c){
        if(c.source==='mailbox') return '邮箱聊过';
        if((c.hiring||'').toLowerCase().includes('event')) return 'Hiring event';
        return 'Recruiter';
      }
      function render(){
        const rows=window._contacts||[];
        const q=(document.getElementById('q').value||'').trim().toLowerCase();
        const co=document.getElementById('company').value;
        const shown=rows.filter(c=>(!co||c.company===co)&&(!q||[c.name,c.email,c.company,c.note].join(' ').toLowerCase().includes(q)));
        document.getElementById('count').textContent='显示 '+shown.length+' / '+rows.length+' 位';
        const body=shown.map(c=>'<tr><td><b>'+esc(c.company)+'</b></td><td>'+esc(c.name||'—')+'</td>'+
          '<td><a href="mailto:'+encodeURIComponent(c.email)+'">'+esc(c.email)+'</a></td>'+
          '<td><span class="pill '+(c.source==='mailbox'?'hi':'lo')+'">'+esc(kind(c))+'</span></td>'+
          '<td class="small muted">'+esc(c.note||'')+'</td></tr>').join('');
        document.getElementById('out').innerHTML='<table><thead><tr><th>公司</th><th>姓名</th><th>Email</th><th>类型</th><th>备注</th></tr></thead><tbody>'+body+'</tbody></table>';
      }
      async function load(){
        const r=await fetch('/api/contacts');
        if(!r.ok){document.getElementById('out').innerHTML='<p class="tip warn">'+esc(await r.text())+'</p>';return}
        const data=await r.json(); window._contacts=data.rows||[];
        const companies=[...new Set(window._contacts.map(c=>c.company).filter(Boolean))].sort((a,b)=>a.localeCompare(b));
        document.getElementById('company').innerHTML='<option value="">所有公司</option>'+companies.map(c=>'<option>'+esc(c)+'</option>').join('');
        render();
      }
      load();
      </script>""")


@app.get("/companies", response_class=HTMLResponse)
def companies_page():
    """Whose turn is it, per company. Companion to the Gmail `Interviews/<Company>` labels —
    same thread grouping, so a row here and a label there are the same pile of mail."""
    return _page("Companies", """<h1>🏢 每家公司走到哪一步</h1>
      <p class="tip">按邮件线程聚合，一家公司一行 —— 和 Gmail 里的
      <code>Interviews/&lt;公司&gt;</code> 标签是同一批邮件。
      <b>🔴 该你回信</b> · <b>🟡 等太久了，该 follow up</b>。
      只显示<b>有真人回过音</b>的公司（纯投递回执的不上榜）。机器读错了就用每行的小按钮改：
      📅 已排期 / ✕ 结案 / 🙈 隐藏，随时可撤销。
      读缓存，秒开；<i>refresh</i> 才会重新读邮箱（约 2 分钟）。</p>
      <p><button onclick="load(false)">Load</button>
         <button onclick="load(true)">Refresh from Gmail</button>
         <label class="small muted"><input type="checkbox" id="dead" onchange="render()">
           显示已冷 / 已挂（超过 45 天没动静的）</label></p>
      <h2 id="cooldowns" style="margin-top:22px">⏳ Cooldown：不让投的公司</h2>
      <p class="tip">ATS 说「投太多了」/「一次只能一份在跑」→ 在这里记一笔（review 页每行公司旁的 🚫 也是同一个入口）：
      <b>扫描跳过这家、review 页隐藏它的岗位</b>，到期自动恢复。写在 config.yaml 里的也一并列出，那种只能改文件解除。</p>
      <form onsubmit="return cdAdd()" style="margin:6px 0 10px">
        <input id="cdco" placeholder="公司名（和 review 页写法一致）" style="width:230px">
        <select id="cddays"><option value="30">30 天</option><option value="60">60 天</option><option value="90">90 天</option><option value="180">180 天</option></select>
        <input id="cdwhy" placeholder="原因（可选）：Ashby 5/180、一次一份…" style="width:290px">
        <button type="submit">加入 cooldown</button>
      </form>
      <div id="cdlist" class="small muted">loading…</div>
      <div id="sum"></div><div id="out"></div>
      <script>
      function esc(s){const e=document.createElement('div');e.textContent=s||'';return e.innerHTML}
      async function cdList(){
        const r=await fetch('/api/companies/cooldowns'); if(!r.ok) return;
        const d=await r.json(); const el=document.getElementById('cdlist');
        if(!d.rows.length){el.innerHTML='<span class="muted">目前没有 cooldown 中的公司</span>';return}
        el.innerHTML=d.rows.map(c=>'<div style="padding:4px 0">⏳ <b>'+esc(c.company)+'</b> 到 '+esc(c.until)+
          (c.reason?' · '+esc(c.reason):'')+' <span class="muted">['+(c.source==='config'?'config.yaml':'手动')+']</span> '+
          (c.source==='manual'?'<a href="#" onclick="return cdClear(`'+encodeURIComponent(c.company)+'`)">解除</a>':'')+'</div>').join('');
      }
      async function cdPost(company,days,reason){
        const r=await fetch('/api/companies/cooldown',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({company,days,reason})});
        if(!r.ok){alert(await r.text());return null}
        return r.json();
      }
      async function cdAdd(){
        const co=document.getElementById('cdco').value.trim(); if(!co) return false;
        const ok=await cdPost(co,+document.getElementById('cddays').value,document.getElementById('cdwhy').value);
        if(ok){document.getElementById('cdco').value='';document.getElementById('cdwhy').value='';}
        await cdList(); return false;
      }
      async function cdClear(co){
        const r=await fetch('/api/companies/cooldown/'+co,{method:'DELETE'});
        if(!r.ok) alert(await r.text()); await cdList(); return false;
      }
      async function cdPrompt(co){
        const name=decodeURIComponent(co);
        const d=prompt('「'+name+'」不让投了？冷却多少天？（30 / 60 / 90 / 180）','30');
        if(!d) return false;
        const why=prompt('原因（可选）','')||'';
        const ok=await cdPost(name,parseInt(d,10),why);
        if(ok) alert(name+' 已加入 cooldown，到 '+ok.until+'。扫描从下一次起跳过这家；review 里它的 '+ok.hidden_roles+' 个岗位已隐藏。');
        await cdList(); return false;
      }
      cdList();
      function ovBtns(r){
        const co=encodeURIComponent(r.company),
              zh={closed:'手动结案',hidden:'隐藏',booked:'已排期',acked:'已处理'};
        const a=(act,label,tip)=>'<a href="#" title="'+tip+'" onclick="return ov(`'+co+'`,'+
          (act?'`'+act+'`':'null')+')">'+label+'</a>';
        const cd=' · <a href="#" title="这家不让投了：扫描跳过 + review 隐藏，到期自动恢复" onclick="return cdPrompt(`'+co+'`)">⏳ 不让投了</a>';
        if(r.override) return '已手动标记：'+zh[r.override]+' '+a(null,'撤销','清除人工标记')+cd;
        return a('acked','✓ 已处理','在邮件之外回过了（平台/电话/日历）——欠账清零，来新邮件会自动复活')+' · '+
               a('booked','📅 已排期','约面藏在正文/日历里，机器没读出来时点这个')+' · '+
               a('closed','✕ 结案','这条线结束了（含你拒了对方的情况）')+' · '+
               a('hidden','🙈 隐藏','误认的公司/不想再看')+cd;
      }
      async function ov(co,action){
        await fetch('/api/companies/override',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({company:decodeURIComponent(co),action})});
        await load(false); return false;
      }
      async function load(refresh){
        const o=document.getElementById('out');
        o.innerHTML='<p class="muted">'+(refresh?'reading Gmail…':'loading…')+'</p>';
        const r=await fetch('/api/companies?refresh='+(refresh?'true':'false'));
        if(!r.ok){o.innerHTML='<p class="tip warn">'+esc(await r.text())+'</p>';return}
        window._d=await r.json(); render();
      }
      function render(){
        const d=window._d; if(!d) return;
        const s=d.summary, showDead=document.getElementById('dead').checked;
        document.getElementById('sum').innerHTML=
          '<p class="small muted">'+d.account+' · <b>'+s.live+'</b> 个在跑（'+
          '<b>'+s.your_turn+'</b> 该你回 · '+s.waiting+' 等对方 · '+s.booked+' 已排期）· '+
          s.cold+' 个已冷 · '+s.closed+' 个已挂</p>';
        const rows=d.rows.filter(r=>showDead||(!r.dead&&!r.cold));
        const mark={2:'🔴',1:'🟡',0:'　'};
        document.getElementById('out').innerHTML=rows.map(r=>
          '<div style="border-bottom:1px solid #eef2f6;padding:10px 0;'+
          (r.dead||r.cold?'opacity:.45':'')+'">'+
          '<span style="font-size:15px">'+mark[r.urgency]+'</span> '+
          '<b style="font-size:15px">'+esc(r.company)+'</b> '+
          '<span class="pill '+(r.dead||r.cold?'lo':(r.rank>=3?'hi':'md'))+'">'+
            esc(r.stage_label)+'</span> '+
          '<span class="small muted">'+r.messages+' 封 · '+r.threads+' 个线程 · 最后 '+
            r.last_date+'（'+r.quiet_days+' 天前）</span>'+
          '<div class="small" style="margin-top:3px">'+esc(r.action)+'</div>'+
          '<div class="small muted">'+esc(r.last_subject)+'</div>'+
          '<div class="small muted">🏷 '+esc(r.label)+' · '+ovBtns(r)+'</div>'+
          '</div>').join('')+
          (d.review.length?'<h3 style="margin-top:22px">认不出公司的线程 ('+d.review.length+
            ')</h3><p class="small muted">没有打标签，也没进上面的表 —— 需要你看一眼</p>'+
            d.review.map(t=>'<div class="small" style="padding:4px 0">'+t.last_date+' · '+
              esc(t.why)+' · '+esc(t.subject)+'</div>').join(''):'');
      }
      load(false);
      </script>""")


@app.get("/api/health")
def api_health():
    rows = hbmod.read()
    return {"ok": not hbmod.red(rows), "tasks": rows}


def _gcal_auth_snapshot() -> dict:
    with _gcal_auth_lock:
        proc = _gcal_auth_proc
        started = _gcal_auth_started
    rc = proc.poll() if proc else None
    try:
        log_text = _gcal_auth_log.read_text(encoding="utf-8", errors="replace")[-6000:]
    except OSError:
        log_text = ""
    m = re.search(r"https://accounts\.google\.com/o/oauth2/auth\?\S+", log_text)
    return {
        "running": bool(proc and rc is None),
        "returncode": rc,
        "started_epoch": started,
        "started_ago_s": round(time.time() - started) if started else None,
        "auth_url": m.group(0) if m else "",
        "log_tail": log_text,
    }


@app.post("/api/bqautoprep/gcal-auth")
def api_bqautoprep_gcal_auth():
    """Start the local Google Calendar readonly OAuth flow for BQ AutoPrep.

    This is deliberately a local-only helper: it just runs the same script the README tells the
    user to run by hand, then the user grants Google Calendar readonly access in the browser.
    """
    global _gcal_auth_proc, _gcal_auth_started
    already_running = False
    with _gcal_auth_lock:
        if _gcal_auth_proc and _gcal_auth_proc.poll() is None:
            already_running = True
        if not already_running:
            _gcal_auth_log.parent.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["PATH"] = env.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
            with open(_gcal_auth_log, "ab") as lf:
                lf.write(("\n===== UI gcal auth %s =====\n" %
                          time.strftime("%Y-%m-%d %H:%M:%S")).encode())
                lf.flush()
                _gcal_auth_proc = subprocess.Popen(
                    [str(cfgmod.ROOT / ".venv/bin/python"), "tools/gcal_auth.py"],
                    cwd=str(cfgmod.ROOT),
                    env=env,
                    stdout=lf,
                    stderr=lf,
                    start_new_session=True,
                )
                _gcal_auth_started = time.time()
    return _gcal_auth_snapshot()


@app.get("/api/bqautoprep/gcal-auth")
def api_bqautoprep_gcal_auth_status():
    return _gcal_auth_snapshot()


@app.get("/health", response_class=HTMLResponse)
def health_page():
    """体检表：每个定时任务经 tools/hb_run.sh 打卡，这里读卡画灯（app/heartbeats.py）。"""
    rows = hbmod.read()
    icon = {"ok": "🟢", "fail": "🔴", "stale": "🔴", "never": "⚪"}
    order = {"fail": 0, "stale": 1, "ok": 2, "never": 3}
    rows.sort(key=lambda r: order[r["status"]])

    def action(r: dict) -> str:
        if r["name"] != "bqautoprep" or r["status"] not in ("fail", "stale"):
            return ""
        return ("<button onclick='fixBQGcal(this)'>重新授权日历</button>"
                "<div id='bq-gcal-auth' class='small muted'></div>")

    trs = "".join(
        f"<tr><td>{icon[r['status']]}</td><td><b>{r['zh']}</b> <span class='d'>{r['name']}</span></td>"
        f"<td>{r['cadence']}</td><td>{r['finished'] or '—'}</td>"
        f"<td>{('%ds' % r['duration_s']) if r['duration_s'] is not None else '—'}</td>"
        f"<td>{r['detail']}</td><td>{action(r)}</td></tr>"
        for r in rows)
    n_red = len(hbmod.red(rows))
    verdict = (f'<p class="tip" style="background:#fef2f2;border-color:#fecaca;color:#991b1b">'
               f'🔴 {n_red} 个任务出问题 — 看红灯行。</p>' if n_red
               else '<p class="tip">🟢 所有定时任务都在按时打卡。</p>')
    return _page("Health", f"""<h1>🩺 定时任务体检表</h1>
      {verdict}
      <p class="tip">原理：每个任务干完活会打一张卡。太久没打卡 = 失联；打了卡但退出码非 0 = 上次失败；
      ⚪ = 新任务还没到运行时间。看板网页本身不用打卡 — 你能打开这页就说明它活着。
      值班员每天 5 次巡查，发现<b>新出现的</b>红灯会弹 Mac 通知。</p>
      <table><tr><th></th><th>任务</th><th>节奏</th><th>上次打卡</th><th>用时</th><th>状态</th><th>操作</th></tr>{trs}</table>
      <script>
      async function fixBQGcal(btn){{
        const box=document.getElementById('bq-gcal-auth');
        btn.disabled=true; btn.textContent='正在打开授权…';
        box.textContent='如果浏览器弹出 Google 页面，选 sample-5fbfd918@example.com，然后点 Allow。';
        try {{
          await fetch('/api/bqautoprep/gcal-auth', {{method:'POST'}});
          pollBQGcal(btn, box);
        }} catch(e) {{
          btn.disabled=false; btn.textContent='重新授权日历';
          box.textContent='启动失败：'+e;
        }}
      }}
      async function pollBQGcal(btn, box){{
        const r=await fetch('/api/bqautoprep/gcal-auth');
        const s=await r.json();
        if(s.running){{
          box.textContent='等待你在浏览器里完成授权…';
          if(s.auth_url) box.innerHTML+=' 没弹窗就打开：<a href="'+s.auth_url+'" target="_blank">授权链接</a>';
          setTimeout(()=>pollBQGcal(btn, box), 2000);
          return;
        }}
        if(s.returncode===0){{
          btn.textContent='授权完成'; box.textContent='已连接日历。刷新页面后红灯会在下次打卡更新。';
          return;
        }}
        btn.disabled=false; btn.textContent='重新授权日历';
        box.textContent='授权没有完成。再点一次按钮重试；如果仍失败，看日志 jobscan/logs/gcal_auth_ui.log。';
      }}
      </script>""")


# ─────────────────────────────────────────────────────────────────────────────
# 工作台设置 + Google 授权（owner 2026-09-23）。一个候选人一个 token（app/google_auth.py），
# 走他浏览器里的 Google 同意页 → 回调到本进程 /oauth/google/callback。为什么不像上面的
# gcal-auth 那样起子进程：同意必须在候选人点按钮的那个 Chrome profile 里完成（他的 Google
# 账号登在那里），子进程只会打开这台机器的默认浏览器 —— 那是 owner 的。
# ─────────────────────────────────────────────────────────────────────────────


def _google_redirect_uri() -> str:
    """Loopback redirect for the 'Desktop app' OAuth client: any http://127.0.0.1:<port>/… is
    accepted without registration, and the port is this workspace's own web port."""
    port = int((cfg.get("ui") or {}).get("port", 8787))
    return f"http://127.0.0.1:{port}/oauth/google/callback"


@app.get("/api/google/auth/status")
def api_google_auth_status():
    return gauthmod.status(cfg)


@app.post("/api/google/auth/start")
def api_google_auth_start():
    client = gauthmod.client_secret_path(cfg)
    if not client.exists():
        raise HTTPException(503, f"缺 OAuth client 文件：{client} —— 去 Google Cloud Console → APIs & Services → "
                                 f"Credentials 建一个「Desktop app」类型的 OAuth client，下载 JSON 放到这个路径"
                                 f"（config.yaml 的 google.client_secret 可改）。")
    try:
        return {"url": gauthmod.start_flow(cfg, _google_redirect_uri())}
    except Exception as e:  # noqa: BLE001 — surface the reason on the page (the message never carries the secret)
        raise HTTPException(500, f"启动授权失败：{type(e).__name__}: {e}")


@app.post("/api/google/auth/revoke")
def api_google_auth_revoke():
    return gauthmod.revoke(cfg)


_RED_TIP = 'class="tip" style="background:#fef2f2;border-color:#fecaca;color:#991b1b"'


@app.get("/oauth/google/callback", response_class=HTMLResponse)
def oauth_google_callback(state: str = "", code: str = "", error: str = ""):
    """Google sends the browser back here after consent. Exchanges the code, saves the
    workspace token, and says WHICH account got authorized (the Chrome profile may have been
    logged into the wrong one)."""
    from html import escape as esc
    back = '<p><a class="b" href="/settings">← 回到工作台设置</a></p>'
    if error or not code:
        return _page("Google 授权", f'<h1>❌ 授权没有完成</h1><p class="tip">Google 返回：'
                     f'<code>{esc(error or "没有 code 参数")}</code>。回设置页再点一次「授权 Google」。</p>{back}')
    try:
        st = gauthmod.finish_flow(cfg, state, code)
    except Exception as e:  # noqa: BLE001 — shown to the person; never includes the token
        return _page("Google 授权", f'<h1>❌ 授权失败</h1><p {_RED_TIP}>{esc(type(e).__name__)}: {esc(str(e))}</p>'
                     f'<p class="tip">常见原因：链接过期（超过 10 分钟）、这个 Google 账号还没加进 Cloud 项目的 '
                     f'Test users、或者同意页上把 Gmail 那一项取消了。</p>{back}')
    acct = esc(st.get("account") or "（没拿到账号邮箱）")
    body = f"<h1>✅ 已授权 {acct}</h1>"
    if st.get("account_matches") is False:
        body += (f'<p {_RED_TIP}><b>⚠️ 账号不对。</b>这个工作台配置的邮箱是 <b>{esc(st.get("expected_account") or "")}</b>，'
                 f'但刚授权的是 <b>{acct}</b> —— 多半是这个 Chrome profile 里登着别人的 Google 账号。'
                 f'先在 Chrome 右上角切到正确的账号，再 <a href="/settings">回设置页重新授权</a>（会覆盖这次的 token）。</p>')
    elif st.get("account_matches") is None:
        body += '<p class="tip">没能核对账号邮箱（userinfo 没返回）。回设置页看状态里显示的账号是不是他的。</p>'
    granted = [n for ok, n in ((st.get("mail_scope"), "Gmail（IMAP/SMTP）"),
                               (st.get("calendar_scope"), "Calendar 只读")) if ok]
    body += f'<p>拿到的权限：{esc("、".join(granted) or "—")}。token 存在 <code>{esc(st["token_path"])}</code>。</p>'
    if not st.get("mail_scope"):
        body += (f'<p {_RED_TIP}>没拿到 Gmail 权限 —— 同意页上 Gmail 那一项被取消了，邮箱功能不会亮。'
                 f'回设置页重新授权时把它勾上。</p>')
    body += back + '<p class="tip">这个标签页可以关了；设置页会自己刷新状态。</p>'
    return _page("Google 授权", body)


_SETTINGS_JS = """
<script>
async function gauthStart(btn){
  const box=document.getElementById('gauth-box'), fb=document.getElementById('gauth-fallback');
  const label=btn.textContent; btn.disabled=true; btn.textContent='正在拿授权链接…';
  // Open the tab synchronously (inside the click) so the popup blocker lets it through, then
  // point it at Google once the URL arrives. Same browser profile = the candidate's Google login,
  // which is the whole point — a copy-pasted URL in another window would consent as someone else.
  const w=window.open('', '_blank');
  try{
    const r=await fetch('/api/google/auth/start',{method:'POST'});
    const j=await r.json();
    if(!r.ok){ if(w) w.close(); box.textContent='启动失败：'+(j.detail||r.status); btn.disabled=false; btn.textContent=label; return; }
    if(w){ w.location=j.url; }
    fb.innerHTML='没弹出新标签？<a href="'+j.url+'" target="_blank">点这里打开 Google 授权页</a>'
      +'<div class="small muted" style="word-break:break-all;margin-top:4px">'+j.url+'</div>';
    box.textContent='在新标签页里选他的 Google 账号 → 同意。完成后这页会自己刷新。';
    gauthPoll();
  }catch(e){ if(w) w.close(); box.textContent='启动失败：'+e; btn.disabled=false; btn.textContent=label; }
}
let _gauthWas=null;
async function gauthPoll(){
  try{
    const r=await fetch('/api/google/auth/status'); const s=await r.json();
    const sig=JSON.stringify([s.authorized,s.account,s.scopes]);
    if(_gauthWas===null){ _gauthWas=sig; }
    else if(sig!==_gauthWas){ location.reload(); return; }
  }catch(e){}
  setTimeout(gauthPoll, 3000);
}
async function gauthRevoke(btn){
  if(!confirm('撤销这个工作台的 Google 授权？邮箱和日历功能会立刻变灰，要用得重新授权。')) return;
  btn.disabled=true;
  try{ await fetch('/api/google/auth/revoke',{method:'POST'}); }catch(e){}
  location.reload();
}
</script>
"""


class PaceIn(BaseModel):
    mode: str
    until: str | None = None             # ISO date; overrides the default auto-revert date
    auto_revert: bool = True             # False = keep a low level until she changes it
    companies_per_day: int | str | None = None   # custom only
    tailor_per_day: int | str | None = None      # custom only; defaults to companies_per_day


@app.get("/api/pace")
def api_pace_get(conn=Depends(view_conn)):
    """Current 投递节奏 level + today's progress (frozen / handled / résumés queued)."""
    eff = pacemod.effective(conn, cfg)
    today = store.la_today()
    return {**eff, "today": today, "frozen_today": pacemod.frozen_count(conn, cfg, today),
            "tailored_today": pacemod.tailored_today(conn, today)}


@app.post("/api/pace")
def api_pace_set(body: PaceIn, conn=Depends(view_conn)):
    """Switch 投递节奏. Takes effect on the next page load (the day's frozen list tops up when
    the level goes up; a lower level shows only the first N of it)."""
    try:
        return pacemod.set_mode(conn, cfg, body.mode, until=body.until or None,
                                auto_revert=body.auto_revert,
                                companies_per_day=body.companies_per_day,
                                tailor_per_day=body.tailor_per_day)
    except ValueError as e:
        raise HTTPException(422, str(e))


def _pace_section(conn) -> str:
    """设置页「投递节奏」一块：五个档位按钮 + 自定义两个数 + 自动恢复日期。"""
    from html import escape as esc
    eff = pacemod.effective(conn, cfg)
    today = store.la_today()
    tomorrow = (datetime.date.fromisoformat(today) + datetime.timedelta(days=1)).isoformat()
    frozen = pacemod.frozen_count(conn, cfg, today)
    tailored = pacemod.tailored_today(conn, today)

    def lim(v, unit):
        return "不限" if v is None else f"{v} {unit}"
    if eff["reverted_from"]:
        state = (f"已自动回到 <b>{esc(eff['label'])}</b>（之前设的「{esc(eff['reverted_label'])}」到期了）")
    elif eff["until"]:
        state = f"<b>{esc(eff['label'])}</b>，到 <b>{esc(eff['until'])}</b> 自动回「{esc(eff['revert_label'])}」"
    else:
        state = f"<b>{esc(eff['label'])}</b>"
    progress = (f"今天已冻结 {frozen} 家 · 已排 {tailored} 份简历"
                if eff["limited"] else "冲刺档不冻结名单、不限简历数（每轮最多 batch_cap 份）")
    btns = []
    for lv in eff["levels"]:
        on = " on" if lv["mode"] == eff["mode"] else ""
        desc = (f"每天 {lim(lv['companies_per_day'], '家')} · 简历 {lim(lv['tailor_per_day'], '份')}"
                if lv["mode"] != "custom" else "自己填两个数")
        btns.append(f"<button class='pace-btn{on}' data-mode='{lv['mode']}' onclick='paceSet(this)'>"
                    f"<b>{esc(lv['label'])}</b><br><span class='small muted'>{desc}</span></button>")
    custom = next(lv for lv in eff["levels"] if lv["mode"] == "custom")
    return f"""
      <h2 id="pace">投递节奏</h2>
      <p class="tip">控制 review 页<b>每天推几家公司</b>、autotailor <b>每天做几份新简历</b>。扫描照常一天四轮，岗位一个不漏；
      少推的只是排到明天。每天第一次打开 review 页时，按「匹配分 + 新鲜度」取最前面的 N 家、每家一个岗位冻结成当天名单；
      处理一家少一家，不会补新的。低于「{esc(eff['revert_label'])}」的档默认 {eff['revert_days']} 天后自动回「{esc(eff['revert_label'])}」。</p>
      <table>
        <tr><th style='width:160px'>现在</th><td>{state}</td></tr>
        <tr><th>今天</th><td>{esc(progress)}</td></tr>
      </table>
      <div class="pace-row">{''.join(btns)}</div>
      <div class="pace-opts small">
        自定义：每天推 <input id="pace-cpd" type="number" min="0" max="{pacemod.CUSTOM_MAX}" value="{custom['companies_per_day']}" style="width:64px"> 家，
        做 <input id="pace-tpd" type="number" min="0" max="{pacemod.CUSTOM_MAX}" value="{custom['tailor_per_day']}" style="width:64px"> 份新简历
        <span class="muted">（选「自定义」时生效）</span><br>
        自动恢复：<input id="pace-until" type="date" min="{tomorrow}"> <span class="muted">（留空 = {eff['revert_days']} 天后）</span>
        <label style="margin-left:10px"><input id="pace-norevert" type="checkbox"> 不自动恢复</label>
      </div>
      <div id="pace-msg" class="small"></div>
      <style>
        .pace-row {{ display:flex; gap:8px; flex-wrap:wrap; margin:10px 0 6px; }}
        .pace-btn {{ min-width:118px; padding:8px 10px; text-align:left; border-radius:6px; line-height:1.35; }}
        .pace-btn.on {{ background:#1a1d21; color:#fff; border-color:#1a1d21; }}
        .pace-btn.on .muted {{ color:#cbd5e1; }}
        .pace-opts {{ margin:4px 0 8px; line-height:2; }}
      </style>
      <script>
      async function paceSet(btn) {{
        const mode = btn.dataset.mode;
        const body = {{ mode, until: document.getElementById('pace-until').value || null,
                        auto_revert: !document.getElementById('pace-norevert').checked }};
        if (mode === 'custom') {{
          body.companies_per_day = document.getElementById('pace-cpd').value;
          body.tailor_per_day = document.getElementById('pace-tpd').value;
        }}
        const msg = document.getElementById('pace-msg');
        msg.textContent = '保存中…';
        const r = await fetch('/api/pace', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
                                             body: JSON.stringify(body) }});
        if (!r.ok) {{ msg.textContent = '没保存上：' + (await r.text()); return; }}
        location.reload();
      }}
      </script>"""


@app.get("/settings", response_class=HTMLResponse)
def settings_page(conn=Depends(view_conn)):
    """工作台设置：投递节奏、这台子是谁的、邮箱/日历接没接上、一键授权 Google。"""
    from html import escape as esc
    st = gauthmod.status(cfg)
    prof = cfg.get("profile") or {}
    mc = cfg.get("mailcode") or {}
    ws = cfgmod.workspace_name(cfg) or "default"
    _mc, _addr, secret = mailmod._cfg(cfg)
    cred_kind = ("Google OAuth token" if isinstance(secret, mailmod.OAuthSecret)
                 else ("secrets.yaml app password" if secret else "无"))
    mail_on = mailmod.available(cfg)
    cal_on = gcalmod.available(cfg)

    def row(k, v):
        return f"<tr><th style='width:160px'>{esc(k)}</th><td>{v}</td></tr>"

    prof_rows = "".join(row(k, esc(str(prof.get(f) or "—"))) for k, f in (
        ("姓", "last_name"), ("名", "first_name"), ("邮箱", "email"), ("电话", "phone"), ("所在地", "location")))

    if st["authorized"]:
        acct = esc(st["account"] or "（账号未知）")
        g_line = f"🟢 已授权 <b>{acct}</b>"
        if st.get("account_blocked"):
            # finish_flow refuses such a token, so this is a hand-copied / pre-check file. It is
            # ignored (mail_scope/calendar_scope False) — say why instead of "没有 Gmail 权限".
            g_line += (f' <span class="pill" style="background:#fee2e2;color:#991b1b">'
                       f'{"账号不匹配" if st["account_matches"] is False else "账号未知"}</span>'
                       f'<div {_RED_TIP}>工作台期待的是 <b>{esc(st["expected_account"] or "")}</b>，这个 token 不会被使用'
                       f'（邮箱/日历都不亮）。撤销后在 Chrome 右上角切到正确的 Google 账号重新授权。</div>')
        granted = [n for ok, n in ((st["mail_scope"], "Gmail（IMAP/SMTP）"),
                                   (st["calendar_scope"], "Calendar 只读")) if ok]
        g_rows = (row("Google 账号", g_line)
                  + row("拿到的权限", esc("、".join(granted) or "—")
                        + ("" if st["mail_scope"] or st.get("account_blocked")
                           else f'<div {_RED_TIP}>没有 Gmail 权限：重新授权时把 Gmail 勾上。</div>'))
                  + row("access token 到期", esc(st["expiry"] or "—") + ' <span class="small muted">（自动刷新，不用管）</span>'))
        auth_btn = "重新授权 Google"
        revoke_btn = "<button onclick='gauthRevoke(this)' style='margin-left:8px'>撤销授权</button>"
    else:
        g_rows = row("Google 账号", "⚪ 未授权")
        auth_btn = "授权 Google（Gmail + Calendar）"
        revoke_btn = ""
    client_line = (f"✅ <code>{esc(str(gauthmod.client_secret_path(cfg)))}</code>" if st["client_present"]
                   else f'<span {_RED_TIP}>❌ 缺 <code>{esc(str(gauthmod.client_secret_path(cfg)))}</code> —— '
                        f'Google Cloud Console → Credentials → 建「Desktop app」OAuth client → 下载 JSON 放到这里。</span>')
    g_rows += row("OAuth client", client_line) + row("token 文件", f"<code>{esc(st['token_path'])}</code>")

    mail_line = (f"{'🟢 可用' if mail_on else '⚪ 不可用'} · mailcode.enabled={'true' if mc.get('enabled') else 'false'} · "
                 f"邮箱 {esc(_addr or '—')} · 凭据：{esc(cred_kind)}")
    if st["authorized"] and st["calendar_scope"]:
        cal_line = "🟢 已连接（这个工作台自己的 token）"
    elif cal_on:
        cal_line = "🟡 已连接，但沿用的是共享的旧 token（tools/gcal_auth.py，owner 的日历）—— 授权 Google 后会换成这个工作台自己的"
    else:
        cal_line = "⚪ 未连接"

    return _page("Settings", f"""<h1>⚙ 工作台设置 <span class="pill lo">{esc(ws)}</span></h1>
      {_pace_section(conn)}
      <h2>这是谁的台子</h2>
      <table>{prof_rows}</table>
      <h2>Google 授权（Gmail + Calendar）</h2>
      <p class="tip">一个 token 同时打通邮箱（IMAP/SMTP，不用 app password）和日历只读。token 只存在本机这个工作台的目录里。</p>
      <table>{g_rows}</table>
      <p><button onclick='gauthStart(this)'>{auth_btn}</button>{revoke_btn}</p>
      <div id="gauth-box" class="small muted"></div>
      <div id="gauth-fallback" class="small"></div>
      <h2>接通状态</h2>
      <table>{row("邮箱", mail_line)}{row("日历", cal_line)}</table>
      <h2>怎么做</h2>
      <ol>
        <li>在<b>这个候选人的 Chrome profile</b> 里打开这一页（右上角头像是他的 Google 账号）。</li>
        <li>点上面的「{auth_btn}」。</li>
        <li>新标签页里选<b>他的</b> Google 账号。</li>
        <li>同意页上把 Gmail 和 Calendar 两项都勾上，点 Continue / Allow。</li>
        <li>回到这一页 —— 它会自己刷新；没刷就手动刷新，看「Google 账号」一行是不是他的邮箱。</li>
      </ol>
      <p class="tip"><b>前提（一次性）：</b>这个 Google Cloud 项目的 OAuth consent screen 多半还是 <b>Testing</b> 模式，
      所以候选人的 Google 账号必须先加进 <b>Test users</b>（Google Cloud Console → APIs &amp; Services →
      OAuth consent screen → Test users），否则同意页会直接拒绝。受限范围 <code>https://mail.google.com/</code>
      对 Test users 不需要走应用审核。</p>
      {_SETTINGS_JS}""")


# ─────────────────────────────────────────────────────────────────────────────
# 念稿练习 (recite) — 2026-08-29。**只加不改**：新表建在 user_state.db，既有路由和表结构
# 一个字没动。设计定稿见 scratchpad/product/spec-v3.md（v2 的 diff）+ 四份 critique-v3。
#
# 落点选 jobscan 而不是新起一个 app：launchd 常驻、8787 已在 LISTEN，日历里的 🎤 块放个
# 链接点开即用。冷启动摩擦才是真正的流失风险，这条已经满足了。
# ─────────────────────────────────────────────────────────────────────────────

_ITEM_ID_RX = re.compile(r"^[a-z0-9:_-]{1,80}$")


def _recite_conn(conn):
    recitemod.ensure_schema(conn)
    return conn


def _recite_item(conn, item_id: str) -> dict:
    if not _ITEM_ID_RX.match(item_id or ""):
        raise HTTPException(400, "bad item id")
    row = conn.execute(
        "SELECT i.*, s.box, s.due_on, s.n_takes, s.n_pass, s.streak, s.last_total, s.last_fix,"
        "       s.exposed, s.exposure_stage, s.exposure_at, s.archived, s.needs_rewrite"
        " FROM recite_items i LEFT JOIN recite_state s ON s.item_id=i.item_id"
        " WHERE i.item_id=?", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such recite item")
    d = dict(row)
    d["points"] = recitemod.parse_key_points(d.get("key_points") or "[]")
    d["scored"], d["extra"] = recitemod.scored_cues(d["points"])
    for k in ("variants", "delivery_notes", "extra_cues"):
        try:
            d[k] = json.loads(d.get(k) or "[]")
        except Exception:
            d[k] = []
    d["canon_spans"] = recitemod.canon_spans(d.get("script") or "")
    try:
        d["dialog"] = json.loads(d.get("dialog") or "null")
    except Exception:
        d["dialog"] = None
    d["dialog_followups"] = _recite_merge_followups(d.get("followup_q") or "", d["dialog"])
    return d


def _recite_company(raw: str) -> str:
    """把 company 列上那些带括号/斜杠的长串压成一个可以当分组标题的短名。

    库里真实存在 `TikTok / ByteDance (ads monetization loop; interviewer ex-Meta 5+y…)`
    这样的值，三个变体其实是同一家公司 —— 不归一就会在侧栏里排出三个 TikTok。
    """
    s = (raw or "").strip()
    for sep in ("(", "（", "/", ";", "，", ","):
        if sep in s:
            s = s.split(sep)[0]
    s = s.strip(" -—·")
    return s or "其他"


def _recite_has_audio(slot: str, item_id: str) -> bool:
    try:
        return (recitemod.data_dir() / slot / f"{item_id}.mp3").exists()
    except Exception:
        return False


def _recite_dialog_followups(dialog) -> list[dict]:
    turns = (dialog or {}).get("turns") or []
    out: list[dict] = []
    cur: dict | None = None
    seen_answer = False
    for t in turns:
        if not isinstance(t, dict):
            continue
        sp = t.get("sp")
        text = (t.get("x") or "").strip()
        if not text:
            continue
        if sp == "me":
            seen_answer = True
            cur = None
            continue
        if sp != "iv" or not seen_answer:
            continue
        if cur is None:
            cur = {"x": text, "s": t.get("s", 0)}
            out.append(cur)
        else:
            cur["x"] = f"{cur['x']} {text}"
    return out


def _recite_merge_followups(explicit: str, dialog) -> list[dict]:
    out = []
    explicit = (explicit or "").strip()
    if explicit:
        out.append({"x": explicit, "s": 0, "source": "recap"})
    for f in _recite_dialog_followups(dialog):
        fx = (f.get("x") or "").strip()
        if fx and not any(fx in (x["x"] or "") or (x["x"] or "") in fx for x in out):
            out.append(f)
    return out


def _recite_library(conn) -> list[dict]:
    """全部条目（含 script 为空的）按 公司 → 场次 分组。库优先：这一页就是总库。"""
    rows = [dict(r) for r in conn.execute(
        "SELECT i.item_id, i.company, i.interview, i.kind, i.question, i.question_zh,"
        "       i.target_sec, i.script, i.quality, i.deferred, i.link_group,"
        "       i.followup_q, i.dialog,"
        "       s.box, s.last_total, s.archived,"
        # n_takes 只在评分发生时才涨（apply_outcome），而 v2 里评分是可选的 —— 直接数
        # recite_takes，否则她录了五遍库里还写「练过 0 次」。
        "       (SELECT COUNT(*) FROM recite_takes t WHERE t.item_id=i.item_id) n_takes"
        "  FROM recite_items i LEFT JOIN recite_state s ON s.item_id=i.item_id"
        " ORDER BY i.company, i.interview, i.item_id")]
    for d in rows:
        d["co"] = _recite_company(d.get("company"))
        d["has_script"] = bool((d.get("script") or "").strip())
        d["has_orig"] = _recite_has_audio("orig", d["item_id"])
        d["has_ref"] = _recite_has_audio("ref", d["item_id"])
        d["title"] = (d.get("question") or d.get("question_zh") or d["item_id"]).strip()
        d["subtitle"] = (d.get("question_zh") or "").strip()
        try:
            dlg = json.loads(d.get("dialog") or "null")
        except Exception:
            dlg = None
        d["followup_count"] = len(_recite_merge_followups(d.get("followup_q") or "", dlg))
        d.pop("script", None)
        d.pop("dialog", None)
    return rows


def _recite_groups(rows: list[dict]) -> list[dict]:
    by_co: dict[str, dict] = {}
    for d in rows:
        co = by_co.setdefault(d["co"], {"name": d["co"], "n": 0, "sessions": {}})
        co["n"] += 1
        s = co["sessions"].setdefault(d.get("interview") or "（未分场）",
                                      {"name": d.get("interview") or "（未分场）", "rows": []})
        # 键名叫 rows 不叫 items：Jinja 里 `s.items` 会先命中 dict.items 方法，静默返回一个
        # builtin 而不是列表 —— 这个坑只在渲染时炸，不在 Python 里。
        s["rows"].append(d)
    out = []
    for co in by_co.values():
        co["sessions"] = sorted(co["sessions"].values(), key=lambda s: s["name"])
        out.append(co)
    out.sort(key=lambda c: (-c["n"], c["name"]))
    return out


@app.get("/recite", response_class=HTMLResponse)
def recite_home(request: Request, conn=Depends(view_conn)):
    _recite_conn(conn)
    c = recitemod.cfg_recite(cfg)
    queue = recitemod.today_queue(conn, cfg)
    for q in queue:
        q["points"] = recitemod.parse_key_points(q.get("key_points") or "[]")
        q["scored"], _ = recitemod.scored_cues(q["points"])
    groups: dict[str, list[str]] = {}
    for r in conn.execute("SELECT item_id, link_group FROM recite_items"
                          " WHERE link_group IS NOT NULL AND link_group!=''"):
        groups.setdefault(r["link_group"], []).append(r["item_id"])
    dupes = {i for ids in groups.values() if len(ids) > 1 for i in ids}
    counts = {"all": conn.execute(
        "SELECT COUNT(*) n FROM recite_items").fetchone()["n"]}
    counts["rough"] = conn.execute(
        "SELECT COUNT(*) n FROM recite_items WHERE quality='rough' OR deferred=1").fetchone()["n"]
    counts["archived"] = conn.execute(
        "SELECT COUNT(*) n FROM recite_state WHERE archived=1").fetchone()["n"]
    counts["thin"] = conn.execute(
        "SELECT COUNT(*) n FROM recite_items WHERE scope='thin'").fetchone()["n"]
    lib = _recite_library(conn)
    counts["orig"] = sum(1 for d in lib if d["has_orig"])
    counts["ref"] = sum(1 for d in lib if d["has_ref"])
    counts["noscript"] = sum(1 for d in lib if not d["has_script"])
    return templates.TemplateResponse(request, "recite.html", {
        "queue": queue, "cfgr": c, "hist": recitemod.histogram(conn),
        "calibrating": recitemod.calibrating_now(conn, cfg),
        "dupes": dupes, "counts": counts,
        "groups": _recite_groups(lib),
        "kinds": sorted({d["kind"] for d in lib if d.get("kind")}),
        "companies": sorted({d["co"] for d in lib}),
        "queue_ids": {q["item_id"] for q in queue},
    })


@app.get("/recite/{item_id}", response_class=HTMLResponse)
def recite_item_page(item_id: str, request: Request, conn=Depends(view_conn)):
    _recite_conn(conn)
    it = _recite_item(conn, item_id)
    st = recitemod.get_state(conn, item_id)
    n_takes = int(st["n_takes"] or 0)
    variant = recitemod.variant_for(it, n_takes)
    # 曝光轮由 recite_state.exposed 显式驱动，**不从 n_takes 推导**（MEM2-P1-4b）：录音先写盘
    # 插行、再评分，ASR/LLM 挂掉时 n_takes 就变成了 1，曝光轮会永久消失，而她既没拿到基线
    # 也没拿到反馈。
    first_exposure = not int(st["exposed"] or 0)
    takes = [dict(r) for r in conn.execute(
        "SELECT * FROM recite_takes WHERE item_id=? ORDER BY id DESC LIMIT 5", (item_id,))]
    for t in takes:
        for k in ("metrics", "scores", "missed_cues", "fact_check", "red_flags"):
            try:
                t[k] = json.loads(t.get(k) or "null")
            except Exception:
                t[k] = None
    it["has_orig"] = _recite_has_audio("orig", item_id)
    it["has_dialog"] = _recite_has_audio("dialog", item_id)
    n_recorded = conn.execute("SELECT COUNT(*) n FROM recite_takes WHERE item_id=?",
                              (item_id,)).fetchone()["n"]
    # 同一场次的邻居 —— 练完一条能直接跳下一条，不用回库再找。
    sib = [dict(r) for r in conn.execute(
        "SELECT item_id, question, question_zh FROM recite_items"
        " WHERE interview=? ORDER BY item_id", (it.get("interview") or "",))]
    idx = next((i for i, r in enumerate(sib) if r["item_id"] == item_id), -1)
    return templates.TemplateResponse(request, "recite_item.html", {
        "it": it, "st": dict(st), "variant": variant, "first_exposure": first_exposure,
        "takes": takes, "cfgr": recitemod.cfg_recite(cfg),
        "calibrating": recitemod.calibrating_now(conn, cfg),
        "queue_ids": [q["item_id"] for q in recitemod.today_queue(conn, cfg)],
        "n_recorded": n_recorded,
        "prev_item": sib[idx - 1] if idx > 0 else None,
        "next_item": sib[idx + 1] if 0 <= idx < len(sib) - 1 else None,
        "sib_pos": (idx + 1, len(sib)) if idx >= 0 else (0, 0),
    })


@app.get("/api/recite/queue")
def api_recite_queue(conn=Depends(view_conn)):
    _recite_conn(conn)
    return {"queue": [{k: v for k, v in q.items() if k != "key_points"}
                      for q in recitemod.today_queue(conn, cfg)]}


@app.get("/recite/audio/{item_id}/{slot}")
def recite_audio(item_id: str, slot: str, conn=Depends(view_conn)):
    """slot ∈ ref | orig | take-<id>。

    路径必须 resolve 到 data/recite/ 之内，且 **不 mount StaticFiles 到 data/** —— 那个目录
    装的是她的录音、转写和评分，隐私红线的第一条就是它不能被整目录暴露出去。
    """
    if not _ITEM_ID_RX.match(item_id or ""):
        raise HTTPException(400, "bad item id")
    root = recitemod.data_dir().resolve()
    if slot == "ref":
        p = root / "ref" / f"{item_id}.mp3"
    elif slot == "orig":
        p = root / "orig" / f"{item_id}.mp3"
    elif slot == "dialog":
        p = root / "dialog" / f"{item_id}.mp3"
    elif slot.startswith("take-"):
        _recite_conn(conn)
        row = conn.execute("SELECT audio_path FROM recite_takes WHERE id=? AND item_id=?",
                           (slot[5:] if slot[5:].isdigit() else -1, item_id)).fetchone()
        if not row or not row["audio_path"]:
            raise HTTPException(404, "no take audio")
        p = root / "takes" / Path(row["audio_path"]).name
    else:
        raise HTTPException(400, "bad slot")
    p = p.resolve()
    if not str(p).startswith(str(root)) or not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(p))


@app.post("/api/recite/{item_id}/exposure")
def api_recite_exposure(item_id: str, body: dict, conn=Depends(view_conn)):
    """A0 曝光轮的进度落库 —— 可中断、可恢复、可跳过。

    恢复时若距 A0-② 超过 2 小时，回到 stage ① 重走（MEM3-3b）：那不是冷却，那是一个完整的
    保持区间，而这一 take 仍会被记成 first_exposure=1 的"基线"，同时污染基线和 r 序列。
    成本 75 秒，一生一次。
    """
    _recite_conn(conn)
    recitemod.get_state(conn, item_id)
    stage = max(0, min(4, int(body.get("stage") or 0)))
    conn.execute("UPDATE recite_state SET exposure_stage=?, exposure_at=? WHERE item_id=?",
                 (stage, store.la_now_iso(), item_id))
    conn.commit()
    return {"ok": True, "stage": stage}


@app.post("/api/recite/{item_id}/mark")
def api_recite_mark(item_id: str, body: dict, conn=Depends(view_conn)):
    """这稿要改 / 归档 / 手动升降箱 / 量级标 thin / 误报加白。"""
    _recite_conn(conn)
    recitemod.get_state(conn, item_id)
    act = str(body.get("action") or "")
    if act == "needs_rewrite":
        conn.execute("UPDATE recite_state SET needs_rewrite=1, snooze_until=? WHERE item_id=?",
                     (recitemod._plus(3), item_id))
        recitemod._append_rough(item_id, "她按了「这稿要改」")
    elif act == "archive":
        conn.execute("UPDATE recite_state SET archived=1 WHERE item_id=?", (item_id,))
    elif act == "unarchive":
        conn.execute("UPDATE recite_state SET archived=0 WHERE item_id=?", (item_id,))
    elif act == "scope_thin":
        conn.execute("UPDATE recite_items SET scope='thin' WHERE item_id=?", (item_id,))
    elif act == "box":
        conn.execute("UPDATE recite_state SET box=?, streak=0 WHERE item_id=?",
                     (max(1, min(5, int(body.get("box") or 1))), item_id))
    elif act == "snooze":
        conn.execute("UPDATE recite_state SET due_on=? WHERE item_id=?",
                     (recitemod._plus(int(body.get("days") or 1)), item_id))
    else:
        raise HTTPException(400, "unknown action")
    conn.commit()
    return {"ok": True}


def _score_take(conn, take_id: int, it: dict, *, transcript_hint: str = "") -> dict:
    """ASR → 指标 → 判官 → 硬门/软分 → 晋降箱。录音和 take 行此时已经在盘上了。"""
    row = dict(conn.execute("SELECT * FROM recite_takes WHERE id=?", (take_id,)).fetchone())
    path = recitemod.data_dir() / "takes" / Path(row["audio_path"]).name
    metrics = recitemod.audio_metrics(path)
    asr = ({"text": transcript_hint, "engine": "given", "error": ""} if transcript_hint
           else recite_asr.transcribe(path, metrics.get("duration") or 0))
    transcript = asr.get("text") or ""
    metrics.update(recitemod.filler_counts(transcript))
    metrics["wpm"] = recitemod.wpm(transcript, metrics)
    metrics["asr_engine"] = asr.get("engine") or ""
    metrics["asr_error"] = asr.get("error") or ""

    script = it.get("script") or ""
    r = recitemod.verbatim_ratio(transcript, script)
    prev = recitemod.prev_transcript(conn, it["item_id"], exclude_take_id=take_id)
    r_prev = recitemod.verbatim_ratio(transcript, prev) if prev else None

    cfg_r = recitemod.cfg_recite(cfg)
    calib = recitemod.calibrating_now(conn, cfg)
    vt = row["variant_type"] or "original"
    n_para = conn.execute(
        "SELECT COUNT(*) n FROM recite_takes WHERE variant_type='paraphrase'").fetchone()["n"]

    review = aimod.review_take(cfg, {
        # 判官看到的必须是**这一次实际问她的**那个问题：变体档下答原问是 structure 失败，
        # 不是 coverage 成功，而这个区分只有在 prompt 里问对了问题时才成立。
        "question": row["variant"] or it.get("question") or "",
        "scored_cues": [c["text"] for c in it["scored"]],
        "delivery_notes": (it.get("delivery_notes") or [])[:1],
        "script": script, "transcript": transcript, "metrics": metrics,
        "target_sec": it.get("target_sec"), "wpm": metrics["wpm"],
        "variant_type": vt, "variant_sec": row["variant_sec"],
        "variant_hint": row["gate_note"] or "",
        "why_line": row["why_line"], "why_q": it.get("why_q"),
        "speaker_name": it.get("speaker_name"), "pace": it.get("pace"),
    })

    checks = recitemod.fact_check(transcript, it)
    res = recitemod.judge_take(
        cfg_r=cfg_r, item=it, points=it["points"], scored=it["scored"],
        transcript=transcript, metrics=metrics, review=review,
        why_line=row["why_line"] or "", variant_type=vt,
        variant_sec=int(row["variant_sec"] or it.get("target_sec") or 60),
        mode=row["mode"] or "recall", first_exposure=bool(row["first_exposure"]),
        box=int((recitemod.get_state(conn, it["item_id"])["box"]) or 1),
        calibrating=calib, r=r, n_paraphrase_takes=n_para,
        number_flags=recitemod.number_flag_strings(checks))

    conn.execute(
        "UPDATE recite_takes SET transcript=?, metrics=?, verbatim_ratio=?, r_prev=?,"
        " scores=?, red_flags=?, missed_cues=?, fact_check=?, fix_next=?, one_line=?,"
        " verdict=?, judge_raw=?, passed=?, dur_sec=?, coverage=?, total=?, hard_ok=?,"
        " gate_note=? WHERE id=?",
        (transcript, json.dumps(metrics, ensure_ascii=False), r, r_prev,
         json.dumps(review["scores"], ensure_ascii=False),
         json.dumps(res["red_flags"], ensure_ascii=False),
         json.dumps(res["missed_cues"], ensure_ascii=False),
         json.dumps(checks, ensure_ascii=False), review["fix_next"], review["one_line"],
         res["verdict"], json.dumps({"raw": review["raw"], "fixed": review["fixed"],
                                     "error": review["error"], "issues": review["issues"],
                                     "evidence": review["evidence"]}, ensure_ascii=False),
         res["passed"], metrics.get("duration"), res["coverage"], res["total"],
         res["hard_ok"], res["gate_note"], take_id))
    conn.commit()

    same_day = bool(conn.execute(
        "SELECT COUNT(*) n FROM recite_takes WHERE item_id=? AND id<>? AND substr(ts,1,10)=?",
        (it["item_id"], take_id, store.la_today())).fetchone()["n"])
    st = recitemod.apply_outcome(
        conn, cfg_r, it["item_id"], passed=bool(res["passed"]),
        first_exposure=bool(row["first_exposure"]), variant_type=vt, same_day=same_day,
        hard_red=bool(res["red_flags"]), demotes_on_fail=res["demotes_on_fail"],
        total=float(res["total"]), fix_next=review["fix_next"])
    if row["first_exposure"]:
        conn.execute("UPDATE recite_state SET exposed=1, exposure_stage=4 WHERE item_id=?",
                     (it["item_id"],))
        conn.commit()
    _recite_log({"item_id": it["item_id"], "take_id": take_id, "ts": store.la_now_iso(),
                 "variant": vt, "passed": res["passed"], "total": res["total"],
                 "coverage": res["coverage"], "r": r, "r_prev": r_prev,
                 "metrics": metrics, "gate_note": res["gate_note"],
                 "judge_fixed": review["fixed"], "judge_error": review["error"]})
    return {"take_id": take_id, "transcript": transcript, "metrics": metrics,
            "verbatim_ratio": r, "r_prev": r_prev, "fact_check": checks,
            "scores": review["scores"], "issues": review["issues"],
            "evidence": review["evidence"], "fix_next": review["fix_next"],
            "one_line": review["one_line"], "judge_error": review["error"],
            "state": st, "calibrating": calib, **res}


def _recite_log(rec: dict) -> None:
    """阈值全量落盘 —— 直方图和第一周的 pass_total 校准都靠它，且它不含音频。"""
    try:
        p = cfgmod.resolve_path("data/logs/recite_takes.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


@app.post("/api/recite/{item_id}/take")
async def api_recite_take(item_id: str, request: Request, conn=Depends(view_conn)):
    """上传录音 → **先写盘 + 插行（scores=NULL）** → ASR → 指标 → LLM → 回填。

    先写盘不是洁癖：ASR 或 Anthropic 任一失败，她那 90 秒不会丢。丢一次录音的挫败感远大于
    少一次评分，而卡片可以老老实实说「评分没跑出来，录音已存 [重试评分]」。
    """
    _recite_conn(conn)
    it = _recite_item(conn, item_id)
    form = await request.form()
    up = form.get("audio")
    if up is None or not hasattr(up, "read"):
        raise HTTPException(400, "no audio")
    blob = await up.read()
    if not blob:
        raise HTTPException(400, "empty audio")
    ext = ".mp4" if "mp4" in (getattr(up, "content_type", "") or "") else ".webm"
    ts = store.la_now_iso().replace(":", "").replace("-", "")
    name = f"{item_id.replace(':', '_')}-{ts}{ext}"
    dst = recitemod.audio_dir("takes") / name
    dst.write_bytes(blob)

    st = recitemod.get_state(conn, item_id)
    first_exposure = int(form.get("first_exposure") or 0) and not int(st["exposed"] or 0)
    cur = conn.execute(
        "INSERT INTO recite_takes(item_id, ts, mode, first_exposure, variant, variant_type,"
        " variant_sec, why_line, audio_path, self_est, gate_note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, store.la_now_iso(), form.get("mode") or "recall", int(first_exposure),
         form.get("variant_q") or "", form.get("variant_type") or "original",
         int(form.get("variant_sec") or it.get("target_sec") or 60),
         (form.get("why_line") or "").strip(), name,
         float(form.get("self_est")) if form.get("self_est") else None,
         form.get("variant_hint") or ""))
    conn.commit()
    take_id = int(cur.lastrowid)
    # ── 库优先返工（v2）：评分降级为可选。默认只存盘，不跑 ASR + 判官。 ──────────
    # 她要的是「听原声 → 自己练 → 回听对比」，评分是这条链之外的一个按钮。
    # 前端要评分时带 score=1，或者事后调 /api/recite/take/<id>/rescore。
    if not str(form.get("score") or "").strip() in ("1", "true", "yes", "on"):
        return {"take_id": take_id, "saved": True, "scored": False,
                "audio_url": f"/recite/audio/{item_id}/take-{take_id}",
                "scores": {}, "transcript": "", "verdict": "", "total": None,
                "gate_note": ""}
    try:
        return _score_take(conn, take_id, it)
    except Exception as e:      # 评分链任何一环挂掉都不该吃掉录音
        return {"take_id": take_id, "saved": True, "judge_error": f"{type(e).__name__}: {e}",
                "scores": {}, "transcript": "", "verdict": "", "total": None,
                "gate_note": "评分没跑出来，录音已存"}


@app.post("/api/recite/take/{take_id}/rescore")
def api_recite_rescore(take_id: int, body: dict | None = None, conn=Depends(view_conn)):
    """[让 AI 评一下] 走的就是这条。body 可选 {why_line, mode} —— why 现在在录完之后才问，
    所以要允许回填到已经落盘的 take 行上；评分本身一个字没改。"""
    _recite_conn(conn)
    row = conn.execute("SELECT item_id FROM recite_takes WHERE id=?", (take_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such take")
    body = body or {}
    if str(body.get("why_line") or "").strip():
        conn.execute("UPDATE recite_takes SET why_line=? WHERE id=?",
                     (str(body["why_line"]).strip(), take_id))
    if body.get("mode") in ("recall", "read"):
        conn.execute("UPDATE recite_takes SET mode=? WHERE id=?", (body["mode"], take_id))
    conn.commit()
    return _score_take(conn, take_id, _recite_item(conn, row["item_id"]))


if __name__ == "__main__":
    import uvicorn

    ui = cfg.get("ui") or {}
    uvicorn.run("app.web:app", host=ui.get("host", "127.0.0.1"),
                port=int(ui.get("port", 8787)))
