"""Two SQLite files, one writer each:
  data/jobscan.db     — written ONLY by the scanner (roles, scan_runs, meta)
  data/user_state.db  — written ONLY by the UI/tailoring (role_state, applied_companies)
The scanner cannot clobber your status/notes because it never opens that file."""
from __future__ import annotations

import datetime
import json
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

from . import budget
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
  first_response_at TEXT, last_contact_at TEXT,
  -- How sure we are that THIS row is the application the reply was about. 'role' = the company
  -- had exactly one open application (or the subject named this exact title), so the match is
  -- unambiguous. 'company' = the company demonstrably replied, but she has N open reqs there and
  -- the mail names none of them; the response is real, the row is the most likely of N.
  -- Added 2026-08-10: outcomes.py used to commit ONLY the unambiguous case, so at Nvidia (39
  -- applications), Mastercard (38), Anthropic (19) and Airbnb (12) it wrote nothing at all — the
  -- write path failed precisely where she applies most, and the funnel reported a 1.1% response
  -- rate that was an artefact of the plumbing. Recording the company-level fact with the
  -- ambiguity ON THE ROW is the fix; silently picking one of 39 and calling it certain is not.
  response_confidence TEXT,
  -- Set on a LinkedIn shadow row (li:<id>) when the same physical posting also exists as a
  -- real ATS row (gh:/lv:/ab:/…): the value is that canonical job_id. LinkedIn discovery and
  -- the board scanner routinely both find the same job; the extension marks the ATS row
  -- applied (it resolves by the employer page's URL) while the UI shows the li: row — so the
  -- owner saw "applied didn't stick" (2026-08-01). Status changes cascade across the link,
  -- and funnel/budget count a linked pair as ONE application.
  duplicate_of TEXT
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
  -- Where this row came from. NULL/'review-md' = a 复盘 section she wrote (the system of record).
  -- 'mailbox' = inferred by tools/backfill_outcomes from a calendar invite or interview mail
  -- because no 复盘 exists yet (2026-08-10: the mailbox showed 19 companies at interview stage
  -- and this table held 4). An inferred row must stay visibly inferred; when she does write the
  -- 复盘, interview_ingest upserts the same marker_id and takes it over.
  source TEXT,
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
-- 回头看 Hindsight 的预测（用户动作 → 本库；机器信号在 ideas.db，写者纪律见 app/hindsight.py）。
-- mode='live' 新品封盘（当天封、不可改）；'backtest' 老题盲猜练手，开奖答案抄进 revealed_verdict。
CREATE TABLE IF NOT EXISTS idea_predictions (
  key TEXT NOT NULL, mode TEXT NOT NULL,
  predicted TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  revealed_verdict TEXT, revealed_at TEXT,
  PRIMARY KEY (key, mode)
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
-- The "I'm applying to THIS one right now" pins. A LinkedIn role's apply_url is the LinkedIn
-- page; clicking Apply there lands on the employer's own ATS, whose URL shares no id with our
-- li:<id> role — so the extension's URL lookup can never join them back (see
-- store.lookup_role_by_ids). Clicking ↗ in the UI records the intent here; the extension asks
-- for it only after its URL lookup fails, and only within a short TTL so a stale pin can't
-- attach yesterday's résumé to today's form.
--
-- One row PER ROLE, not a single global slot. The slot version lost every ↗ but the last, and
-- the owner's actual habit is to open a batch of tabs and click ↗ down the list — so by the
-- time a form was filled the slot held whatever was clicked last. That silently labelled a
-- Precisely posting with a Temporal role (2026-07-31) and would have attached Temporal's
-- résumé. company_norm is denormalised in on purpose: the extension has to pick the pin that
-- matches the page it is on, and that decision must not need a join back to jobs.roles.
CREATE TABLE IF NOT EXISTS apply_pins (
  job_id TEXT PRIMARY KEY,
  company_norm TEXT,
  pinned_at TEXT NOT NULL
);

-- 公司进度页（/companies）的人工改写。机器从邮件读出的只是提议；这里存人的拍板：
-- closed = 手动结案（归入"已挂"，比如 CipherData 是我拒的他们，邮件里读不出来）
-- hidden = 从看板消失（误认的公司、不想再看的）
-- booked = 已约上面试（约面藏在邮件正文/日历里、标题正则读不出来时，人来标）
-- acked  = 已处理（在邮件之外回过了：平台里回的、电话里说的。等价于"当天发过一封信"，
--          之后再来新邮件，欠账自然复活）
-- 一家一行，撤销即删行。
CREATE TABLE IF NOT EXISTS company_overrides (
  company TEXT PRIMARY KEY,
  action TEXT NOT NULL CHECK(action IN ('closed','hidden','booked','acked')),
  note TEXT DEFAULT '',
  set_at TEXT NOT NULL
);

-- 手动 cooldown（owner 2026-09-22 拍板）：在表单上看到「这家不让投了」（ATS 限次数 / 一次只能
-- 一份在跑），在 review 页或 /companies 看板点一下就进这张表。效果与 config.yaml `cooldowns:`
-- 完全一样（扫描跳过这家 + review 隐藏它的岗位），只是不用改 YAML；until < today 自动失效，
-- 解除即删行。一家一行。扫描器只读这张表（user_state.db 的唯一写者是 UI）。
CREATE TABLE IF NOT EXISTS company_cooldowns (
  company_norm TEXT PRIMARY KEY,
  company TEXT NOT NULL,
  until TEXT NOT NULL,
  reason TEXT DEFAULT '',
  set_at TEXT NOT NULL
);

-- 投递节奏（owner 2026-09-28，app/pace.py）：一行设置 + 每天的工作表。
-- pace_settings：当前档位（sprint/normal/slow/pause/custom）、自动恢复日期、自定义的两个数。
-- pace_daily：kind='shown' = 当天冻结的 review 名单（每家一个岗位，pos 是顺序）；
--             kind='tailored' = 当天 autotailor 已排进批次的岗位（算每日简历额度）；
--             kind='filled' = 哨兵行，记「今天已经按 N 家找过一轮」，免得队列不足 N 家时每次刷新都重算。
CREATE TABLE IF NOT EXISTS pace_settings (
  id INTEGER PRIMARY KEY CHECK(id = 1),
  mode TEXT NOT NULL,
  until TEXT,
  set_at TEXT NOT NULL,
  companies_per_day INTEGER,
  tailor_per_day INTEGER
);
CREATE TABLE IF NOT EXISTS pace_daily (
  day TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('shown','tailored','filled')),
  job_id TEXT NOT NULL,
  company_norm TEXT,
  pos INTEGER NOT NULL DEFAULT 0,
  at TEXT NOT NULL,
  PRIMARY KEY (day, kind, job_id)
);

-- Outreach drafts（owner 2026-09-23）：给招聘联系人的自荐信草稿，一人一行（按邮箱）。
-- 生成 → 她在 /outreach 页改 → 存进 Gmail 草稿箱 → 她自己在 Gmail 里发。这里只记草稿和状态。
-- status: draft（已生成）| saved（已进 Gmail 草稿箱）| dismissed（不发）| error（生成失败）
CREATE TABLE IF NOT EXISTS outreach_drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact_email TEXT NOT NULL UNIQUE,
  contact_name TEXT DEFAULT '',
  company TEXT NOT NULL,
  source TEXT DEFAULT '',
  subject TEXT DEFAULT '',
  body TEXT DEFAULT '',
  roles_json TEXT DEFAULT '[]',
  resume_path TEXT,
  status TEXT NOT NULL DEFAULT 'draft',
  error TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  saved_at TEXT
);
"""


_QUEUE_COLS = [
    "msg_id", "uid", "from_addr", "from_name", "reply_to", "subject", "snippet",
    "category", "asks_availability", "repliable", "draft", "references_hdr",
    "in_reply_to", "status", "found_at", "sent_at",
]


def company_overrides(conn) -> dict[str, dict]:
    """{公司名: {action, note, set_at}} — /companies 看板的人工改写。"""
    return {r["company"]: dict(r) for r in conn.execute("SELECT * FROM company_overrides")}


def set_company_override(conn, company: str, action: str | None, note: str = "") -> None:
    """action=None 即撤销（删行）。"""
    if not company:
        return
    if action is None:
        conn.execute("DELETE FROM company_overrides WHERE company=?", (company,))
    else:
        conn.execute(
            "INSERT INTO company_overrides(company, action, note, set_at) VALUES(?,?,?,?)"
            " ON CONFLICT(company) DO UPDATE SET action=excluded.action,"
            "   note=excluded.note, set_at=excluded.set_at",
            (company, action, note,
             datetime.datetime.now(LA).isoformat(timespec="seconds")))
    conn.commit()


def manual_cooldowns(conn, today: str | None = None) -> dict[str, dict]:
    """{norm_key: {company, until, reason, set_at, source:'manual'}} — UI-set cooldowns that
    have not expired (`until` is inclusive)."""
    today = today or la_today()
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT * FROM company_cooldowns"):
        if (r["until"] or "") >= today:
            out[r["company_norm"]] = {"company": r["company"], "until": r["until"],
                                      "reason": r["reason"] or "", "set_at": r["set_at"],
                                      "source": "manual"}
    return out


def set_company_cooldown(conn, company: str, days: int | None = None,
                         until: str | None = None, reason: str = "") -> dict:
    """Put `company` on cooldown until `until` (ISO date, inclusive) or `days` from today.
    Same company again = overwrite (the later decision wins). Returns the stored row."""
    company = (company or "").strip()
    if not company:
        raise ValueError("empty company")
    today = datetime.date.fromisoformat(la_today())
    if until:
        u = datetime.date.fromisoformat(str(until)[:10])   # garbage → ValueError → 422 upstream
    else:
        d = 30 if days is None else int(days)      # 0 is a value, not "unset" — it must fail below
        if not 1 <= d <= 365:
            raise ValueError("days must be between 1 and 365")
        u = today + datetime.timedelta(days=d)
    if u < today:
        raise ValueError("until is in the past")
    cn = cfgmod.norm_key(company)
    row = {"company_norm": cn, "company": company, "until": u.isoformat(),
           "reason": (reason or "").strip(), "set_at": la_now_iso()}
    conn.execute(
        "INSERT INTO company_cooldowns(company_norm, company, until, reason, set_at)"
        " VALUES(?,?,?,?,?) ON CONFLICT(company_norm) DO UPDATE SET company=excluded.company,"
        "   until=excluded.until, reason=excluded.reason, set_at=excluded.set_at",
        (cn, company, row["until"], row["reason"], row["set_at"]))
    conn.commit()
    return {**row, "source": "manual"}


def clear_company_cooldown(conn, company: str) -> bool:
    cur = conn.execute("DELETE FROM company_cooldowns WHERE company_norm=?",
                       (cfgmod.norm_key(company),))
    conn.commit()
    return cur.rowcount > 0


def _config_company_name(cfg: dict, cn: str) -> str:
    """Display name for a config cooldown: the scan list's spelling, else the `cooldowns:` key
    as she wrote it, else the norm key itself."""
    for c in cfg.get("companies") or []:
        if cfgmod.norm_key(c.get("name", "")) == cn:
            return c["name"]
    for k in cfg.get("cooldowns") or {}:
        if cfgmod.norm_key(k) == cn:
            return k
    return cn


def _merge_cooldowns(cfg: dict, manual: dict[str, dict], today: str) -> dict[str, dict]:
    out = {cn: {**cd, "source": "config", "company": _config_company_name(cfg, cn)}
           for cn, cd in cfgmod.active_cooldowns(cfg, today).items()}
    for cn, cd in manual.items():
        if cn not in out or cd["until"] > out[cn]["until"]:   # both sides → the later date wins
            out[cn] = cd
    return out


def active_cooldowns(conn, cfg: dict, today: str | None = None) -> dict[str, dict]:
    """config.yaml `cooldowns:` ∪ UI-set cooldowns (company_cooldowns), keyed by norm_key;
    each entry carries until / reason / source ('config' | 'manual') / company. The UI and
    discovery use this; the scanner uses active_cooldowns_readonly (it must not write user db)."""
    today = today or la_today()
    return _merge_cooldowns(cfg, manual_cooldowns(conn, today), today)


def active_cooldowns_readonly(cfg: dict, today: str | None = None) -> dict[str, dict]:
    """Scanner side of active_cooldowns: user_state.db opened READ-ONLY and never created —
    it has exactly one writer (the UI). No file, no table, or a locked file → config only,
    with a line on stderr so a silent fallback never masquerades as 'no manual cooldowns'."""
    today = today or la_today()
    p = user_db_path(cfg)
    if not p.exists():
        return _merge_cooldowns(cfg, {}, today)
    try:
        ro = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        try:
            manual = manual_cooldowns(ro, today)
        finally:
            ro.close()
    except sqlite3.Error as e:
        print(f"cooldowns: could not read manual cooldowns from {p.name} ({e}); config only",
              file=sys.stderr)
        manual = {}
    return _merge_cooldowns(cfg, manual, today)


# ---------- outreach drafts ----------

def outreach_list(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM outreach_drafts ORDER BY updated_at DESC")]


def outreach_get(conn, draft_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM outreach_drafts WHERE id=?", (draft_id,)).fetchone()
    return dict(r) if r else None


def outreach_upsert(conn, rec: dict) -> dict:
    """Create or replace the draft for rec['contact_email'] (one live draft per contact)."""
    now = la_now_iso()
    conn.execute(
        "INSERT INTO outreach_drafts(contact_email, contact_name, company, source, subject, body,"
        " roles_json, resume_path, status, error, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(contact_email) DO UPDATE SET contact_name=excluded.contact_name,"
        "   company=excluded.company, source=excluded.source, subject=excluded.subject,"
        "   body=excluded.body, roles_json=excluded.roles_json, resume_path=excluded.resume_path,"
        "   status=excluded.status, error=excluded.error, updated_at=excluded.updated_at, saved_at=NULL",
        (rec["contact_email"].lower(), rec.get("contact_name", ""), rec["company"], rec.get("source", ""),
         rec.get("subject", ""), rec.get("body", ""), json.dumps(rec.get("roles") or [], ensure_ascii=False),
         rec.get("resume_path"), rec.get("status", "draft"), rec.get("error", ""), now, now))
    conn.commit()
    r = conn.execute("SELECT * FROM outreach_drafts WHERE contact_email=?", (rec["contact_email"].lower(),)).fetchone()
    return dict(r)


def outreach_update(conn, draft_id: int, **fields) -> dict | None:
    allowed = {"subject", "body", "status", "error", "saved_at", "resume_path"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return outreach_get(conn, draft_id)
    sets["updated_at"] = la_now_iso()
    conn.execute("UPDATE outreach_drafts SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?",
                 (*sets.values(), draft_id))
    conn.commit()
    return outreach_get(conn, draft_id)


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
    ("role_state", "duplicate_of", "TEXT"),
    ("role_state", "response_confidence", "TEXT"),
    ("interviews", "source", "TEXT"),
    # 投递身份（apply profile）：这个岗位用哪张“名片”投 — NULL/'casey' = 默认名片，
    # 'alex' = 法名名片（config.yaml apply_profiles 里定义）。岗位级、不锁公司（owner
    # 2026-09-08 拍板：同公司不同岗允许不同身份）。
    ("role_state", "apply_profile", "TEXT"),
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
    # company_overrides 的 CHECK 约束后来加了 'acked'。CHECK 烤在建表语句里，ALTER 改不了，
    # 检测到旧约束就搬家重建（保留已有行）。
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table'"
                       " AND name='company_overrides'").fetchone()
    if row and "'acked'" not in (row["sql"] or ""):
        conn.execute("ALTER TABLE company_overrides RENAME TO _company_overrides_old")
        conn.executescript(USER_SCHEMA)
        conn.execute("INSERT OR IGNORE INTO company_overrides"
                     " SELECT * FROM _company_overrides_old")
        conn.execute("DROP TABLE _company_overrides_old")
        conn.commit()
    # apply_pin (single row, id=1) → apply_pins (one row per role). Not an additive column, so
    # _migrate can't express it. Nothing is carried over and nothing is lost that matters: a
    # pin is intent that expires in 45 minutes, and the replacement is written by the next ↗.
    conn.execute("DROP TABLE IF EXISTS apply_pin")
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
    "duplicate_of",     # missing here would silently NULL it on every upsert_state
    "response_confidence",
    "apply_profile",    # which identity card this role is applied with (None = default)
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


def find_twin(conn, job_id: str) -> str | None:
    """The other row of the same physical posting, or None.

    A LinkedIn shadow row (li:) and a scanned ATS row are twins when they share company_norm
    and title_norm EXACTLY and the match is unambiguous (exactly one candidate — a company
    posting the same title in three locations gives three candidates and no safe answer).
    Containment matching is deliberately NOT used here: "Software Engineer" ⊂ "Senior
    Software Engineer" would cascade an application onto a job that was never applied to."""
    me = conn.execute(
        "SELECT job_id, company_norm, title_norm FROM jobs.roles WHERE job_id=?",
        (job_id,)).fetchone()
    if not me or not me["company_norm"] or not me["title_norm"]:
        return None
    i_am_shadow = job_id.startswith("li:")
    # Compare title_norm with spaces stripped: the LinkedIn ingest and the board scanner
    # normalised differently at different times ("fullstackengineer" vs "fullstack engineer"
    # — the REAL Atticus pair), so byte-equality silently missed most true twins. Stripping
    # spaces is still whole-string equality — it cannot introduce the containment matches
    # this function refuses.
    cands = conn.execute(
        "SELECT job_id FROM jobs.roles WHERE company_norm=?"
        " AND REPLACE(title_norm, ' ', '') = ?"
        " AND job_id != ? AND (job_id LIKE 'li:%') = ?",
        (me["company_norm"], me["title_norm"].replace(" ", ""),
         job_id, 0 if i_am_shadow else 1),
    ).fetchall()
    return cands[0]["job_id"] if len(cands) == 1 else None


def _record_link(conn, a: str, b: str) -> None:
    """Store the link on the SHADOW side only (duplicate_of always points li: → canonical),
    so 'is this row an alias' is one NULL-check everywhere counting applications."""
    shadow, canonical = (a, b) if a.startswith("li:") else (b, a)
    if not shadow.startswith("li:") or canonical.startswith("li:"):
        return                                  # li:↔li: or ats↔ats — not a shadow/canonical pair
    cur = conn.execute("SELECT duplicate_of FROM role_state WHERE job_id=?",
                       (shadow,)).fetchone()
    if not cur or cur["duplicate_of"] != canonical:
        upsert_state(conn, shadow, duplicate_of=canonical)


# Statuses that describe a real-world event on the ONE physical posting, so they must hold on
# both rows of a linked pair. 'new'/'skip' are review bookkeeping and never cascade.
_CASCADE_STATUSES = {"applied", "interviewing", "rejected"}


def set_status(conn, job_id: str, status: str, resume_variant: str | None = None,
               _cascading: bool = False) -> dict:
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}")
    fields: dict = {"status": status}
    if status == "applied":
        fields["applied_date"] = la_today()
        fields["applied_with_resume"] = resume_variant or "base"
    out = upsert_state(conn, job_id, **fields)
    # Cascade real-world statuses to the twin row of the same physical posting. Computed on
    # the fly when no link is stored yet — the twin may have been scanned in only this
    # morning — and self-heals the link for every later consumer. _cascading guards the
    # recursion (partner's own cascade would bounce straight back).
    if not _cascading and status in _CASCADE_STATUSES:
        try:
            partner = None
            row = conn.execute("SELECT duplicate_of FROM role_state WHERE job_id=?",
                               (job_id,)).fetchone()
            if row and row["duplicate_of"]:
                partner = row["duplicate_of"]
            if not partner:
                partner = conn.execute(
                    "SELECT job_id FROM role_state WHERE duplicate_of=?",
                    (job_id,)).fetchone()
                partner = partner["job_id"] if partner else None
            if not partner:
                partner = find_twin(conn, job_id)
            if partner:
                _record_link(conn, job_id, partner)
                cur = (conn.execute("SELECT status FROM role_state WHERE job_id=?",
                                    (partner,)).fetchone() or {"status": "new"})["status"]
                if cur != status:
                    set_status(conn, partner, status, resume_variant, _cascading=True)
        except sqlite3.OperationalError:
            pass        # user DB opened without jobs attached — skip linking, never block the mark
    return out


def set_notes(conn, job_id: str, notes: str) -> dict:
    return upsert_state(conn, job_id, notes=notes)


def reconcile_duplicates(conn) -> dict:
    """One pass over every li: shadow row: link unambiguous twins, mirror real-world statuses,
    and copy tailor artefacts to whichever side lacks them. Idempotent — safe to run any time.

    Mirroring copies the SOURCE row's applied_date/applied_with_resume instead of stamping
    today: the backfill is recording history, not making news. Statuses only flow onto rows
    still at 'new'/'skip' — when both sides carry real statuses (e.g. applied vs interviewing),
    both are human statements and neither is overwritten.

    Tailor pointers go BOTH directions: the extension resolves the ATS row on the employer's
    page and needs has_tailored_pdf there; the UI list often shows the li: row."""
    stats = {"linked": 0, "status_mirrored": 0, "tailor_copied": 0}
    shadows = conn.execute("SELECT job_id FROM jobs.roles WHERE job_id LIKE 'li:%'").fetchall()
    for s in shadows:
        canonical = find_twin(conn, s["job_id"])
        if not canonical:
            continue
        before = conn.execute("SELECT duplicate_of FROM role_state WHERE job_id=?",
                              (s["job_id"],)).fetchone()
        if not before or before["duplicate_of"] != canonical:
            stats["linked"] += 1
        _record_link(conn, s["job_id"], canonical)

        rows = {}
        for jid in (s["job_id"], canonical):
            r = conn.execute("SELECT * FROM role_state WHERE job_id=?", (jid,)).fetchone()
            rows[jid] = dict(r) if r else {"job_id": jid, "status": "new",
                                           "applied_date": None, "applied_with_resume": None,
                                           "tailored_resume_path": None, "prompt_path": None,
                                           "tailor_status": "none"}
        a, b = rows[s["job_id"]], rows[canonical]

        for src, dst in ((a, b), (b, a)):
            if (src.get("status") in _CASCADE_STATUSES
                    and (dst.get("status") or "new") in ("new", "skip")):
                upsert_state(conn, dst["job_id"], status=src["status"],
                             applied_date=src.get("applied_date"),
                             applied_with_resume=src.get("applied_with_resume"))
                dst["status"] = src["status"]
                stats["status_mirrored"] += 1
            if src.get("tailored_resume_path") and not dst.get("tailored_resume_path"):
                upsert_state(conn, dst["job_id"],
                             tailor_status=src.get("tailor_status") or "ready",
                             prompt_path=src.get("prompt_path"),
                             tailored_resume_path=src.get("tailored_resume_path"))
                dst["tailored_resume_path"] = src.get("tailored_resume_path")
                stats["tailor_copied"] += 1
    return stats


# ---------- apply pins (see apply_pins in USER_SCHEMA) ----------

PIN_TTL_MIN = 45     # a pin older than this is stale — an application session doesn't last longer


def _pin_is_fresh(pinned_at) -> bool:
    try:
        when = datetime.datetime.fromisoformat(pinned_at)
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    age_min = (datetime.datetime.now(datetime.timezone.utc) - when).total_seconds() / 60
    return age_min <= PIN_TTL_MIN


def set_apply_pin(conn, job_id: str) -> None:
    """Record 'I'm applying to this role right now'. Adds to the live set — it does NOT
    replace the others, because opening a batch of tabs and clicking ↗ down the list is the
    normal way this gets used, and every one of those clicks is a real statement of intent."""
    # Microseconds, not seconds: two ↗ clicks land in the same second all the time, and when
    # several pins fit the same page we pick the newest — a tie there is a coin flip between
    # two employers' résumés.
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        row = conn.execute(
            "SELECT company_norm FROM jobs.roles WHERE job_id=?", (job_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None          # user DB opened without the jobs DB attached — pin still works,
                            # it just carries no company and can't be matched by company alone
    conn.execute(
        "INSERT INTO apply_pins (job_id, company_norm, pinned_at) VALUES (?, ?, ?)"
        " ON CONFLICT(job_id) DO UPDATE SET pinned_at=excluded.pinned_at,"
        " company_norm=excluded.company_norm",
        (job_id, (row["company_norm"] if row else None) or "", now),
    )
    _prune_apply_pins(conn)
    conn.commit()


def _prune_apply_pins(conn) -> None:
    """Drop pins past the TTL. Done on every write so the table stays a handful of rows and
    a stale pin can never be picked, even if the reader forgets to check."""
    # Same precision as the writer, so the string compare below matches chronological order.
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(minutes=PIN_TTL_MIN)).isoformat()
    conn.execute("DELETE FROM apply_pins WHERE pinned_at < ?", (cutoff,))


def get_apply_pins(conn) -> list[dict]:
    """Every live pin, newest first. The caller picks which one fits the page it is on —
    this layer deliberately does not guess, because guessing is what produced the
    Temporal-résumé-on-a-Precisely-form bug."""
    rows = conn.execute(
        "SELECT job_id, company_norm, pinned_at FROM apply_pins"
        " ORDER BY pinned_at DESC, rowid DESC"        # rowid breaks any residual timestamp tie
    ).fetchall()
    return [dict(r) for r in rows if _pin_is_fresh(r["pinned_at"])]


def get_apply_pin(conn) -> str | None:
    """The most recent live pin's job_id, or None. Back-compat for callers (and older
    extension builds) that still want a single answer — prefer get_apply_pins."""
    pins = get_apply_pins(conn)
    return pins[0]["job_id"] if pins else None


def clear_apply_pin(conn, job_id: str | None = None) -> None:
    """Clear one pin (the frame that claimed it) or all of them."""
    if job_id:
        conn.execute("DELETE FROM apply_pins WHERE job_id=?", (job_id,))
    else:
        conn.execute("DELETE FROM apply_pins")
    conn.commit()


RESPONSE_CONFIDENCE = ["role", "company"]     # best first; see role_state.response_confidence


def record_signal(conn, job_id: str, *, stage: str | None = None, outcome: str | None = None,
                  note: str = "", when: str | None = None,
                  confidence: str = "role", inbound: bool = False) -> dict:
    """Record something that happened to an application after it went out.

    Monotonic on purpose: `stage` only advances (a scheduling email arriving after an onsite
    must not demote the row back to 'responded'), and `first_response_at` is written once —
    it is the funnel's key measurement and a later contact must not overwrite it. Notes are
    appended with their date, never replaced, so re-running a scan can't erase history.

    `confidence` says how sure we are this is the RIGHT row (see the column comment): 'role'
    when the company had one open application or the subject named this title, 'company' when
    the company demonstrably replied but the requisition is unknown. It rides along with
    `first_response_at` and, like `stage`, only ever improves — a later unambiguous signal
    upgrades a 'company' guess to 'role', never the other way round.

    `inbound=True` marks contact that did NOT come out of this application: a recruiter InMail
    or referral invite that PREDATES it. Microsoft wrote on 07-17 and she applied on 07-24;
    Anthropic invited her to the Fellow program on 07-17 and she then filed 19 applications
    there. Counting either as a response would credit the machine for a conversation it did not
    start, so inbound contact moves `last_contact_at` and appends a note — nothing else. It can
    never write `first_response_at`, advance a stage, or close a row (owner call, 2026-08-10).
    """
    when = when or la_today()
    row = conn.execute("SELECT * FROM role_state WHERE job_id=?", (job_id,)).fetchone()
    cur = dict(row) if row else {}
    fields: dict = {"last_contact_at": when}

    if inbound:
        prev = (cur.get("outcome_note") or "").strip()
        line = f"{when}: {note}"
        if note and line not in prev.splitlines():
            fields["outcome_note"] = f"{prev}\n{line}".strip()
        return upsert_state(conn, job_id, **fields)

    if confidence not in RESPONSE_CONFIDENCE:
        raise ValueError(f"invalid confidence {confidence!r}")
    # Only stamped when this signal is what actually creates first_response_at, or when it
    # sharpens an existing guess. Rank by list position: 'role' (0) beats 'company' (1).
    def _mark_confidence() -> None:
        prev = cur.get("response_confidence")
        if prev is None or RESPONSE_CONFIDENCE.index(confidence) < RESPONSE_CONFIDENCE.index(prev):
            fields["response_confidence"] = confidence

    if stage:
        if stage not in STAGE_RANK:
            raise ValueError(f"invalid stage {stage!r}")
        if STAGE_RANK[stage] > STAGE_RANK.get(cur.get("stage") or "applied", -1):
            fields["stage"] = stage
        if stage != "applied" and not cur.get("first_response_at"):
            fields["first_response_at"] = when      # write-once
        if stage != "applied":
            _mark_confidence()
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
        if outcome not in ("in_progress", "no_response"):
            if not cur.get("first_response_at"):
                fields["first_response_at"] = when
            _mark_confidence()
    if note:
        prev = (cur.get("outcome_note") or "").strip()
        line = f"{when}: {note}"
        # Appended, never replaced — but not appended twice. The outcomes job runs at 11:00 and
        # 16:00 and re-reads the same 14 days both times, so without this the same rejection
        # stacks up one line per run and the note stops being readable (2026-08-10).
        if line not in prev.splitlines():
            fields["outcome_note"] = f"{prev}\n{line}".strip()
    return upsert_state(conn, job_id, **fields)


# ---------- interviews (复盘 index; md files stay the source of truth) ----------

def interview_upsert(conn, *, marker_id: str, company: str, happened_on: str,
                     round_type: str, sources: str, job_id: str | None = None,
                     transcript_url: str | None = None, review_path: str | None = None,
                     source: str = "review-md") -> None:
    """Insert or refresh one interview. A job_id already set (auto-matched earlier or linked
    by hand in the UI) is never cleared by a re-ingest — only filled when currently NULL.
    transcript_url / review_path: a value parsed from the files wins; None keeps what's there.

    `source` records who found this interview. A 复盘 always wins: re-ingesting the markdown
    over a row the mailbox backfill guessed at promotes it to 'review-md' and overwrites the
    inferred heading, which is the whole point of reusing the same marker_id."""
    now = la_now_iso()
    row = conn.execute("SELECT job_id FROM interviews WHERE marker_id=?", (marker_id,)).fetchone()
    if row:
        conn.execute(
            "UPDATE interviews SET company=?, happened_on=?, round_type=?, sources=?,"
            " job_id=COALESCE(job_id, ?), transcript_url=COALESCE(?, transcript_url),"
            " review_path=COALESCE(?, review_path), source=?, updated_at=? WHERE marker_id=?",
            (company, happened_on, round_type, sources, job_id,
             transcript_url, review_path, source, now, marker_id))
    else:
        conn.execute(
            "INSERT INTO interviews (marker_id, job_id, company, happened_on, round_type,"
            " sources, transcript_url, review_path, source, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (marker_id, job_id, company, happened_on, round_type, sources,
             transcript_url, review_path, source, now, now))
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

def _sort_rows(out: list[dict], sort: str) -> None:
    """Order the rows in place. `rank_score` = weighted_score + freshness bonus, so the
    default already IS 'score + fresh'; `base_score` is the freshness-free view."""
    if sort == "company":
        out.sort(key=lambda r: (r["company"], -(r["rank_score"] or 0)))
    elif sort == "first_seen":
        out.sort(key=lambda r: (r["first_seen"] or "", r["rank_score"] or 0), reverse=True)
    elif sort == "base_score":
        out.sort(key=lambda r: (r["weighted_score"] or 0, r["first_seen"] or ""), reverse=True)
    else:
        out.sort(key=lambda r: (r["rank_score"] or 0, r["first_seen"] or ""), reverse=True)


def effective_profile_key(cfg: dict, role: dict) -> str:
    """这个岗位实际用哪张投递名片（apply profile）：岗位级手动选择 > 公司级小号名单 >
    默认名片。公司级名单（config apply_profiles.companies，key=company_norm）只影响还没
    投出的岗位 —— 已投的是历史事实，事后把公司划给小号不改写当时用什么身份投的。"""
    ap = cfg.get("apply_profiles") or {}
    default = ap.get("default") or "casey"
    profiles = ap.get("profiles") or {}
    explicit = role.get("apply_profile")
    if explicit and explicit in profiles:
        return explicit
    # 2026-09-25: an applied row with NO recorded card used to fall back to the default card,
    # so at a 小号 company the extension filled Casey + candidate whenever the role had
    # already been marked applied (by a confirmation-mail scan, a twin cascade, or a first
    # attempt). Since api_set_status now freezes the card at apply time, an explicit value is
    # the history; a missing one is unknown, and the company map is the better guess.
    key = (ap.get("companies") or {}).get((role.get("company_norm") or "").lower())
    return key if key in profiles else default


# One public page, no account: apply in one sitting. Workday/Avature demand a per-company
# account plus a 5-6 step wizard — a different class of effort entirely.
_EASY_ATS = {"greenhouse", "ashby", "lever", "rippling", "smartrecruiters", "gem"}
_ACCOUNT_ATS = {"workday", "avature"}


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
    sort: str = "diversity",     # keep select_daily's company interleave; see _sort_rows
    rot: int = 0,
    tag: str = "",               # 'onsite' | 'startup' — company-level flags from company_notes.yaml
    identity: str = "",          # apply-profile key ('casey'/'alex') — 只看这张名片名下的岗位
    tailored: str = "",          # 'ready' = has a validated résumé PDF · 'none' = doesn't
    ease: str = "",              # 'easy' = one-page no-account boards · 'account' = login+wizard
    pace: bool = False,          # apply 投递节奏 (app/pace.py) to the review tab — only the review
                                 # page and autotailor opt in; outreach / tools see the full queue
) -> dict:
    # Company-level tags live in company_notes.yaml (onsite_interview / startup), never in the
    # roles DB: they describe the COMPANY's hiring process, not a posting, and the owner edits
    # them by hand. Loaded once per view so each row can carry its chips + the tag filter works.
    notes_all = cfgmod.load_company_notes(cfg)
    rows = [dict(r) for r in conn.execute(
        "SELECT r.*, s.status AS own_status, s.notes, s.applied_date,"
        "       s.applied_with_resume, s.tailor_status, s.tailored_resume_path,"
        "       s.status_updated_at, s.duplicate_of, s.apply_profile"
        " FROM jobs.roles r LEFT JOIN role_state s ON s.job_id = r.job_id"
    )]
    applied_cos = {r["name_norm"]: r["display_name"] for r in
                   conn.execute("SELECT * FROM applied_companies")}
    # config.yaml ∪ UI-set (company_cooldowns) — {norm_key: {until, reason, source, company}}
    cooldown_cos = active_cooldowns(conn, cfg, la_today())
    # Workspace eligibility applied at VIEW time (owner 2026-09-23): the shared jobs.db keeps
    # one score per role, and each candidate's own dead-ends are filtered here —
    #   needs_sponsorship → hide JDs that say the employer will not sponsor;
    #   location_pref.review_only → review shows only remote / preferred-area roles.
    prof = cfg.get("profile") or {}
    needs_spon = bool(prof.get("needs_sponsorship"))
    loc_only = bool(((cfg.get("scoring") or {}).get("location_pref") or {}).get("review_only"))
    # Fit rank (owner 2026-09-23): the shared jobs.db scores every role with the OWNER's keyword
    # weights, so a second candidate's queue led with infra JDs asking 13 hard skills of which
    # he names 6 (résumé score 54) while hers ask 5 and she names 4. With fit_pref on, each row
    # gets +bonus × (JD hard skills the candidate's base already names / JD hard skills) on
    # rank_score, and rows under review_min leave the review tab (still under 'all').
    fit_cfg = (cfg.get("scoring") or {}).get("fit_pref") or {}
    fit_on = bool(fit_cfg.get("enabled"))
    fit_base: set = set()
    if fit_on:
        from . import resume_score as _rs, tailoring as _tl
        try:
            fit_base = _rs.base_hard_skills(_tl._base_text(_tl.load_base(cfg)))
        except FileNotFoundError:
            fit_on = False
    sig_mode = cfg.get("signature_match", "badge")

    closed_by_sig: dict[tuple, dict] = {}
    for r in rows:
        if (r["own_status"] or "new") in CLOSED_STATUSES:
            closed_by_sig.setdefault((r["company_norm"], r["title_norm"]), r)

    today = la_today()
    week_ago = (datetime.date.fromisoformat(today) - datetime.timedelta(days=6)).isoformat()
    two_weeks_ago = (datetime.date.fromisoformat(today) - datetime.timedelta(days=13)).isoformat()
    month_ago = (datetime.date.fromisoformat(today) - datetime.timedelta(days=29)).isoformat()

    out = []
    counts = {"new": 0, "applied": 0, "interviewing": 0, "skip": 0, "rejected": 0}
    # For the duplicate_of hide below: only honour a link whose canonical row still exists —
    # purge tools DELETE FROM roles, and a dangling link must not vanish a live posting
    # (followup.py:105 guards the same way).
    live_ids = {r["job_id"] for r in rows}
    for r in rows:
        eff = r["own_status"] or "new"
        r["status"] = eff
        r["notes"] = r["notes"] or ""
        r["hidden_company"] = r["company_norm"] in applied_cos
        cn_notes = notes_all.get(r["company_norm"], {})
        r["onsite"] = bool(cn_notes.get("onsite_interview"))
        r["onsite_note"] = cn_notes.get("onsite_note") or ""
        r["startup"] = bool(cn_notes.get("startup"))
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
        # 优先远程或湾区 (owner 2026-09-22): rank-only lift, same contract as freshness
        lp = scoring.location_pref(r.get("location"), r.get("is_remote"), cfg)
        r["loc_pref"] = lp
        r["rank_score"] = (r["weighted_score"] or 0) + f["bonus"] + lp["bonus"]
        r["no_sponsor"] = needs_spon and scoring.no_sponsorship_jd(r.get("jd_text"))
        r["loc_excluded"] = loc_only and lp["kind"] not in ("remote", "bay")
        r["fit"] = None
        r["fit_excluded"] = False
        if fit_on:
            from . import resume_score as _rs
            cov = _rs.fit_coverage(_rs.jd_hard_skills(r.get("jd_hash"), r.get("jd_text")), fit_base)
            if cov is not None:
                bonus = round(float(fit_cfg.get("bonus", 15)) * cov, 1)
                r["fit"] = {"coverage": round(cov, 2), "bonus": bonus}
                r["rank_score"] += bonus
                r["fit_excluded"] = cov < float(fit_cfg.get("review_min", 0.30))
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
        # A linked li: shadow row (role_state.duplicate_of → canonical ATS row) is the same
        # physical posting twice — only the canonical row (direct apply link, full JD) stays
        # in the review queue. Owner-approved 2026-09-15; budget/followup already count this way.
        in_review = (eff == "new" and not r["hidden_company"] and not hidden_sig and not cd
                     and r["verdict"] != "drop"
                     and not r["no_sponsor"] and not r["loc_excluded"] and not r["fit_excluded"]
                     and not (r["duplicate_of"] and r["duplicate_of"] in live_ids))
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
        if remote == "bay" and r["loc_pref"]["kind"] not in ("remote", "bay"):
            continue                      # "remote or Bay Area" — the location_kind classifier
        # 投递身份：每行算出实际用哪张名片（手动 > 公司小号名单 > 默认），徽章和
        # identity 过滤都用它。放在 tag 过滤旁边，跟其他公司级属性一个待遇。
        r["apply_profile_effective"] = effective_profile_key(cfg, r)
        if identity and r["apply_profile_effective"] != identity:
            continue
        if tailored == "ready" and (r["tailor_status"] or "") != "ready":
            continue
        if tailored == "none" and (r["tailor_status"] or "") == "ready":
            continue
        # Application friction (owner 2026-09-10): one-page public forms vs boards that force
        # account creation + a multi-step wizard. 'manual' (LinkedIn/captured) roles land on
        # arbitrary external portals — they count as neither and only show under "all".
        if ease == "easy" and r["ats"] not in _EASY_ATS:
            continue
        if ease == "account" and r["ats"] not in _ACCOUNT_ATS:
            continue
        if tag == "onsite" and not r["onsite"]:
            continue
        if tag == "startup" and not r["startup"]:
            continue
        if tag == "onsite_or_startup" and not (r["onsite"] or r["startup"]):
            continue
        # Compare on the DATE only: li:/cap: rows store a full timestamp in first_seen
        # ("2026-09-22T08:51:18-07:00"), so an equality test against the date hid every
        # LinkedIn/captured role from "new today" (59 in the queue, 4 shown, 2026-09-22).
        if seen == "today" and (r["first_seen"] or "")[:10] != today:
            continue
        if seen == "week" and (r["first_seen"] or "") < week_ago:
            continue
        if seen == "two_weeks" and (r["first_seen"] or "") < two_weeks_ago:
            continue
        if seen == "month" and (r["first_seen"] or "") < month_ago:
            continue
        # 任意日期值 = 截止线「这天及之后」。2026-09-11 之前是"恰好那天"，从没被用过；
        # owner 的真实需求是"太旧的不投"，截止线才是那个语义。
        if seen not in ("all", "today", "week", "two_weeks", "month") and seen \
                and (r["first_seen"] or "") < seen:
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

    if fit_on:
        from . import resume_score as _rs
        _rs.save_jd_cache()

    _sort_rows(out, sort)

    # --- per-company cap: in the review tab, show at most `max` roles per company (top by
    # score), frozen for `window_days` so you work through one batch before the next surfaces.
    # A company over the cap records a batch date; overflow is held (still visible under 'all'). ---
    cap_cfg = cfg.get("per_company_cap") or {}
    cap_max = int(cap_cfg.get("max", 0) or 0)
    cap_info: dict[str, dict] = {}
    budget_info: dict = {}
    if status == "new" and budget.enabled(cfg):
        # New three-knob budget supersedes the display-only per-company cap entirely:
        # stock (lifetime per company) + pace (strict-company rate limits) + daily buckets.
        out, budget_info = budget.select_daily(out, rows, cfg, today, rot=rot)
        counts["new"] = len(out)
        # select_daily re-orders by its own company ring, which silently made the sort
        # dropdown a no-op in the review tab (all four options returned byte-identical
        # lists). Re-apply the chosen sort AFTERWARDS: 'diversity' keeps the interleave
        # select_daily just built, anything else re-sorts those same rows. Which roles you
        # get never depends on the sort — only their order does.
        if sort and sort != "diversity":
            _sort_rows(out, sort)
    elif status == "new" and cap_max > 0:
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

    pace_info: dict = {}
    if status == "new" and pace:
        from . import pace as pacemod
        # The day's list is frozen from the UNFILTERED queue (same budget order the page uses,
        # rot=0), so which filter was on at first open never decides the day's companies.
        full = lambda: fetch_view(conn, cfg, status="new", sort="diversity", rot=0)["rows"]
        live = {r["job_id"] for r in rows if r.get("in_review")}
        out, pace_info = pacemod.apply_review(conn, cfg, out, full, today, live_ids=live)
        if pace_info["pace"]["limited"]:
            counts["new"] = len(out)
    elif pace:
        # applied / interviewing / all tabs: keep the nav's "review N" in step with the paced
        # review tab. Read-only — visiting another tab never creates the day's freeze; before
        # the freeze exists the badge shows what the first open will push (≤ N companies).
        from . import pace as pacemod
        eff = pacemod.effective(conn, cfg, today)
        if eff["limited"]:
            n = eff["companies_per_day"]
            live_rows = [r for r in rows if r.get("in_review")]
            if pacemod.has_freeze(conn, today):
                ids = {f["job_id"] for f in pacemod._frozen(conn, today)[:n]}
                counts["new"] = sum(1 for r in live_rows if r["job_id"] in ids)
            else:
                counts["new"] = min(n, len({r["company_norm"] for r in live_rows}))

    companies = sorted({r["company"] for r in rows})
    return {
        "rows": out,
        "pace": pace_info,
        "counts": counts,
        "total_roles": len(rows),
        "companies": companies,
        "applied_companies": sorted(applied_cos.values(), key=str.lower),
        "cap_info": cap_info,
        "budget": budget_info,
        "diversify_picks": div_picks,
        "cooldowns": cooldown_cos,            # the header shows a count + link to manage them
        "workspace": cfg.get("workspace") or {},   # {name} of the candidate this instance serves
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
            "SELECT r.*, s.status AS own_status, s.tailor_status, s.tailored_resume_path,"
            "       s.apply_profile"
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
                "SELECT r.*, s.status AS own_status, s.tailor_status, s.tailored_resume_path,"
                "       s.apply_profile"
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
            "SELECT r.*, s.status AS own_status, s.tailor_status, s.tailored_resume_path,"
            "       s.apply_profile"
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
        "       s.tailored_resume_path, s.apply_profile"
        " FROM jobs.roles r LEFT JOIN role_state s ON s.job_id = r.job_id"
        " WHERE r.job_id = ?",
        (job_id,),
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    d["status"] = d.pop("own_status") or "new"
    return d
