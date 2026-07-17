"""Local review UI + JSON API. Writes ONLY to user_state.db.
Usage: python -m app.web  →  http://127.0.0.1:8787"""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import config as cfgmod
from . import store

cfg = cfgmod.load_config()
app = FastAPI(title="JobScan")
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
