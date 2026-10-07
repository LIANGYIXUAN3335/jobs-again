"""Invite-only multi-tenant pilot UI, mounted alongside the legacy owner workspace."""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import time
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import platform_store as ps

router = APIRouter()
COOKIE = "jobscan_session"


def esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def _secret() -> bytes:
    return os.environ.get("JOBSCAN_SESSION_SECRET", "local-development-only-change-me").strip().encode()


def _sign(user_id: str) -> str:
    expiry = str(int(time.time()) + 7 * 86400)
    body = f"{user_id}.{expiry}"
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _actor(request: Request, conn) -> dict | None:
    parts = request.cookies.get(COOKIE, "").split(".")
    if len(parts) != 3 or not parts[1].isdigit() or int(parts[1]) < time.time():
        return None
    body = ".".join(parts[:2])
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parts[2]):
        return None
    return ps.get_user(conn, parts[0])


def _split(value: str) -> list[str]:
    return [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]


def _layout(title: str, body: str, actor: dict | None = None, notice: str = "") -> HTMLResponse:
    nav = ""
    if actor:
        admin = '<a href="/portal/admin">Admin</a>' if actor["role"] in ("owner", "admin") else ""
        nav = f'<nav><a href="/portal">Workspace</a><a href="/portal/settings">Configuration</a>{admin}<form method="post" action="/portal/logout"><button>Sign out</button></form></nav>'
    msg = f'<div class="notice">{notice}</div>' if notice else ""
    return HTMLResponse(f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · JobScan</title><link rel="stylesheet" href="/static/platform.css"></head><body><header><a class="brand" href="/portal"><span>JS</span> JobScan</a>{nav}</header><main>{msg}{body}</main></body></html>''')


def _need_actor(request: Request, conn, admin: bool = False) -> dict:
    actor = _actor(request, conn)
    if not actor:
        raise HTTPException(401, "sign in required")
    if admin and actor["role"] not in ("owner", "admin"):
        raise HTTPException(403, "admin required")
    return actor


@router.get("/portal/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    return _layout("Sign in", f'''<section class="auth"><div><div class="eyebrow">INVITE-ONLY PILOT</div><h1>Turn one trusted career profile into job-ready resumes.</h1><p class="lede">Continuous technical-job discovery, role matching and fact-safe tailoring in one workspace.</p></div><form method="post" action="/portal/login" class="panel"><label>Email<input type="email" name="email" required></label><label>Access code<input type="password" name="code" required></label><button class="primary">Sign in</button><p class="muted">New customer? <a href="/portal/register">Activate an invitation</a></p>{f'<p class="error">{esc(error)}</p>' if error else ''}</form></section>''')


@router.post("/portal/login")
def login(email: str = Form(...), code: str = Form(...)):
    with ps.connect() as conn:
        user = ps.authenticate(conn, email, code)
    if not user:
        return RedirectResponse("/portal/login?error=" + quote("邮箱或登录码不正确"), 303)
    resp = RedirectResponse("/portal", 303)
    resp.set_cookie(COOKIE, _sign(user["id"]), httponly=True, secure=os.environ.get("JOBSCAN_COOKIE_SECURE") == "1", samesite="lax", max_age=7 * 86400)
    return resp


@router.post("/portal/logout")
def logout():
    resp = RedirectResponse("/portal/login", 303)
    resp.delete_cookie(COOKIE)
    return resp


@router.get("/portal/register", response_class=HTMLResponse)
def register_page(error: str = ""):
    return _layout("Activate", f'''<section class="auth compact"><div><div class="eyebrow">ACTIVATE WORKSPACE</div><h1>Create your private workspace</h1><p class="lede">Your resume, preferences and job pipeline stay isolated from every other customer.</p></div><form method="post" action="/portal/register" class="panel"><label>Name<input name="name" required></label><label>Email<input type="email" name="email" required></label><label>Invitation code<input name="invite" required></label><label>Create access code<input type="password" name="code" minlength="8" required></label><button class="primary">Activate</button>{f'<p class="error">{esc(error)}</p>' if error else ''}</form></section>''')


@router.post("/portal/register")
def register(name: str = Form(...), email: str = Form(...), invite: str = Form(...), code: str = Form(...)):
    try:
        with ps.connect() as conn:
            user = ps.register(conn, email, name, code, invite)
    except (ValueError, sqlite3.IntegrityError) as exc:
        return RedirectResponse("/portal/register?error=" + quote(str(exc)), 303)
    resp = RedirectResponse("/portal", 303)
    resp.set_cookie(COOKIE, _sign(user["id"]), httponly=True,
                    secure=os.environ.get("JOBSCAN_COOKIE_SECURE") == "1",
                    samesite="lax", max_age=7 * 86400)
    return resp


@router.get("/portal", response_class=HTMLResponse)
def dashboard(request: Request, notice: str = ""):
    with ps.connect() as conn:
        actor = _actor(request, conn)
        if not actor:
            return RedirectResponse("/portal/login", 303)
        data = ps.dashboard(conn, actor)
    resume = data["resume"]
    roles = data["config"]["target_roles"]
    role_names = {"software": "Software / Infrastructure", "data": "Data Engineering", "mle": "MLE / Applied AI", "systems": "Systems / GPU / Hardware"}
    role_chips = "".join(f'<span class="chip">{role_names[r]}</span>' for r in roles) or '<span class="muted">No role profiles selected</span>'
    if resume:
        action = f'<form method="post" action="/portal/resume/{resume["id"]}/confirm"><button class="primary">Confirm facts and approve</button></form>' if resume["status"] == "customer_confirmation" else ""
        resume_card = f'<div class="metric"><span>Base Resume v{resume["version"]}</span><strong class="status {resume["status"]}">{resume["status"].replace("_", " ")}</strong><p>{esc(resume["admin_notes"] or "Waiting for the next review step.")}</p>{action}</div>'
    else:
        resume_card = '<div class="empty"><strong>No Base Resume yet</strong><p>Paste the factual source resume below. Tailoring stays locked until admin review and your confirmation.</p></div>'
    gate = "Ready for automatic tailoring" if data["tailoring_enabled"] else "Tailoring locked until Base Resume approval"
    body = f'''<section class="hero"><div><div class="eyebrow">{actor["role"].upper()} WORKSPACE</div><h1>Good morning, {esc(actor["display_name"])}.</h1><p class="lede">Your job pipeline starts from one verified source of truth.</p></div><div class="readiness"><span>PIPELINE STATUS</span><strong>{gate}</strong></div></section><section class="grid"><article><h2>Target lanes</h2><div class="chips">{role_chips}</div><p class="muted">Match threshold {data["config"]["min_match_score"]} · Daily tailor cap {data["config"]["daily_tailor_limit"]}</p><a class="text-link" href="/portal/settings">Edit configuration →</a></article><article><h2>Resume readiness</h2>{resume_card}</article></section><section class="workspace"><div><h2>Submit a new source resume</h2><p class="muted">A new upload creates a version. Previous approved versions remain in the audit history.</p></div><form method="post" action="/portal/resume"><textarea name="source_text" rows="14" placeholder="Paste the complete resume text here..." required></textarea><button class="primary">Send for admin review</button></form></section>'''
    return _layout("Workspace", body, actor, notice)


@router.post("/portal/resume")
def upload_resume(request: Request, source_text: str = Form(...)):
    with ps.connect() as conn:
        actor = _need_actor(request, conn)
        try:
            ps.upload_resume(conn, actor, source_text)
        except ValueError as exc:
            return RedirectResponse("/portal?notice=" + quote(str(exc)), 303)
    return RedirectResponse("/portal?notice=" + quote("Resume verson created and sent to admin review."), 303)


@router.post("/portal/resume/{resume_id}/confirm")
def confirm_resume(resume_id: str, request: Request):
    with ps.connect() as conn:
        actor = _need_actor(request, conn)
        ps.confirm_resume(conn, actor, resume_id)
    return RedirectResponse("/portal?notice=" + quote("Base Resume approved. Tailoring is now enabled."), 303)


@router.get("/portal/settings", response_class=HTMLResponse)
def settings(request: Request, notice: str = ""):
    with ps.connect() as conn:
        actor = _actor(request, conn)
        if not actor:
            return RedirectResponse("/portal/login", 303)
        c = ps.config_for(conn, actor["id"])
    check = lambda r: "checked" if r in c["target_roles"] else ""
    body = f'''<section class="pagehead"><div class="eyebrow">CUSTOMER CONFIGURATION</div><h1>Decide what enters your pipeline.</h1><p>Hard filters run before model work, so unsuitable jobs never spend tailoring capacity.</p></section><form method="post" action="/portal/settings" class="settings-grid"><fieldset><legend>Target roles</legend><label class="check"><input type="checkbox" name="roles" value="software" {check("software")}> Software / Backend / Infrastructure</label><label class="check"><input type="checkbox" name="roles" value="data" {check("data")}> Data Engineering / Platform</label><label class="check"><input type="checkbox" name="roles" value="mle" {check("mle")}> MLE / Applied AI</label><label class="check"><input type="checkbox" name="roles" value="systems" {check("systems")}> Systems / GPU / Hardware</label></fieldset><fieldset><legend>Location and companies</legend><label>Preferred locations<input name="locations" value="{', '.join(c['locations'])}" placeholder="Bay Area, Remote, Seattle"></label><label>Company watchlist<input name="watchlist" value="{', '.join(c['company_watchlist'])}" placeholder="NVIDIA, Google, Waymo"></label><label>Company blacklist<input name="blacklist" value="{', '.join(c['company_blacklist'])}"></label></fieldset><fieldset><legend>Automation limits</legend><label>Minimum match score<input type="number" min="0" max="100" name="score" value="{c['min_match_score']}"></label><label>Maximum tailored resumes per day<input type="number" min="0" max="100" name="limit" value="{c['daily_tailor_limit']}"></label><p class="muted">Only jobs that pass eligibility, score and duplicate gates are queued.</p></fieldset><button class="primary save">Save configuration</button></form>'''
    return _layout("Configuration", body, actor, notice)


@router.post("/portal/settings")
async def save_settings(request: Request):
    form = await request.form()
    with ps.connect() as conn:
        actor = _need_actor(request, conn)
        ps.save_config(conn, actor, {"target_roles": form.getlist("roles"), "locations": _split(form.get("locations", "")), "company_watchlist": _split(form.get("watchlist", "")), "company_blacklist": _split(form.get("blacklist", "")), "min_match_score": form.get("score", 75), "daily_tailor_limit": form.get("limit", 10)})
    return RedirectResponse("/portal/settings?notice=" + quote("Configuration saved."), 303)


@router.get("/portal/admin", response_class=HTMLResponse)
def admin(request: Request, notice: str = "", invite: str = ""):
    with ps.connect() as conn:
        actor = _actor(request, conn)
        if not actor:
            return RedirectResponse("/portal/login", 303)
        if actor["role"] not in ("owner", "admin"):
            raise HTTPException(403)
        snap = ps.admin_snapshot(conn)
    users = "".join(f'<tr><td>{esc(u["display_name"])}</td><td>{esc(u["email"])}</td><td><span class="chip">{u["role"]}</span></td><td>{f"<form method=post action=/portal/admin/users/{u[chr(34)+"id"+chr(34)]}/role><select name=role><option>customer</option><option>admin</option></select><button>Update</button></form>" if actor["role"] == "owner" and u["role"] != "owner" else ""}</td></tr>' for u in snap["users"])
    reviews = "".join(f'<article class="review"><div><strong>{esc(r["display_name"])}</strong><span>{esc(r["email"])} · v{r["version"]}</span></div><pre>{esc(r["source_text"])}</pre><form method="post" action="/portal/admin/resumes/{r["id"]}/review"><textarea name="notes" rows="3" placeholder="Review notes for the customer"></textarea><button name="decision" value="reject">Return</button><button class="primary" name="decision" value="approve">Send for customer confirmation</button></form></article>' for r in snap["pending"]) or '<div class="empty">No resumes waiting for review.</div>'
    invite_box = f'<div class="invite-result"><span>New invitation code</span><strong>{invite}</strong><small>Shown once. Send it to the customer securely.</small></div>' if invite else ""
    body = f'''<section class="pagehead"><div class="eyebrow">OPERATIONS</div><h1>Admin control room</h1><p>Invite customers, review factual resumes and keep every privileged action auditable.</p></section><section class="grid admin-grid"><article><h2>Create invitation</h2><form method="post" action="/portal/admin/invites"><label>Bind to email (optional)<input type="email" name="email"></label><label>Valid days<input type="number" name="days" value="7" min="1" max="30"></label><button class="primary">Create code</button></form>{invite_box}</article><article><h2>Accounts</h2><table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th></th></tr></thead><tbody>{users}</tbody></table></article></section><section class="workspace"><h2>Base Resume review queue</h2>{reviews}</section>'''
    return _layout("Admin", body, actor, notice)


@router.post("/portal/admin/invites")
def make_invite(request: Request, email: str = Form(""), days: int = Form(7)):
    with ps.connect() as conn:
        actor = _need_actor(request, conn, admin=True)
        code = ps.create_invite(conn, actor, email, max(1, min(days, 30)))
    return RedirectResponse("/portal/admin?invite=" + quote(code), 303)


@router.post("/portal/admin/resumes/{resume_id}/review")
def review(resume_id: str, request: Request, decision: str = Form(...), notes: str = Form("")):
    with ps.connect() as conn:
        actor = _need_actor(request, conn, admin=True)
        ps.review_resume(conn, actor, resume_id, decision, notes)
    return RedirectResponse("/portal/admin?notice=" + quote("Review decision saved."), 303)


@router.post("/portal/admin/users/{user_id}/role")
def change_role(user_id: str, request: Request, role: str = Form(...)):
    with ps.connect() as conn:
        actor = _need_actor(request, conn, admin=True)
        ps.set_role(conn, actor, user_id, role)
    return RedirectResponse("/portal/admin?notice=" + quote("Account role updated."), 303)


@router.get("/api/platform/health")
def health():
    with ps.connect() as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    return {"ok": True, "service": "jobscan-platform", "users": users}


# sqlite is only referenced in one exception path; keep imports visible and explicit.
import sqlite3  # noqa: E402
