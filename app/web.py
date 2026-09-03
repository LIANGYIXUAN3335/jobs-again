"""Local review UI + JSON API. Writes ONLY to user_state.db.
Usage: python -m app.web  →  http://127.0.0.1:8787"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import ai as aimod
from . import company_board as boardmod
from . import config as cfgmod
from . import followup as followupmod
from . import funnel as funnelmod
from . import gcal as gcalmod
from . import heartbeats as hbmod
from . import ideas as ideasmod
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

store.seed_applied_companies(cfg)  # one-way merge of config's applied_companies list


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
    # 'diversity' = keep select_daily's company interleave (each company's lead role first,
    # then everyone's #2, …). Default in the review tab because it is the order she works
    # top-down in; the other options re-sort those same rows.
    sort: str = "diversity",
    rot: str = "",
    conn=Depends(view_conn),
):
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
        rot=rot_i,
    )
    params = {
        "status": status, "q": q, "company": company, "min_score": min_score if ms else "",
        "verdict": verdict, "seen": seen, "remote": remote, "seniority": seniority,
        "sort": sort, "rot": rot_i or "",
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
        "company_applied": [                             # OTHER roles at this company already done
            {"title": r["title"], "status": r["status"], "date": r["applied_date"]}
            for r in prior[:6]
        ],
        "mailcode_enabled": mailmod.available(cfg),
        "eeo_decline": bool(cfg.get("eeo_decline", True)),
        "eeo": cfg.get("eeo") or {},          # per-field self-ID preference; unset → decline
        "profile": cfg.get("profile") or {},
        "experience": profile_experience(role),   # work history + bullets for Workday's My Experience
        "skills": profile_skills(role),           # for Workday's "Type to Add Skills" typeahead
        "top_role_default": top_role_default,      # true → chosen as the company's best guess
    }


@app.get("/api/lookup")
def api_lookup(url: str, conn=Depends(view_conn)):
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
    return _lookup_payload(conn, role)


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
def api_top_role(company: str, conn=Depends(view_conn)):
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
        " AND COALESCE(s.status,'new') NOT IN ('applied','interviewing','rejected')"
        " ORDER BY r.weighted_score DESC LIMIT 1", (cn,)).fetchone()
    if not row:
        raise HTTPException(404, f"no un-applied tailored role for {company!r}")
    role = store.get_role(conn, row["job_id"])
    return _lookup_payload(conn, role, top_role_default=True)


@app.get("/api/profile")
def api_profile():
    """Role-independent profile for forms whose URL carries no job id (Google/Microsoft careers).
    The extension fills name/email/phone/eligibility/consent from this — no résumé, no
    role-specific questions, since we can't tell which posting it is."""
    ai_cfg = cfg.get("ai") or {}
    return {
        "profile_only": True,
        "profile": cfg.get("profile") or {},
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
        raise HTTPException(503, "email scan disabled (set mailcode.email + gmail_app_password)")
    try:
        return applied_scan.scan(cfg, days=days)
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


@app.get("/api/funnel")
def api_funnel(by: str = "score,match,company,ats", conn=Depends(view_conn)):
    dims = [d.strip() for d in by.split(",") if d.strip() in funnelmod._DIMENSIONS]
    rep = funnelmod.report(cfg, conn, dims or ["score"])
    rep.pop("apps", None)                       # the per-application rows are big and unused here
    return rep


@app.get("/funnel", response_class=HTMLResponse)
def funnel_page(conn=Depends(view_conn)):
    dims = ["score", "match", "culture", "company", "ats"]
    rep = funnelmod.report(cfg, conn, dims)
    o, s, tune = rep["overall"], rep["signal"], rep["tuning"]
    if not o["applied"]:
        return _page("Funnel", '<h1>📊 Funnel</h1><p class="tip">No applications recorded yet.</p>')

    def rate(r):
        if r["response_rate"] is None:
            return '<span class="muted">—</span>'
        txt = f'{r["response_rate"]:.0f}%'
        return txt if r["enough"] else f'{txt} <span class="muted small">?</span>'

    tables = ""
    for d in dims:
        rows = "".join(
            f'<tr><td>{r["bucket"]}</td><td class="n">{r["applied"]}</td>'
            f'<td class="n">{r["mature"]}</td><td class="n">{r["responded"]}</td>'
            f'<td class="n">{r["interviewed"]}</td><td class="n">{r["offered"]}</td>'
            f'<td class="n">{r["quiet"]}</td><td class="n">{rate(r)}</td></tr>'
            for r in rep["by"][d][:20])
        tables += (f'<h2>by {d}</h2><table><tr><th>{d}</th><th class="n">applied</th>'
                   f'<th class="n">judgeable</th><th class="n">replied</th><th class="n">intv</th>'
                   f'<th class="n">offer</th><th class="n">quiet</th><th class="n">reply rate</th>'
                   f'</tr>{rows}</table>')

    if s["verdict"] == "not enough data":
        signal = (f'<p class="tip warn"><b>Does the role score predict a reply?</b> '
                  f'Not answerable yet — {s["n"]} judgeable applications, need about {s["need"]}. '
                  f'Record outcomes on <a href="/outcomes">/outcomes</a> and this fills itself in.</p>')
    else:
        signal = (f'<p class="tip"><b>Does the role score predict a reply?</b> {s["verdict"]} — '
                  f'bottom half {s["low_half_rate"]:.0f}% vs top half {s["high_half_rate"]:.0f}% '
                  f'(split at {s["cutoff"]:.0f}, n={s["n"]}).</p>')

    return _page("Funnel", f"""<h1>📊 Funnel</h1>
      <p>{o['applied']} applications · {o['companies']} companies ·
         <b>{o['mature']}</b> old enough to judge (≥{o['window']}d),
         {o['pending']} still too fresh</p>
      <p>replied <b>{o['responded']}</b> · interview+ <b>{o['interviewed']}</b> ·
         offer <b>{o['offered']}</b> · rejected {o['rejected']} · quiet {o['quiet']}</p>
      {signal}{tables}
      <p class="small muted">Applications younger than {o['window']} days are excluded from every
      rate — counting yesterday's silence as a rejection would drag every number to zero.
      "?" marks a bucket with fewer than {tune['min_n']} judgeable applications.</p>""")


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
    total_open = sum(r["n_actions"] - r["n_done"] for r in rows)

    blocks = []
    for r in rows:
        items = store.interview_items_for(conn, r["marker_id"])
        acts = [i for i in items if i["kind"] == "action"]
        qs = [i for i in items if i["kind"] in ("tech_q", "ai_q")]
        act_html = "".join(
            f'<li class="{ "done" if i["done"] else "" }">{"✅" if i["done"] else "⬜"} '
            f'{i["text"]}</li>' for i in acts)
        q_html = "".join(f'<li>{"🤖 " if i["kind"] == "ai_q" else ""}{i["text"]}</li>' for i in qs)
        src = json.loads(r["sources"] or "{}")
        links = " · ".join(f'<a href="/interviews/file?f={fn}" target="_blank">{fn.split(".")[0][:28]}</a>'
                           for fn in src)
        if r["review_path"]:
            links = (f'<a href="/interviews/file?f={quote(r["review_path"])}" target="_blank">'
                     f'📄 transcript</a> · ') + links
        if r["transcript_url"]:
            links = (f'<a href="{r["transcript_url"]}" target="_blank" rel="noopener">'
                     f'🎙 Otter</a> · ') + links
        role_tag = (f'<span class="pill hi">{r["role_company"]} — {(r["role_title"] or "")[:40]}</span>'
                    if r["job_id"] else
                    f'<select class="lk" data-m="{r["marker_id"]}"><option value="">link to a role…</option>{opts}</select>')
        blocks.append(f"""
        <div class="iv">
          <div class="ivh"><b>{r['company']}</b> · {r['happened_on']} · {r['round_type'] or '—'}
            {role_tag}
            <span class="small muted">act {r['n_done']}/{r['n_actions']} · q {r['n_questions']} · {links}</span></div>
          {f'<details><summary class="small">action items</summary><ul class="ivl">{act_html}</ul></details>' if acts else ''}
          {f'<details><summary class="small">questions asked</summary><ul class="ivl">{q_html}</ul></details>' if qs else ''}
        </div>""")

    return _page("Interviews", f"""<h1>🎤 Interviews</h1>
      <p class="tip">每场面试的问题与 action items，索引自 <code>面试复盘/</code>（markdown 是唯一事实源，
      这里只读）。<b>{total_open} 个 action item 还开着。</b> 勾选状态在 md 里改，跑一次 ingest 就同步。</p>
      <p><button onclick="ing(this)">re-ingest from 面试复盘/</button></p>
      <style>.iv{{border-bottom:1px solid #eef2f6;padding:10px 0}}.ivh{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
      .ivl{{margin:6px 0 2px;padding-left:18px}}.ivl li{{margin:3px 0}}.ivl li.done{{color:#94a3b8}}
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
      <div id="sum"></div><div id="out"></div>
      <script>
      function esc(s){const e=document.createElement('div');e.textContent=s||'';return e.innerHTML}
      function ovBtns(r){
        const co=encodeURIComponent(r.company),
              zh={closed:'手动结案',hidden:'隐藏',booked:'已排期',acked:'已处理'};
        const a=(act,label,tip)=>'<a href="#" title="'+tip+'" onclick="return ov(`'+co+'`,'+
          (act?'`'+act+'`':'null')+')">'+label+'</a>';
        if(r.override) return '已手动标记：'+zh[r.override]+' '+a(null,'撤销','清除人工标记');
        return a('acked','✓ 已处理','在邮件之外回过了（平台/电话/日历）——欠账清零，来新邮件会自动复活')+' · '+
               a('booked','📅 已排期','约面藏在正文/日历里，机器没读出来时点这个')+' · '+
               a('closed','✕ 结案','这条线结束了（含你拒了对方的情况）')+' · '+
               a('hidden','🙈 隐藏','误认的公司/不想再看');
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


@app.get("/health", response_class=HTMLResponse)
def health_page():
    """体检表：每个定时任务经 tools/hb_run.sh 打卡，这里读卡画灯（app/heartbeats.py）。"""
    rows = hbmod.read()
    icon = {"ok": "🟢", "fail": "🔴", "stale": "🔴", "never": "⚪"}
    order = {"fail": 0, "stale": 1, "ok": 2, "never": 3}
    rows.sort(key=lambda r: order[r["status"]])
    trs = "".join(
        f"<tr><td>{icon[r['status']]}</td><td><b>{r['zh']}</b> <span class='d'>{r['name']}</span></td>"
        f"<td>{r['cadence']}</td><td>{r['finished'] or '—'}</td>"
        f"<td>{('%ds' % r['duration_s']) if r['duration_s'] is not None else '—'}</td>"
        f"<td>{r['detail']}</td></tr>"
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
      <table><tr><th></th><th>任务</th><th>节奏</th><th>上次打卡</th><th>用时</th><th>状态</th></tr>{trs}</table>""")


if __name__ == "__main__":
    import uvicorn

    ui = cfg.get("ui") or {}
    uvicorn.run("app.web:app", host=ui.get("host", "127.0.0.1"),
                port=int(ui.get("port", 8787)))
