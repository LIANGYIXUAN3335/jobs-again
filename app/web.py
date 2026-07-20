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
from . import gcal as gcalmod
from . import ingest as ingestmod
from . import mail as sendmail
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
    for pat in (r"gh_jid=(\d+)", r"[?&]token=(\d+)", r"/jobs/(\d+)",
                # Workday req ids: .../Software-Engineer-II_R-279240, .../job/R-273932/...,
                # .../Staff-Software-Engineer_REF078668W  → job_id ends ':R-279240'
                r"[_/]([A-Z]{1,4}-?\d{3,}[A-Za-z0-9]*)"):
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


def profile_experience() -> list[dict]:
    """Work history for the extension's Workday filler, from the LOCKED base résumé."""
    try:
        base = tailoring.load_base(cfg)
    except Exception:
        return []
    return [{
        "company": j.get("company", ""), "title": j.get("title", ""),
        "location": j.get("location", ""), **_parse_dates(j.get("dates", "")),
    } for j in (base.get("experience") or [])]
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
        "eeo_decline": bool(cfg.get("eeo_decline", True)),
        "eeo": cfg.get("eeo") or {},          # per-field self-ID preference; unset → decline
        "profile": cfg.get("profile") or {},
        "experience": profile_experience(),   # work history for Workday's My Experience step
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


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    ui = cfg.get("ui") or {}
    uvicorn.run("app.web:app", host=ui.get("host", "127.0.0.1"),
                port=int(ui.get("port", 8787)))
