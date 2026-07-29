"""Two SQLite files, one writer each:
  data/jobscan.db     — written ONLY by the scanner (roles, scan_runs, meta)
  data/user_state.db  — written ONLY by the UI/tailoring (role_state, applied_companies)
The scanner cannot clobber your status/notes because it never opens that file."""
from __future__ import annotations

import datetime
import json
import re
import sqlite3
from zoneinfo import ZoneInfo

from . import config as cfgmod
from . import scoring
from .config import db_path, norm_key, user_db_path

LA = ZoneInfo("America/Los_Angeles")
STATUSES = ["new", "applied", "interviewing", "skip", "rejected"]

# The tailored PDF's résumé↔JD fit score (0-100, from resume_score.py) is baked into the
# filename tail as " - NN.pdf" at ingest time. Parse it back out for display — this is a
# SEPARATE number from the role's weighted_score (which is preference fit, not résumé fit).
_RESUME_SCORE_RE = re.compile(r" - (\d{1,3})\.pdf$", re.IGNORECASE)


def resume_score_from_path(path: str | None) -> int | None:
    """Return the résumé fit score encoded in a tailored PDF filename, or None."""
    if not path:
        return None
    m = _RESUME_SCORE_RE.search(str(path))
    return int(m.group(1)) if m else None

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
    "posted_at", "first_seen", "last_seen", "s_stability", "s_culture", "s_depth", "s_wlb",
    "weighted_score", "verdict", "why", "caution", "scored_at", "updated_at",
]

JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS roles (
  job_id TEXT PRIMARY KEY, ats TEXT, company TEXT, company_norm TEXT,
  title TEXT, title_norm TEXT, department TEXT, location TEXT,
  is_remote INTEGER DEFAULT 0, seniority TEXT, apply_url TEXT,
  jd_text TEXT, jd_hash TEXT, posted_at TEXT, first_seen TEXT, last_seen TEXT,
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
-- Interview reviews (面试复盘), indexed INTO the pipeline. The markdown files under
-- 面试复盘/ stay the system of record (the interview-review skill writes them, the atoms
-- card cron watches them); these tables are the join layer: which jobscan role each
-- interview belongs to, plus its questions/action items surfaced in the UI. Re-ingest is
-- idempotent (items replaced per marker; a manually-set job_id link always survives).
CREATE TABLE IF NOT EXISTS interviews (
  marker_id TEXT PRIMARY KEY,          -- the atoms-source id ("2026-07-06-amazon"), or derived
  job_id TEXT,                         -- linked jobscan role; NULL until matched
  company TEXT, happened_on TEXT, round_type TEXT,
  sources TEXT,                        -- json {file: heading} — where the sections live
  transcript_url TEXT,                 -- Otter share link, from 面试复盘/README.md
  review_path TEXT,                    -- per-interview review/transcript md, relative to 复盘 dir
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS interview_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  marker_id TEXT NOT NULL,
  kind TEXT NOT NULL,                  -- action | tech_q | ai_q
  text TEXT NOT NULL,
  done INTEGER DEFAULT 0,              -- actions only, read from the checkbox in the md
  src_file TEXT
);
CREATE INDEX IF NOT EXISTS idx_interview_items ON interview_items(marker_id);
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
    # 60s not 5s: launchd wake-from-sleep coalescing can fire the 08:20 linkedin job and
    # the 09:00 scan together, and a full-DB rescore holds a write txn far longer than 5s.
    # WAL keeps readers unblocked either way; a colliding writer should wait, not crash.
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def connect_jobs(cfg: dict) -> sqlite3.Connection:
    conn = _connect(db_path(cfg))
    conn.executescript(JOBS_SCHEMA)
    _migrate(conn, _JOBS_MIGRATIONS)
    return conn


# Columns added to a table after it already existed in the wild. CREATE TABLE IF NOT EXISTS is
# a no-op on an existing table, so they have to be ALTERed in. Additive only — nothing here
# ever drops or rewrites a column, so an old DB keeps every status she has set.
_USER_MIGRATIONS = [
    ("role_state", "stage", "TEXT"),
    ("role_state", "outcome", "TEXT"),
    ("role_state", "outcome_note", "TEXT DEFAULT ''"),
    ("role_state", "first_response_at", "TEXT"),
    ("role_state", "last_contact_at", "TEXT"),
    ("interviews", "transcript_url", "TEXT"),
    ("interviews", "review_path", "TEXT"),
]
_JOBS_MIGRATIONS = [
    ("roles", "posted_at", "TEXT"),
]


def _migrate(conn: sqlite3.Connection, migrations: list[tuple[str, str, str]]) -> None:
    for table, col, decl in migrations:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def _migrate_user(conn: sqlite3.Connection) -> None:
    _migrate(conn, _USER_MIGRATIONS)


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


# ---------- interviews (复盘 index; md files stay the source of truth) ----------

def interview_upsert(conn, *, marker_id: str, company: str, happened_on: str,
                     round_type: str, sources: str, job_id: str | None = None,
                     transcript_url: str | None = None, review_path: str | None = None) -> None:
    """Insert or refresh one interview. A job_id already set (auto-matched earlier or linked
    by hand in the UI) is never cleared by a re-ingest — only filled when currently NULL.
    transcript_url / review_path: a value parsed from the files wins; None keeps what's there."""
    now = la_now_iso()
    row = conn.execute("SELECT job_id FROM interviews WHERE marker_id=?", (marker_id,)).fetchone()
    if row:
        conn.execute(
            "UPDATE interviews SET company=?, happened_on=?, round_type=?, sources=?,"
            " job_id=COALESCE(job_id, ?), transcript_url=COALESCE(?, transcript_url),"
            " review_path=COALESCE(?, review_path), updated_at=? WHERE marker_id=?",
            (company, happened_on, round_type, sources, job_id,
             transcript_url, review_path, now, marker_id))
    else:
        conn.execute(
            "INSERT INTO interviews (marker_id, job_id, company, happened_on, round_type,"
            " sources, transcript_url, review_path, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (marker_id, job_id, company, happened_on, round_type, sources,
             transcript_url, review_path, now, now))
    conn.commit()


def interview_replace_items(conn, marker_id: str, items: list[dict]) -> None:
    conn.execute("DELETE FROM interview_items WHERE marker_id=?", (marker_id,))
    conn.executemany(
        "INSERT INTO interview_items (marker_id, kind, text, done, src_file)"
        " VALUES (?,?,?,?,?)",
        [(marker_id, i["kind"], i["text"], int(i.get("done", 0)), i.get("src_file", ""))
         for i in items])
    conn.commit()


def interview_link(conn, marker_id: str, job_id: str | None) -> None:
    conn.execute("UPDATE interviews SET job_id=?, updated_at=? WHERE marker_id=?",
                 (job_id, la_now_iso(), marker_id))
    conn.commit()


def interviews_overview(conn) -> list[dict]:
    """Every ingested interview + item counts + the linked role (when matched)."""
    rows = [dict(r) for r in conn.execute(
        "SELECT i.*,"
        " (SELECT COUNT(*) FROM interview_items x WHERE x.marker_id=i.marker_id AND x.kind='action') n_actions,"
        " (SELECT COUNT(*) FROM interview_items x WHERE x.marker_id=i.marker_id AND x.kind='action' AND x.done=1) n_done,"
        " (SELECT COUNT(*) FROM interview_items x WHERE x.marker_id=i.marker_id AND x.kind IN ('tech_q','ai_q')) n_questions,"
        " r.title AS role_title, r.company AS role_company, s.status AS role_status"
        " FROM interviews i"
        " LEFT JOIN jobs.roles r ON r.job_id = i.job_id"
        " LEFT JOIN role_state s ON s.job_id = i.job_id"
        " ORDER BY i.happened_on DESC, i.marker_id")]
    return rows


def interview_items_for(conn, marker_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM interview_items WHERE marker_id=? ORDER BY kind, id", (marker_id,))]


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
        r["resume_score"] = resume_score_from_path(r.get("tailored_resume_path"))
        # Recency boost, applied to the RANK only — weighted_score/verdict stay untouched so
        # they mean the same thing this week as last week. See scoring.freshness().
        # Hand-captured/manual roles get NO first_seen fallback: their first_seen is the
        # moment the owner clicked capture, not the posting date — only a real posted_at
        # (parsed from the JD) may earn them the boost.
        f = scoring.freshness(r.get("posted_at"), r.get("first_seen"), cfg, today=today,
                              allow_inferred=not r["job_id"].startswith(("cap:", "manual:")))
        r["fresh"] = f
        r["fresh_bonus"] = f["bonus"]
        r["rank_score"] = (r["weighted_score"] or 0) + f["bonus"]
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

    # --- diversify: a bounded EXPLORATION budget (owner's ask, 2026-07-25). The weights are
    # pure exploitation — culture 1.40 means low-culture companies never surface, so the funnel
    # can never test whether its priors are actually true for HER. Rule: a company with ZERO
    # applications and a low average role score gets +`bonus` on ONE role (its best), at most
    # `max_picks` companies at a time. Rank-only, like freshness — weighted_score stays honest
    # for the funnel. Self-retiring by design: the first application ends that company's
    # subsidy, so each bonus buys exactly one probe. Readout: funnel's culture-prior dimension.
    div_cfg = (cfg.get("scoring") or {}).get("diversify") or {}
    div_picks: list[dict] = []
    if div_cfg.get("enabled", True):
        applied_by_co = {r["company_norm"] for r in rows
                        if (r["own_status"] or "new") in ("applied", "interviewing")}
        co_scores: dict[str, list] = {}
        top_by_co: dict[str, dict] = {}
        for r in rows:
            if r["verdict"] != "drop" and r["weighted_score"] is not None:
                co_scores.setdefault(r["company_norm"], []).append(r["weighted_score"])
            if r.get("in_review") and (r["company_norm"] not in top_by_co
                                       or r["rank_score"] > top_by_co[r["company_norm"]]["rank_score"]):
                top_by_co[r["company_norm"]] = r
        mean_below = float(div_cfg.get("company_mean_below", 62))
        floor_ = float(div_cfg.get("min_role_score", 50))
        cands = []
        for cn, top in top_by_co.items():
            if cn in applied_by_co or cn not in co_scores:
                continue
            mean = sum(co_scores[cn]) / len(co_scores[cn])
            if mean >= mean_below or (top["weighted_score"] or 0) < floor_:
                continue
            cands.append((mean, top))
        # Worst-priced companies first — the owner's ask was the "culture 特别差" tier, and
        # that's also where a reply carries the most information about mispriced priors. A
        # strongest-wildcard-first order was tried and spent the whole budget on mildly-low
        # companies (Okta/Autodesk at prior ~3.0), which tests nothing. min_role_score is the
        # quality floor that keeps this from becoming applications she can't win.
        cands.sort(key=lambda t: t[0])
        bonus = float(div_cfg.get("bonus", 15))
        for mean, top in cands[:int(div_cfg.get("max_picks", 6))]:
            top["diversify"] = {"bonus": bonus, "company_mean": round(mean, 1)}
            top["rank_score"] += bonus
            div_picks.append({"company": top["company"], "job_id": top["job_id"],
                              "title": top["title"], "score": top["weighted_score"],
                              "company_mean": round(mean, 1)})

    if sort == "company":
        out.sort(key=lambda r: (r["company"], -r["rank_score"]))
    elif sort == "first_seen":
        out.sort(key=lambda r: (r["first_seen"] or "", r["rank_score"]), reverse=True)
    elif sort == "base_score":
        out.sort(key=lambda r: (r["weighted_score"] or 0, r["first_seen"] or ""), reverse=True)
    else:
        out.sort(key=lambda r: (r["rank_score"], r["first_seen"] or ""), reverse=True)

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
                top = sorted(rs, key=lambda x: -x["rank_score"])[:cap_max]
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
        "diversify_picks": div_picks,
        "run": last_run(conn),
        "today": today,
    }


def lookup_role_by_ids(conn: sqlite3.Connection, candidates: list[str]) -> dict | None:
    """Find a role whose raw ATS id (last segment of job_id) matches any candidate
    pulled from an application-page URL. Prefer one that already has a tailored PDF.

    Second pass: the candidate id appearing inside a stored APPLY_URL. Captured roles'
    job_ids are hashes (cap:…) that never contain the ATS id, but their apply_url does —
    Apple's apply flow (/app/en-us/apply/200673880-0836) and its details page
    (/en-us/details/200673880-0836/<slug>) share the req id, and that id is the only join."""
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
        for cand in candidates:
            if len(cand) < 6:
                continue                       # short tokens would match half the corpus
            # Path-delimited for medium ids; UUID-length ids (Deel's ?ats_id=<uuid>) are
            # globally unique enough to match anywhere in the URL, including after an '='.
            pat = f"%{cand}%" if len(cand) >= 12 else f"%/{cand}%"
            for r in conn.execute(
                "SELECT r.*, s.status AS own_status, s.tailor_status, s.tailored_resume_path"
                " FROM jobs.roles r LEFT JOIN role_state s ON s.job_id = r.job_id"
                " WHERE r.apply_url LIKE ?",
                (pat,),
            ):
                hits.append(dict(r))
    if not hits:
        return None
    hits.sort(key=lambda d: 0 if d.get("tailored_resume_path") else 1)
    d = hits[0]
    d["status"] = d.pop("own_status") or "new"
    return d


def _canon_apply_url(url: str) -> str:
    """Same normalization as manual.canonical_capture_url (kept local — manual imports store,
    so importing back would be a cycle): keep only pid= when present (Eightfold boards differ
    ONLY by pid), else drop the query; always drop the fragment and trailing slash."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    s = urlsplit((url or "").strip())
    q = dict(parse_qsl(s.query))
    query = urlencode({"pid": q["pid"]}) if "pid" in q else ""
    return urlunsplit((s.scheme, s.netloc, s.path.rstrip("/"), query, ""))


def lookup_role_by_apply_url(conn: sqlite3.Connection, urls: list[str]) -> dict | None:
    """Match the page the extension is on to a role by its stored apply_url.

    The id-based lookup only understands scanner-ATS URL shapes (gh_jid=, /jobs/<n>, pid=…).
    Captured roles (Tesla, Apple, any careers site) carry no such id — but their apply_url IS
    the page they were captured from, so the URL itself is the join key. Both sides are
    canonicalized, then: exact match, or the page is DEEPER than the stored URL at a '/'
    boundary (…/job/x/apply). Never the reverse — a list page is a prefix of every job's URL
    and must not resolve to one. Prefer a role with a tailored PDF, same as the id lookup."""
    cands = [c for c in dict.fromkeys(_canon_apply_url(u) for u in urls if u) if c]
    if not cands:
        return None
    stored = conn.execute(
        "SELECT job_id, apply_url FROM jobs.roles"
        " WHERE apply_url IS NOT NULL AND apply_url != ''").fetchall()
    hit_ids: list[str] = []
    for r in stored:
        sc = _canon_apply_url(r["apply_url"])
        if not sc:
            continue
        for u in cands:
            if u == sc or u.startswith(sc + "/"):
                hit_ids.append(r["job_id"])
                break
    if not hit_ids:
        return None
    hits = []
    for jid in hit_ids:
        r = conn.execute(
            "SELECT r.*, s.status AS own_status, s.tailor_status, s.tailored_resume_path"
            " FROM jobs.roles r LEFT JOIN role_state s ON s.job_id = r.job_id"
            " WHERE r.job_id = ?", (jid,)).fetchone()
        if r:
            hits.append(dict(r))
    if not hits:
        return None
    hits.sort(key=lambda d: (0 if d.get("tailored_resume_path") else 1,
                             -len(d.get("apply_url") or "")))   # longest (most specific) match
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
