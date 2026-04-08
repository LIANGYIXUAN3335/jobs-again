"""Local review UI + JSON API. Writes ONLY to user_state.db.
Usage: python -m app.web  →  http://127.0.0.1:8787"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import ai as aimod
from . import config as cfgmod
from . import ingest as ingestmod
from . import mailcode as mailmod
from . import store, tailoring

cfg = cfgmod.load_config()
app = FastAPI(title="JobScan")
# The browser extension's service worker calls this local API. Server binds 127.0.0.1
# only, so a permissive CORS policy here is not remotely reachable.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def url_candidates(url: str) -> list[str]:
    """Raw ATS ids that might appear in an application-page URL."""
    c: list[str] = []
    for pat in (r"gh_jid=(\d+)", r"[?&]token=(\d+)", r"/jobs/(\d+)"):
        c += re.findall(pat, url)
    c += _UUID.findall(url)
    seen, out = set(), []
    for x in c:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
app.mount("/static", StaticFiles(directory=str(cfgmod.ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(cfgmod.ROOT / "templates"))

store.seed_applied_companies(cfg)  # one-way merge of config's applied_companies list


def view_conn():
    conn = store.connect_view(cfg)
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
    min_score: float = 0.0,
    verdict: str = "",
    seen: str = "all",
    remote: str = "",
    seniority: str = "",
    sort: str = "score",
    conn=Depends(view_conn),
):
    data = store.fetch_view(
        conn, cfg, status=status, q=q, company=company, min_score=min_score,
        verdict=verdict, seen=seen, remote=remote, seniority=seniority, sort=sort,
    )
    params = {
        "status": status, "q": q, "company": company, "min_score": min_score or "",
        "verdict": verdict, "seen": seen, "remote": remote, "seniority": seniority,
        "sort": sort,
    }
    return templates.TemplateResponse(request, "list.html", {
        **data,
        "params": params,
        "statuses": store.STATUSES,
        "resume_variants": resume_variants(),
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
    if not store.get_role(conn, job_id):
        raise HTTPException(404, "unknown job_id")
    state = store.set_status(conn, job_id, body.status, body.resume_variant)
    return {"ok": True, "state": state}


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
        raise HTTPException(500, f"base résumé missing ({tailoring.BASE_PATH})")
    return {"ok": True, "prompt_path": str(path),
            "hint": "open this file in VS Code and run Claude Code, then click Ingest"}


@app.post("/api/ingest")
def api_ingest(conn=Depends(view_conn)):
    seen: dict = {}
    n = ingestmod._scan_once(cfg, seen, force=True)
    return {"ok": True, "ingested": n}


@app.get("/api/lookup")
def api_lookup(url: str, conn=Depends(view_conn)):
    """The extension asks 'what role is this application page?' Returns role + profile."""
    role = store.lookup_role_by_ids(conn, url_candidates(url))
    if not role:
        raise HTTPException(404, "no matching role for this URL")
    has_pdf = bool(role.get("tailored_resume_path")
                   and Path(role["tailored_resume_path"]).exists())
    return {
        "job_id": role["job_id"], "company": role["company"], "title": role["title"],
        "status": role["status"], "tailor_status": role.get("tailor_status") or "none",
        "has_tailored_pdf": has_pdf,
        "resume_variant": "tailored" if has_pdf else "base",
        "ai_enabled": aimod.available(cfg),
        "mailcode_enabled": mailmod.available(cfg),
        "profile": cfg.get("profile") or {},
    }


@app.get("/api/roles/{job_id}/resume.pdf")
def api_resume_pdf(job_id: str, conn=Depends(view_conn)):
    role = store.get_role(conn, job_id)
    if not role:
        raise HTTPException(404, "unknown job_id")
    p = role.get("tailored_resume_path")
    if not p or not Path(p).exists():
        raise HTTPException(404, "no tailored PDF yet")
    return FileResponse(p, media_type="application/pdf", filename=Path(p).name)


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
def api_email_code():
    """Fetch the newest email verification code (opt-in; reads inbox over IMAP)."""
    if not mailmod.available(cfg):
        raise HTTPException(503, "email-code disabled (set mailcode.enabled + gmail_app_password)")
    try:
        return mailmod.fetch_latest_code(cfg)
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


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    ui = cfg.get("ui") or {}
    uvicorn.run("app.web:app", host=ui.get("host", "127.0.0.1"),
                port=int(ui.get("port", 8787)))
