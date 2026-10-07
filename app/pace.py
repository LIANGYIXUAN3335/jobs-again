"""投递节奏 / delivery pace (owner 2026-09-28 拍板).

One switch, four levels, for when she wants the workbench to push LESS without losing anything:

    冲刺 sprint  review shows every eligible company (what the page did before this existed);
                 autotailor tailors every new eligible role (≤ auto_tailor.batch_cap per run)
    正常 normal  30 companies a day on the review page, ≤ 40 new résumés a day
    低速 slow    10 companies a day, ≤ 10 new résumés a day
    暂停 pause   nothing new on the review page, no autotailor for this workspace
    自定义 custom her own two numbers (companies a day, résumés a day) — owner 2026-09-28:
                 「可以自己选择到底每天推多少个」

Which companies make the day's list: the same order the review page already uses — each
company's best role by rank_score (match score + freshness bonus), strongest companies first,
one role per company.

Design rules (all from the 2026-09-28 discussion):

* Scanning is untouched. Four rounds a day keep landing roles in jobs.db; this module only
  decides how many of them she is SHOWN and how many get a résumé. Switching back up later
  loses nothing.
* The daily quota is per CALENDAR DAY (LA time), not per scan round. The first time the review
  queue is computed on a day, the top-N companies' lead roles are FROZEN into `pace_daily`.
  Working a row off (applied / skip / …) uses up quota — it is NOT replaced by the next company.
  That keeps the 2026-07-31 rule intact: clearing a row must never make a new one appear, or the
  queue reads as endless. Roles that land in the afternoon rounds wait for tomorrow's freeze.
* The freeze is computed on the UNFILTERED queue, so the day's list never depends on which
  filter she happened to have on when she first opened the page. Filters then narrow the
  frozen list.
* Raising the level mid-day tops the frozen list up; lowering it shows only the first N frozen.
* Levels below the revert target (slow / pause by default) auto-revert to `revert_to` after
  `revert_days` unless she picks a date or "no auto-revert" — so a low setting can't be
  forgotten for a month. `until` is the day the revert HAPPENS (the UI reads "10/12 自动回正常"),
  so the low level is in force through until-1.
* Per workspace: the setting lives in that workspace's user_state.db. Robin / clients are
  unaffected by her switch.
* Manual "✎ 一键 tailor 当前列表" is an explicit ask and is neither capped nor counted here.
"""
from __future__ import annotations

import datetime

from . import config as cfgmod
from . import store

ORDER = ("sprint", "normal", "slow", "pause", "custom")
CUSTOM_MAX = 500

DEFAULT_MODES: dict[str, dict] = {
    "sprint": {"label": "冲刺", "companies_per_day": None, "tailor_per_day": None},
    "normal": {"label": "正常", "companies_per_day": 30, "tailor_per_day": 40},
    "slow":   {"label": "低速", "companies_per_day": 10, "tailor_per_day": 10},
    "pause":  {"label": "暂停", "companies_per_day": 0, "tailor_per_day": 0},
    # numbers come from pace_settings (her input); these are only the form's starting values
    "custom": {"label": "自定义", "companies_per_day": 20, "tailor_per_day": 20},
}

_FILLED = "__filled__"   # sentinel job_id: "today's freeze already looked for N companies"


def conf(cfg: dict) -> dict:
    """Merged view of config `delivery_pace:` over the built-in defaults."""
    dp = cfg.get("delivery_pace") or {}
    over = dp.get("modes") or {}
    modes = {}
    for k in ORDER:
        m = {**DEFAULT_MODES[k], **(over.get(k) or {})}
        for f in ("companies_per_day", "tailor_per_day"):
            if m[f] is not None:
                m[f] = max(0, int(m[f]))
        modes[k] = m
    default = dp.get("default_mode", "sprint")
    revert_to = dp.get("revert_to", "normal")
    if default not in modes:
        default = "sprint"
    if revert_to not in modes:
        revert_to = "normal"
    return {"modes": modes, "default": default, "revert_to": revert_to,
            "revert_days": max(1, int(dp.get("revert_days", 14)))}


def _auto_reverts(c: dict, mode: str, companies: int | None = None) -> bool:
    """Only levels that push LESS than the revert target expire on their own."""
    target = c["modes"][c["revert_to"]]["companies_per_day"]
    this = companies if mode == "custom" and companies is not None else c["modes"][mode]["companies_per_day"]
    if this is None:
        return False
    return target is None or this < target


def _num(v, name: str) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number") from None
    if not 0 <= n <= CUSTOM_MAX:
        raise ValueError(f"{name} must be between 0 and {CUSTOM_MAX}")
    return n


def get_setting(conn) -> dict | None:
    r = conn.execute("SELECT mode, until, set_at, companies_per_day, tailor_per_day "
                     "FROM pace_settings WHERE id=1").fetchone()
    return dict(r) if r else None


def set_mode(conn, cfg: dict, mode: str, until: str | None = None,
             auto_revert: bool = True, companies_per_day=None, tailor_per_day=None) -> dict:
    """Store the level. `until` (ISO date = the day it switches back) overrides the default;
    auto_revert=False keeps a low level until she changes it. Levels at or above the revert
    target never carry an until. mode='custom' needs companies_per_day (0–500);
    tailor_per_day defaults to the same number."""
    c = conf(cfg)
    if mode not in c["modes"]:
        raise ValueError(f"unknown mode {mode!r}; one of {', '.join(ORDER)}")
    cpd = tpd = None
    if mode == "custom":
        if companies_per_day in (None, ""):
            raise ValueError("custom needs companies_per_day")
        cpd = _num(companies_per_day, "companies_per_day")
        tpd = cpd if tailor_per_day in (None, "") else _num(tailor_per_day, "tailor_per_day")
    today = datetime.date.fromisoformat(store.la_today())
    u: str | None = None
    if _auto_reverts(c, mode, cpd):
        if until:
            d = datetime.date.fromisoformat(str(until)[:10])   # garbage → ValueError → 422
            if d <= today:
                raise ValueError("until must be after today")
            u = d.isoformat()
        elif auto_revert:
            u = (today + datetime.timedelta(days=c["revert_days"])).isoformat()
    conn.execute(
        "INSERT INTO pace_settings(id, mode, until, set_at, companies_per_day, tailor_per_day) "
        "VALUES(1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET mode=excluded.mode, until=excluded.until, "
        "set_at=excluded.set_at, "
        # presets write NULL here; COALESCE keeps her last custom numbers for the form
        "companies_per_day=COALESCE(excluded.companies_per_day, pace_settings.companies_per_day), "
        "tailor_per_day=COALESCE(excluded.tailor_per_day, pace_settings.tailor_per_day)",
        (mode, u, store.la_now_iso(), cpd, tpd))
    conn.commit()
    return effective(conn, cfg)


def effective(conn, cfg: dict, today: str | None = None) -> dict:
    """The level in force today. An expired `until` resolves to revert_to (reported as
    reverted_from so the UI can say what happened) — computed, not written, so a read-only
    caller never has to write."""
    c = conf(cfg)
    today = today or store.la_today()
    s = get_setting(conn)
    mode = (s or {}).get("mode") or c["default"]
    if mode not in c["modes"]:
        mode = c["default"]
    until = (s or {}).get("until")
    reverted_from = None
    if until and today >= until:
        reverted_from, mode, until = mode, c["revert_to"], None
    custom = dict(c["modes"]["custom"])
    for f in ("companies_per_day", "tailor_per_day"):     # her last custom numbers, if any
        if (s or {}).get(f) is not None:
            custom[f] = int(s[f])
    m = custom if mode == "custom" else c["modes"][mode]
    return {"mode": mode, "label": m["label"],
            "companies_per_day": m["companies_per_day"], "tailor_per_day": m["tailor_per_day"],
            "limited": m["companies_per_day"] is not None,
            "until": until, "reverted_from": reverted_from,
            "reverted_label": c["modes"][reverted_from]["label"] if reverted_from else None,
            "set_at": (s or {}).get("set_at"),
            "revert_to": c["revert_to"], "revert_label": c["modes"][c["revert_to"]]["label"],
            "revert_days": c["revert_days"],
            "levels": [{"mode": k, **(custom if k == "custom" else c["modes"][k]),
                        "auto_reverts": _auto_reverts(c, k, custom["companies_per_day"])}
                       for k in ORDER]}


# ---------------------------------------------------------------------------- review side

def _frozen(conn, day: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT job_id, company_norm, pos FROM pace_daily "
        "WHERE day=? AND kind='shown' ORDER BY pos", (day,))]


def _filled_for(conn, day: str) -> int:
    r = conn.execute("SELECT MAX(pos) FROM pace_daily WHERE day=? AND kind='filled'", (day,)).fetchone()
    return int(r[0]) if r and r[0] is not None else -1


def freeze_today(conn, cfg: dict, n: int, ordered_rows_fn, today: str | None = None) -> list[dict]:
    """Make sure today's frozen list holds (up to) n companies; return it in order.

    ordered_rows_fn() → the UNFILTERED review queue in its budget order (lead role of each
    company first). Only called when the list needs topping up, and at most once per (day, n):
    a queue with fewer than n companies would otherwise recompute on every page view."""
    today = today or store.la_today()
    frozen = _frozen(conn, today)
    if len(frozen) >= n or _filled_for(conn, today) >= n:
        return frozen
    # Build the candidate order BEFORE taking the lock (fetch_view commits internally), then
    # serialize: the web page and the autotailor prep can both be first of the day, and two
    # unlocked freezers merged two different top-N lists (duplicate companies, tied pos).
    cand = list(ordered_rows_fn())
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        frozen = _frozen(conn, today)                      # re-read under the lock
        if len(frozen) >= n or _filled_for(conn, today) >= n:
            conn.commit()
            return frozen
        _freeze_locked(conn, today, n, cand, frozen)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return _frozen(conn, today)


def _freeze_locked(conn, today: str, n: int, cand: list[dict], frozen: list[dict]) -> None:
    have = {f["company_norm"] for f in frozen}
    now = store.la_now_iso()
    pos = (frozen[-1]["pos"] + 1) if frozen else 0
    for r in cand:
        if len(frozen) >= n:
            break
        cn = r.get("company_norm")
        if not cn or cn in have:
            continue
        have.add(cn)
        # OR IGNORE: the web page and the 09:xx autotailor prep can freeze at the same moment;
        # both compute the same order, so whichever lands first wins and the other is a no-op.
        conn.execute("INSERT OR IGNORE INTO pace_daily(day, kind, job_id, company_norm, pos, at) "
                     "VALUES(?,?,?,?,?,?)", (today, "shown", r["job_id"], cn, pos, now))
        frozen.append({"job_id": r["job_id"], "company_norm": cn, "pos": pos})
        pos += 1
    conn.execute("INSERT OR REPLACE INTO pace_daily(day, kind, job_id, company_norm, pos, at) "
                 "VALUES(?,?,?,?,?,?)", (today, "filled", f"{_FILLED}{n}", None, n, now))


def has_freeze(conn, today: str | None = None) -> bool:
    today = today or store.la_today()
    return conn.execute("SELECT 1 FROM pace_daily WHERE day=? AND kind IN ('shown','filled') LIMIT 1",
                        (today,)).fetchone() is not None


def frozen_count(conn, cfg: dict, today: str | None = None) -> int:
    """How many companies today's list pushes at the level now in force (capped at N, so a
    level lowered mid-day reports the smaller number the review page actually uses)."""
    eff = effective(conn, cfg, today)
    if not eff["limited"]:
        return 0
    return len(_frozen(conn, today or store.la_today())[:eff["companies_per_day"]])


def apply_review(conn, cfg: dict, rows: list[dict], ordered_rows_fn,
                 today: str | None = None, live_ids: set | None = None) -> tuple[list[dict], dict]:
    """Narrow the (already filtered, already ordered) review rows to today's quota.
    Returns (rows, info). Sprint → rows untouched.

    live_ids = job_ids still in the UNFILTERED review queue. A frozen row counts as done once it
    leaves that queue for ANY reason — applied/skip, but also 🚫 cooldown, hidden company,
    duplicate-linked, dropped, delisted — so 「做完了」 is reachable and the 「筛选后显示」 hint only
    appears when a real filter narrows the list."""
    today = today or store.la_today()
    eff = effective(conn, cfg, today)
    if not eff["limited"]:
        return rows, {"pace": eff}
    n = eff["companies_per_day"]
    frozen = freeze_today(conn, cfg, n, ordered_rows_fn, today)[:n] if n > 0 else []
    ids = [f["job_id"] for f in frozen]
    idset = set(ids)
    shown = [r for r in rows if r["job_id"] in idset]
    if live_ids is not None:
        remaining = sum(1 for j in ids if j in live_ids)
    else:
        worked = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            worked += conn.execute(
                f"SELECT COUNT(*) FROM role_state WHERE job_id IN ({','.join('?' * len(chunk))}) "
                "AND COALESCE(status,'new') != 'new'", chunk).fetchone()[0]
        remaining = max(0, len(frozen) - worked)
    info = {"pace": eff, "quota": n, "frozen": len(frozen), "handled": len(frozen) - remaining,
            "remaining": remaining, "shown": len(shown),
            "tailored_today": tailored_today(conn, today)}
    return shown, info


# ---------------------------------------------------------------------------- tailor side

def tailored_today(conn, today: str | None = None) -> int:
    today = today or store.la_today()
    return conn.execute("SELECT COUNT(*) FROM pace_daily WHERE day=? AND kind='tailored'",
                        (today,)).fetchone()[0]


def tailor_allowance(conn, cfg: dict, today: str | None = None) -> int | None:
    """How many more résumés autotailor may queue today. None = no pace limit (sprint)."""
    eff = effective(conn, cfg, today)
    if not eff["limited"]:
        return None
    return max(0, int(eff["tailor_per_day"] or 0) - tailored_today(conn, today))


def log_tailored(conn, job_ids: list[str], today: str | None = None) -> None:
    today = today or store.la_today()
    now = store.la_now_iso()
    conn.executemany("INSERT OR IGNORE INTO pace_daily(day, kind, job_id, company_norm, pos, at) "
                     "VALUES(?,?,?,?,?,?)", [(today, "tailored", j, None, 0, now) for j in job_ids])
    conn.commit()


def prune(conn, keep_days: int = 30) -> None:
    """pace_daily is a working table, not history — drop rows older than keep_days."""
    cutoff = (datetime.date.fromisoformat(store.la_today())
              - datetime.timedelta(days=keep_days)).isoformat()
    conn.execute("DELETE FROM pace_daily WHERE day < ?", (cutoff,))
    conn.commit()


__all__ = ["ORDER", "DEFAULT_MODES", "conf", "get_setting", "set_mode", "effective",
           "freeze_today", "has_freeze", "frozen_count", "apply_review", "tailored_today", "tailor_allowance",
           "log_tailored", "prune"]

_ = cfgmod  # imported for callers that monkeypatch config through this module in tests
