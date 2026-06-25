"""Two SQLite files, one writer each:
  data/jobscan.db     — written ONLY by the scanner (roles, scan_runs, meta)
  data/user_state.db  — written ONLY by the UI/tailoring (role_state, applied_companies)
The scanner cannot clobber your status/notes because it never opens that file."""
from __future__ import annotations

import datetime
import json
import sqlite3
from zoneinfo import ZoneInfo

from . import config as cfgmod
from .config import db_path, norm_key, user_db_path

LA = ZoneInfo("America/Los_Angeles")
STATUSES = ["new", "applied", "interviewing", "skip", "rejected"]
CLOSED_STATUSES = {"applied", "skip", "rejected"}

# What happened AFTER applying. `status` answers "is this still in my review queue"; these
# answer "did it work", which is the only thing that can tell you whether the four-dimension
# score predicts anything. Ordered — a role's stage only ever moves right.
STAGES = ["applied", "responded", "screen", "interview", "onsite", "offer"]
STAGE_RANK = {s: i for i, s in enumerate(STAGES)}
# Terminal results. "no_response" is the honest name for silence; it is set by hand or by the
# funnel's staleness rule, never guessed from an absence of email.
OUTCOMES = ["in_progress", "rejected", "no_response", "offer", "hired", "declined", "withdrawn"]

ROLE_COLS = [
    "job_id", "ats", "company", "company_norm", "title", "title_norm", "department",
    "location", "is_remote", "seniority", "apply_url", "jd_text", "jd_hash",
    "first_seen", "last_seen", "s_stability", "s_culture", "s_depth", "s_wlb",
    "weighted_score", "verdict", "why", "caution", "scored_at", "updated_at",
]

JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS roles (
  job_id TEXT PRIMARY KEY, ats TEXT, company TEXT, company_norm TEXT,
  title TEXT, title_norm TEXT, department TEXT, location TEXT,
  is_remote INTEGER DEFAULT 0, seniority TEXT, apply_url TEXT,
  jd_text TEXT, jd_hash TEXT, first_seen TEXT, last_seen TEXT,
  s_stability REAL, s_culture REAL, s_depth REAL, s_wlb REAL,
  weighted_score REAL, verdict TEXT, why TEXT, caution TEXT,
  scored_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_roles_sig ON roles(company_norm, title_norm);
CREATE TABLE IF NOT EXISTS scan_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, mode TEXT,
  n_fetched INTEGER, n_kept INTEGER, n_new INTEGER, n_errors INTEGER, details_json TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

USER_SCHEMA = """
CREATE TABLE IF NOT EXISTS role_state (
  job_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'new',
  status_updated_at TEXT, applied_date TEXT, applied_with_resume TEXT,
  notes TEXT DEFAULT '',
  tailor_status TEXT DEFAULT 'none', prompt_path TEXT, tailored_resume_path TEXT,
  -- outcome half: what happened after the application went in (see STAGES / OUTCOMES)
  stage TEXT, outcome TEXT, outcome_note TEXT DEFAULT '',
  first_response_at TEXT, last_contact_at TEXT
);
-- One row per follow-up nudge actually drafted, so the "has this gone quiet" maths and the
-- two-per-application ceiling survive a restart. Draft-only: sent_at is stamped by hand.
CREATE TABLE IF NOT EXISTS followups (
  job_id TEXT, seq INTEGER, drafted_at TEXT, sent_at TEXT,
  channel TEXT, to_addr TEXT, subject TEXT, body TEXT,
  PRIMARY KEY (job_id, seq)
);
CREATE TABLE IF NOT EXISTS applied_companies (
  name_norm TEXT PRIMARY KEY, display_name TEXT, added_at TEXT
);
CREATE TABLE IF NOT EXISTS company_batch (
  company_norm TEXT PRIMARY KEY, batch_date TEXT, job_ids TEXT
);
CREATE TABLE IF NOT EXISTS reply_queue (
  msg_id TEXT PRIMARY KEY,              -- email Message-ID, for dedup
  uid TEXT, from_addr TEXT, from_name TEXT, reply_to TEXT,
  subject TEXT, snippet TEXT,
  category TEXT,                        -- scheduling | assessment | recruiter | other
  asks_availability INTEGER DEFAULT 0,
  repliable INTEGER DEFAULT 1,          -- 0 when the sender is a no-reply relay
  draft TEXT, references_hdr TEXT, in_reply_to TEXT,
  status TEXT DEFAULT 'pending',        -- pending | sent | dismissed
  found_at TEXT, sent_at TEXT
);
"""


_QUEUE_COLS = [
    "msg_id", "uid", "from_addr", "from_name", "reply_to", "subject", "snippet",
    "category", "asks_availability", "repliable", "draft", "references_hdr",
    "in_reply_to", "status", "found_at", "sent_at",
]


def batch_get(conn, company_norm: str) -> dict | None:
    r = conn.execute("SELECT batch_date, job_ids FROM company_batch WHERE company_norm=?",
                     (company_norm,)).fetchone()
    if not r:
        return None
    return {"batch_date": r["batch_date"], "job_ids": set(json.loads(r["job_ids"] or "[]"))}


def batch_set(conn, company_norm: str, batch_date: str, job_ids) -> None:
    conn.execute("INSERT OR REPLACE INTO company_batch (company_norm, batch_date, job_ids) VALUES (?,?,?)",
                 (company_norm, batch_date, json.dumps(sorted(job_ids))))
    conn.commit()


def queue_has(conn, msg_id: str) -> bool:
    # stored msg_ids are stripped on insert (queue_add), so an exact match on the stripped
    # lookup key is reliable — SQLite's TRIM() doesn't remove newlines, so don't rely on it.
    return conn.execute("SELECT 1 FROM reply_queue WHERE msg_id=?",
                        ((msg_id or "").strip(),)).fetchone() is not None


def queue_closed_rows(conn) -> list[dict]:
    """Conversations the user has finished with (sent or dismissed) — used to suppress
    re-drafting a NEW message that belongs to the same conversation (same sender+subject)."""
    return [dict(r) for r in conn.execute(
        "SELECT from_addr, subject FROM reply_queue WHERE status IN ('sent','dismissed')")]


def queue_add(conn, item: dict) -> None:
    item = {**item, "msg_id": (item.get("msg_id") or "").strip()}
    row = {k: item.get(k) for k in _QUEUE_COLS}
    conn.execute(
        f"INSERT OR IGNORE INTO reply_queue ({', '.join(_QUEUE_COLS)}) "
        f"VALUES ({', '.join('?' for _ in _QUEUE_COLS)})",
        [row[k] for k in _QUEUE_COLS])
    conn.commit()


def queue_list(conn, status: str = "pending") -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM reply_queue WHERE status=? ORDER BY found_at DESC", (status,)).fetchall()
    return [dict(r) for r in rows]


def queue_get(conn, msg_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM reply_queue WHERE msg_id=?", (msg_id,)).fetchone()
    return dict(r) if r else None


def queue_mark(conn, msg_id: str, status: str, sent_at: str | None = None) -> None:
    conn.execute("UPDATE reply_queue SET status=?, sent_at=? WHERE msg_id=?",
                 (status, sent_at, msg_id))
    conn.commit()


def queue_count_pending(conn) -> int:
    return conn.execute("SELECT COUNT(*) n FROM reply_queue WHERE status='pending'").fetchone()["n"]


def la_now_iso() -> str:
    return datetime.datetime.now(LA).isoformat(timespec="seconds")


def la_today() -> str:
    return datetime.datetime.now(LA).date().isoformat()


def _connect(path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def connect_jobs(cfg: dict) -> sqlite3.Connection:
    conn = _connect(db_path(cfg))
    conn.executescript(JOBS_SCHEMA)
    return conn


# Columns added to role_state after the table already existed in the wild. CREATE TABLE IF NOT
# EXISTS is a no-op on an existing table, so they have to be ALTERed in. Additive only —
# nothing here ever drops or rewrites a column, so an old DB keeps every status she has set.
_USER_MIGRATIONS = [
    ("role_state", "stage", "TEXT"),
    ("role_state", "outcome", "TEXT"),
    ("role_state", "outcome_note", "TEXT DEFAULT ''"),
    ("role_state", "first_response_at", "TEXT"),
    ("role_state", "last_contact_at", "TEXT"),
]


def _migrate_user(conn: sqlite3.Connection) -> None:
    for table, col, decl in _USER_MIGRATIONS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def connect_user(cfg: dict) -> sqlite3.Connection:
    conn = _connect(user_db_path(cfg))
    conn.executescript(USER_SCHEMA)
    _migrate_user(conn)
    return conn


def connect_view(cfg: dict) -> sqlite3.Connection:
    """User DB read-write, jobs DB attached for joins (queried read-only by code)."""
    connect_jobs(cfg).close()  # bootstrap schema if the scanner has never run
    conn = connect_user(cfg)
    conn.execute("ATTACH DATABASE ? AS jobs", (str(db_path(cfg)),))
    return conn


# ---------- scanner side (jobs db) ----------

def get_existing(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["job_id"]: r for r in conn.execute("SELECT * FROM roles")}


def upsert_roles(conn: sqlite3.Connection, rows: list[dict]) -> None:
    placeholders = ", ".join(f":{c}" for c in ROLE_COLS)
    updates = ", ".join(
        f"{c}=excluded.{c}" for c in ROLE_COLS if c not in ("job_id", "first_seen")
    )
    sql = (
        f"INSERT INTO roles ({', '.join(ROLE_COLS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(job_id) DO UPDATE SET {updates}"
    )
    conn.executemany(sql, rows)
    conn.commit()


def record_run(conn, *, mode, n_fetched, n_kept, n_new, n_errors, details) -> None:
    conn.execute(
        "INSERT INTO scan_runs (run_at, mode, n_fetched, n_kept, n_new, n_errors, details_json)"
        " VALUES (?,?,?,?,?,?,?)",
        (la_now_iso(), mode, n_fetched, n_kept, n_new, n_errors, json.dumps(details)),
    )
    conn.commit()


def last_run(conn) -> dict | None:
    r = conn.execute(
        "SELECT * FROM jobs.scan_runs ORDER BY run_id DESC LIMIT 1"
        if _has_attached_jobs(conn)
        else "SELECT * FROM scan_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    d["details"] = json.loads(d.pop("details_json") or "{}")
    return d


def _has_attached_jobs(conn) -> bool:
    return any(r["name"] == "jobs" for r in conn.execute("PRAGMA database_list"))


def meta_get(conn, key: str) -> str | None:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def meta_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


# ---------- user side (user_state db) ----------

# EVERY column of role_state must be listed here: upsert_state writes with INSERT OR REPLACE,
# so a column left out would be silently reset to NULL on the next status change.
_STATE_COLS = [
    "job_id", "status", "status_updated_at", "applied_date", "applied_with_resume",
    "notes", "tailor_status", "prompt_path", "tailored_resume_path",
    "stage", "outcome", "outcome_note", "first_response_at", "last_contact_at",
]


def upsert_state(conn, job_id: str, **fields) -> dict:
    row = conn.execute("SELECT * FROM role_state WHERE job_id=?", (job_id,)).fetchone()
    data = dict(row) if row else {c: None for c in _STATE_COLS}
    data.update({"job_id": job_id})
    if data.get("status") is None:
        data["status"] = "new"
    if data.get("notes") is None:
        data["notes"] = ""
    if data.get("tailor_status") is None:
        data["tailor_status"] = "none"
    data.update(fields)
    if "status" in fields:
        data["status_updated_at"] = la_now_iso()
    conn.execute(
        f"INSERT OR REPLACE INTO role_state ({', '.join(_STATE_COLS)}) "
        f"VALUES ({', '.join(':' + c for c in _STATE_COLS)})",
        data,
    )
    conn.commit()
    return data


def set_status(conn, job_id: str, status: str, resume_variant: str | None = None) -> dict:
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}")
    fields: dict = {"status": status}
    if status == "applied":
        fields["applied_date"] = la_today()
        fields["applied_with_resume"] = resume_variant or "base"
    return upsert_state(conn, job_id, **fields)


def set_notes(conn, job_id: str, notes: str) -> dict:
    return upsert_state(conn, job_id, notes=notes)


def record_signal(conn, job_id: str, *, stage: str | None = None, outcome: str | None = None,
                  note: str = "", when: str | None = None) -> dict:
    """Record something that happened to an application after it went out.

    Monotonic on purpose: `stage` only advances (a scheduling email arriving after an onsite
    must not demote the row back to 'responded'), and `first_response_at` is written once —
    it is the funnel's key measurement and a later contact must not overwrite it. Notes are
    appended with their date, never replaced, so re-running a scan can't erase history.
    """
    when = when or la_today()
    row = conn.execute("SELECT * FROM role_state WHERE job_id=?", (job_id,)).fetchone()
    cur = dict(row) if row else {}
    fields: dict = {"last_contact_at": when}

    if stage:
        if stage not in STAGE_RANK:
            raise ValueError(f"invalid stage {stage!r}")
        if STAGE_RANK[stage] > STAGE_RANK.get(cur.get("stage") or "applied", -1):
            fields["stage"] = stage
        if stage != "applied" and not cur.get("first_response_at"):
            fields["first_response_at"] = when      # write-once
        # A real reply means the row is no longer just "applied" in the review UI either.
        if stage in ("screen", "interview", "onsite") and cur.get("status") == "applied":
            fields["status"] = "interviewing"
    if outcome:
        if outcome not in OUTCOMES:
            raise ValueError(f"invalid outcome {outcome!r}")
        fields["outcome"] = outcome
        if outcome == "rejected" and (cur.get("status") or "new") != "rejected":
            fields["status"] = "rejected"
        if outcome in ("offer", "hired") and STAGE_RANK.get(cur.get("stage") or "", -1) < STAGE_RANK["offer"]:
            fields["stage"] = "offer"
        if outcome != "in_progress" and not cur.get("first_response_at") and outcome != "no_response":
            fields["first_response_at"] = when
    if note:
        prev = (cur.get("outcome_note") or "").strip()
        fields["outcome_note"] = f"{prev}\n{when}: {note}".strip()
    return upsert_state(conn, job_id, **fields)


# ---------- follow-ups (draft-only; nothing here sends) ----------

def followups_for(conn, job_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM followups WHERE job_id=? ORDER BY seq", (job_id,))]


def followup_counts(conn) -> dict[str, int]:
    return {r["job_id"]: r["n"] for r in conn.execute(
        "SELECT job_id, COUNT(*) n FROM followups GROUP BY job_id")}


def followup_add(conn, job_id: str, *, channel: str, to_addr: str | None,
                 subject: str, body: str) -> int:
    seq = (conn.execute("SELECT COALESCE(MAX(seq),0) m FROM followups WHERE job_id=?",
                        (job_id,)).fetchone()["m"]) + 1
    conn.execute(
        "INSERT INTO followups (job_id, seq, drafted_at, sent_at, channel, to_addr, subject, body)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (job_id, seq, la_today(), None, channel, to_addr, subject, body))
    conn.commit()
    return seq


def followup_mark_sent(conn, job_id: str, seq: int) -> None:
    conn.execute("UPDATE followups SET sent_at=? WHERE job_id=? AND seq=?",
                 (la_today(), job_id, seq))
    conn.commit()


def set_tailor(conn, job_id: str, **fields) -> dict:
    allowed = {"tailor_status", "prompt_path", "tailored_resume_path"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"set_tailor got unexpected fields: {bad}")
    return upsert_state(conn, job_id, **fields)


def list_applied_companies(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM applied_companies ORDER BY display_name"
    )]


def add_applied_company(conn, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO applied_companies (name_norm, display_name, added_at)"
        " VALUES (?,?,?)",
        (norm_key(name), name.strip(), la_now_iso()),
    )
    conn.commit()


def remove_applied_company(conn, name: str) -> None:
    conn.execute("DELETE FROM applied_companies WHERE name_norm=?", (norm_key(name),))
    conn.commit()


def seed_applied_companies(cfg: dict) -> None:
    names = cfg.get("applied_companies") or []
    if not names:
        return
    conn = connect_user(cfg)
    try:
        for n in names:
            add_applied_company(conn, str(n))
    finally:
        conn.close()


# ---------- the review view (join of both files) ----------

def fetch_view(
    conn: sqlite3.Connection,
    cfg: dict,
    *,
    status: str = "new",
    q: str = "",
    company: str = "",
    min_score: float = 0.0,
    verdict: str = "",
    seen: str = "all",
    remote: str = "",
    seniority: str = "",
    sort: str = "score",
) -> dict:
    rows = [dict(r) for r in conn.execute(
        "SELECT r.*, s.status AS own_status, s.notes, s.applied_date,"
        "       s.applied_with_resume, s.tailor_status, s.tailored_resume_path,"
        "       s.status_updated_at"
        " FROM jobs.roles r LEFT JOIN role_state s ON s.job_id = r.job_id"
    )]
    applied_cos = {r["name_norm"]: r["display_name"] for r in
                   conn.execute("SELECT * FROM applied_companies")}
    cooldown_cos = cfgmod.active_cooldowns(cfg)     # {norm_key: {until, reason}}
    sig_mode = cfg.get("signature_match", "badge")

    closed_by_sig: dict[tuple, dict] = {}
    for r in rows:
        if (r["own_status"] or "new") in CLOSED_STATUSES:
            closed_by_sig.setdefault((r["company_norm"], r["title_norm"]), r)

    today = la_today()
    week_ago = (datetime.date.fromisoformat(today) - datetime.timedelta(days=6)).isoformat()

    out = []
    counts = {"new": 0, "applied": 0, "interviewing": 0, "skip": 0, "rejected": 0}
    for r in rows:
        eff = r["own_status"] or "new"
        r["status"] = eff
        r["notes"] = r["notes"] or ""
        r["hidden_company"] = r["company_norm"] in applied_cos
        cd = cooldown_cos.get(r["company_norm"])
        r["cooldown"] = cd                          # {until, reason} or None
        r["badge_ref"] = None
        hidden_sig = False
        if eff == "new" and sig_mode != "off":
            ref = closed_by_sig.get((r["company_norm"], r["title_norm"]))
            if ref and ref["job_id"] != r["job_id"]:
                if sig_mode == "hide":
                    hidden_sig = True
                else:
                    r["badge_ref"] = {
                        "job_id": ref["job_id"],
                        "status": ref["own_status"],
                        "date": ref["applied_date"] or (ref["status_updated_at"] or "")[:10],
                    }
        r["hidden_sig"] = hidden_sig

        # verdict 'drop' (over the years ceiling / non-US) is a dead-end — keep it out of the
        # review queue entirely (it still shows under 'all'), and never auto-tailored.
        in_review = (eff == "new" and not r["hidden_company"] and not hidden_sig and not cd
                     and r["verdict"] != "drop")
        r["in_review"] = in_review
        if in_review:
            counts["new"] += 1
        elif eff in counts and eff != "new":
            counts[eff] += 1

        # --- status / visibility filter ---
        if status == "all":
            pass
        elif status == "new":
            if not in_review:
                continue
        elif eff != status:
            continue

        # --- field filters ---
        if company and r["company"] != company:
            continue
        if min_score and (r["weighted_score"] or 0) < float(min_score):
            continue
        if verdict and r["verdict"] != verdict:
            continue
        if seniority and r["seniority"] != seniority:
            continue
        if remote == "remote" and not r["is_remote"]:
            continue
        if seen == "today" and r["first_seen"] != today:
            continue
        if seen == "week" and (r["first_seen"] or "") < week_ago:
            continue
        if seen not in ("all", "today", "week") and seen and r["first_seen"] != seen:
            continue
        if q:
            hay = f'{r["company"]} {r["title"]}'.lower()
            if q.lower() not in hay:
                continue
        out.append(r)

    if sort == "company":
        out.sort(key=lambda r: (r["company"], -(r["weighted_score"] or 0)))
    elif sort == "first_seen":
        out.sort(key=lambda r: (r["first_seen"] or "", r["weighted_score"] or 0), reverse=True)
    else:
        out.sort(key=lambda r: (r["weighted_score"] or 0, r["first_seen"] or ""), reverse=True)

    # --- per-company cap: in the review tab, show at most `max` roles per company (top by
    # score), frozen for `window_days` so you work through one batch before the next surfaces.
    # A company over the cap records a batch date; overflow is held (still visible under 'all'). ---
    cap_cfg = cfg.get("per_company_cap") or {}
    cap_max = int(cap_cfg.get("max", 0) or 0)
    cap_info: dict[str, dict] = {}
    if status == "new" and cap_max > 0:
        window = int(cap_cfg.get("window_days", 7))
        stale = (datetime.date.fromisoformat(today) - datetime.timedelta(days=window)).isoformat()
        review_by_co: dict[str, list] = {}
        for r in rows:
            if r.get("in_review"):
                review_by_co.setdefault(r["company_norm"], []).append(r)
        active: dict[str, set] = {}
        for cn, rs in review_by_co.items():
            if len(rs) <= cap_max:
                active[cn] = {x["job_id"] for x in rs}
                continue
            rec = batch_get(conn, cn)
            still_new = {x["job_id"] for x in rs}
            if rec and (rec["batch_date"] or "") >= stale:
                # frozen for the whole window — even if you clear all 15, the next batch waits
                # until `window_days` pass (your "re-search next week"). Cleared ones just drop out.
                active[cn] = rec["job_ids"] & still_new
            else:
                top = sorted(rs, key=lambda x: -(x["weighted_score"] or 0))[:cap_max]
                ids = {x["job_id"] for x in top}
                batch_set(conn, cn, today, ids)
                active[cn] = ids
                rec = {"batch_date": today}
            held = len(rs) - len(active[cn])
            if held > 0:
                cap_info[rs[0]["company"]] = {"shown": len(active[cn]), "held": held,
                                              "since": rec["batch_date"], "window_days": window}
        out = [r for r in out if r["company_norm"] not in active or r["job_id"] in active[r["company_norm"]]]
        counts["new"] = sum(len(v) for v in active.values())

    companies = sorted({r["company"] for r in rows})
    return {
        "rows": out,
        "counts": counts,
        "total_roles": len(rows),
        "companies": companies,
        "applied_companies": sorted(applied_cos.values(), key=str.lower),
        "cap_info": cap_info,
        "run": last_run(conn),
        "today": today,
    }


def lookup_role_by_ids(conn: sqlite3.Connection, candidates: list[str]) -> dict | None:
    """Find a role whose raw ATS id (last segment of job_id) matches any candidate
    pulled from an application-page URL. Prefer one that already has a tailored PDF."""
    hits = []
    for cand in candidates:
        for r in conn.execute(
            "SELECT r.*, s.status AS own_status, s.tailor_status, s.tailored_resume_path"
            " FROM jobs.roles r LEFT JOIN role_state s ON s.job_id = r.job_id"
            " WHERE r.job_id LIKE ?",
            (f"%:{cand}",),
        ):
            hits.append(dict(r))
    if not hits:
        return None
    hits.sort(key=lambda d: 0 if d.get("tailored_resume_path") else 1)
    d = hits[0]
    d["status"] = d.pop("own_status") or "new"
    return d


def get_role(conn: sqlite3.Connection, job_id: str) -> dict | None:
    r = conn.execute(
        "SELECT r.*, s.status AS own_status, s.notes, s.applied_date,"
        "       s.applied_with_resume, s.tailor_status, s.prompt_path,"
        "       s.tailored_resume_path"
        " FROM jobs.roles r LEFT JOIN role_state s ON s.job_id = r.job_id"
        " WHERE r.job_id = ?",
        (job_id,),
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    d["status"] = d.pop("own_status") or "new"
    return d
