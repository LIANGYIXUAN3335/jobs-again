"""Small, dependency-free control-plane store for the invite-only SaaS pilot.

The existing scanner databases remain untouched.  This database owns accounts,
tenant configuration and base-resume approval; jobs stay in the shared corpus.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import sqlite3
from pathlib import Path

from . import config as cfgmod


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL CHECK(role IN ('owner','admin','customer')),
  access_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
  code_hash TEXT PRIMARY KEY,
  created_by TEXT NOT NULL,
  email TEXT,
  expires_at TEXT NOT NULL,
  used_by TEXT,
  used_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_configs (
  user_id TEXT PRIMARY KEY,
  target_roles TEXT NOT NULL DEFAULT '[]',
  locations TEXT NOT NULL DEFAULT '[]',
  company_watchlist TEXT NOT NULL DEFAULT '[]',
  company_blacklist TEXT NOT NULL DEFAULT '[]',
  min_match_score INTEGER NOT NULL DEFAULT 75,
  daily_tailor_limit INTEGER NOT NULL DEFAULT 10,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS resume_bases (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  source_text TEXT NOT NULL,
  admin_notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK(status IN
    ('uploaded','analyzed','admin_review','customer_confirmation','approved','rejected')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  approved_by TEXT,
  approved_at TEXT,
  UNIQUE(user_id, version),
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id TEXT,
  action TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT,
  details TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
"""

ROLE_FAMILIES = ("software", "data", "mle", "systems")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    raw = os.environ.get("JOBSCAN_PLATFORM_DB", "data/platform.db")
    p = Path(raw)
    return p if p.is_absolute() else cfgmod.ROOT / p


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    bootstrap_owner(conn)
    return conn


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _password_hash(value: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", value.encode(), bytes.fromhex(salt), 310_000)
    return f"{salt}${digest.hex()}"


def _password_matches(value: str, encoded: str) -> bool:
    try:
        salt, _ = encoded.split("$", 1)
    except ValueError:
        # One-way compatibility for an early local pilot database.
        return secrets.compare_digest(encoded, _hash(value))
    return secrets.compare_digest(encoded, _password_hash(value, salt))


def _user_id(email: str) -> str:
    return hashlib.sha256(email.lower().strip().encode()).hexdigest()[:20]


def bootstrap_owner(conn: sqlite3.Connection) -> None:
    email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    code = os.environ.get("BOOTSTRAP_ADMIN_CODE", "").strip()
    if not email or not code:
        return
    ts = now()
    uid = _user_id(email)
    conn.execute(
        "INSERT INTO users(id,email,display_name,role,access_hash,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET role='owner', "
        "active=1, updated_at=excluded.updated_at",
        (uid, email, "Alex", "owner", _password_hash(code), ts, ts),
    )
    conn.execute(
        "INSERT OR IGNORE INTO user_configs(user_id,updated_at) VALUES(?,?)", (uid, ts)
    )
    conn.commit()


def authenticate(conn: sqlite3.Connection, email: str, code: str) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE email=? AND active=1", (email.strip().lower(),)).fetchone()
    if not row or not _password_matches(code, row["access_hash"]):
        return None
    return dict(row)


def get_user(conn: sqlite3.Connection, user_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
    return dict(row) if row else None


def create_invite(conn: sqlite3.Connection, actor: dict, email: str = "", days: int = 7) -> str:
    if actor["role"] not in ("owner", "admin"):
        raise PermissionError("admin required")
    code = secrets.token_urlsafe(9)
    ts = now()
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO invites(code_hash,created_by,email,expires_at,created_at) VALUES(?,?,?,?,?)",
        (_hash(code), actor["id"], email.strip().lower() or None, expires, ts),
    )
    audit(conn, actor["id"], "invite.created", "invite", None, {"email": email, "days": days})
    conn.commit()
    return code


def register(conn: sqlite3.Connection, email: str, name: str, access_code: str, invite_code: str) -> dict:
    email = email.strip().lower()
    inv = conn.execute("SELECT * FROM invites WHERE code_hash=?", (_hash(invite_code),)).fetchone()
    if not inv or inv["used_by"] or inv["expires_at"] < now():
        raise ValueError("邀请码无效或已经过期")
    if inv["email"] and inv["email"] != email:
        raise ValueError("这个邀请码绑定了其他邮箱")
    if len(access_code) < 8:
        raise ValueError("登录码至少需要 8 个字符")
    uid, ts = _user_id(email), now()
    conn.execute(
        "INSERT INTO users(id,email,display_name,role,access_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (uid, email, name.strip() or email.split("@")[0], "customer", _password_hash(access_code), ts, ts),
    )
    conn.execute("INSERT INTO user_configs(user_id,updated_at) VALUES(?,?)", (uid, ts))
    conn.execute("UPDATE invites SET used_by=?,used_at=? WHERE code_hash=?", (uid, ts, _hash(invite_code)))
    audit(conn, uid, "user.registered", "user", uid)
    conn.commit()
    return get_user(conn, uid)


def config_for(conn: sqlite3.Connection, user_id: str) -> dict:
    row = conn.execute("SELECT * FROM user_configs WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        raise KeyError(user_id)
    out = dict(row)
    for key in ("target_roles", "locations", "company_watchlist", "company_blacklist"):
        out[key] = json.loads(out[key] or "[]")
    return out


def save_config(conn: sqlite3.Connection, actor: dict, values: dict) -> dict:
    roles = [x for x in values.get("target_roles", []) if x in ROLE_FAMILIES]
    score = min(100, max(0, int(values.get("min_match_score", 75))))
    limit = min(100, max(0, int(values.get("daily_tailor_limit", 10))))
    packed = lambda key: json.dumps(sorted(set(x.strip() for x in values.get(key, []) if x.strip())))
    conn.execute(
        "UPDATE user_configs SET target_roles=?,locations=?,company_watchlist=?,company_blacklist=?,"
        "min_match_score=?,daily_tailor_limit=?,updated_at=? WHERE user_id=?",
        (json.dumps(roles), packed("locations"), packed("company_watchlist"),
         packed("company_blacklist"), score, limit, now(), actor["id"]),
    )
    audit(conn, actor["id"], "config.updated", "user", actor["id"])
    conn.commit()
    return config_for(conn, actor["id"])


def upload_resume(conn: sqlite3.Connection, actor: dict, text: str) -> dict:
    text = text.strip()
    if len(text) < 80:
        raise ValueError("简历内容太短，请粘贴完整简历")
    version = conn.execute(
        "SELECT COALESCE(MAX(version),0)+1 FROM resume_bases WHERE user_id=?", (actor["id"],)
    ).fetchone()[0]
    rid, ts = secrets.token_hex(10), now()
    conn.execute(
        "INSERT INTO resume_bases(id,user_id,version,source_text,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)", (rid, actor["id"], version, text, "admin_review", ts, ts)
    )
    audit(conn, actor["id"], "resume.uploaded", "resume", rid, {"version": version})
    conn.commit()
    return dict(conn.execute("SELECT * FROM resume_bases WHERE id=?", (rid,)).fetchone())


def latest_resume(conn: sqlite3.Connection, user_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM resume_bases WHERE user_id=? ORDER BY version DESC LIMIT 1", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def review_resume(conn: sqlite3.Connection, actor: dict, resume_id: str, decision: str, notes: str) -> None:
    if actor["role"] not in ("owner", "admin"):
        raise PermissionError("admin required")
    status = {"approve": "customer_confirmation", "reject": "rejected"}.get(decision)
    if not status:
        raise ValueError("invalid decision")
    conn.execute(
        "UPDATE resume_bases SET status=?,admin_notes=?,updated_at=? WHERE id=?",
        (status, notes.strip(), now(), resume_id),
    )
    audit(conn, actor["id"], f"resume.{decision}", "resume", resume_id)
    conn.commit()


def confirm_resume(conn: sqlite3.Connection, actor: dict, resume_id: str) -> None:
    row = conn.execute("SELECT * FROM resume_bases WHERE id=? AND user_id=?", (resume_id, actor["id"])).fetchone()
    if not row or row["status"] != "customer_confirmation":
        raise ValueError("这份简历还不能确认")
    ts = now()
    conn.execute("UPDATE resume_bases SET status='approved',approved_by=?,approved_at=?,updated_at=? WHERE id=?",
                 (actor["id"], ts, ts, resume_id))
    audit(conn, actor["id"], "resume.confirmed", "resume", resume_id)
    conn.commit()


def dashboard(conn: sqlite3.Connection, actor: dict) -> dict:
    cfg = config_for(conn, actor["id"])
    resume = latest_resume(conn, actor["id"])
    return {"config": cfg, "resume": resume, "tailoring_enabled": bool(resume and resume["status"] == "approved")}


def admin_snapshot(conn: sqlite3.Connection) -> dict:
    users = [dict(x) for x in conn.execute("SELECT id,email,display_name,role,active,created_at FROM users ORDER BY created_at")]
    pending = [dict(x) for x in conn.execute(
        "SELECT r.*,u.email,u.display_name FROM resume_bases r JOIN users u ON u.id=r.user_id "
        "WHERE r.status='admin_review' ORDER BY r.created_at"
    )]
    return {"users": users, "pending": pending}


def set_role(conn: sqlite3.Connection, actor: dict, user_id: str, role: str) -> None:
    if actor["role"] != "owner":
        raise PermissionError("owner required")
    if role not in ("admin", "customer") or user_id == actor["id"]:
        raise ValueError("invalid role change")
    conn.execute("UPDATE users SET role=?,updated_at=? WHERE id=?", (role, now(), user_id))
    audit(conn, actor["id"], "user.role_changed", "user", user_id, {"role": role})
    conn.commit()


def audit(conn, actor_id: str | None, action: str, target_type: str, target_id: str | None,
          details: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_logs(actor_id,action,target_type,target_id,details,created_at) VALUES(?,?,?,?,?,?)",
        (actor_id, action, target_type, target_id, json.dumps(details or {}), now()),
    )
