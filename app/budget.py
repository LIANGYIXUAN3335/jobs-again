"""Application budget — the three knobs that decide what the review queue shows today.

Replaces the old `per_company_cap` (a pure DISPLAY cap that never limited anything) and the
`auto_tailor.min_score` gate. The governing principle, set by the owner 2026-07-30:

    eligibility is BINARY and decides whether to apply at all;
    the 0-100 score only ORDERS what's already eligible — it is never a gate.

Three independent knobs, because they answer opposite questions:

  ① STOCK  — how many applications this company is worth, total. Never resets. The cost of
             concentration (37 applications to one company are ~1 independent sample, and
             ATS anti-abuse scores repeat applicants down) depends on the TOTAL, not the
             pace — so a daily cap that refills tomorrow does not address it. Spend the 8,
             the company retires, and the slot moves to one of the ~150 companies with zero.

  ② PACE   — how often a strict company will accept another application (Ashby's 3/60 days
             is a real limit we already hit at Plaid on 2026-07-20). Time-windowed.

  ③ ROTATION — one row per company, and a cursor that rotates BOTH which companies lead
             and which of that company's roles is shown. Replaces the old fixed buckets
             (confidence/fresh/explore), which were undone by a final re-sort on score
             anyway, so explore picks sank to the bottom and were never seen.

             Owner's requirement (2026-07-30): "每个公司至少在前面能轮着出现一次".
             Random shuffling does NOT give that — it is memoryless, so covering all
             201 companies in the top-50 takes ~24 draws (coupon collector) instead of
             the 5 a rotation needs. So this is a ROTATION, never a shuffle.

             One integer `rot` drives everything, which is what keeps the page
             deterministic: refresh does not move anything, only 「换一批」 does.
             Nothing is persisted — the cursor lives in the URL (owner: no freezing).
"""
from __future__ import annotations

import datetime
import re


def _cfg(cfg: dict) -> dict:
    return (cfg.get("application_budget") or {}) if isinstance(cfg, dict) else {}


def enabled(cfg: dict) -> bool:
    return bool(_cfg(cfg).get("enabled"))


def _days_ago(today: str, n: int) -> str:
    return (datetime.date.fromisoformat(today) - datetime.timedelta(days=n)).isoformat()


# Coarse role FLAVOUR from the title, used only by the tie-break below. This is deliberately
# not resume_score.role_family() — that one answers "is this even an engineering job" (eng /
# non_eng) and so calls every row here "eng", which cannot break any tie. Order matters:
# "ML Evaluation Infra" is ML, not infra.
_KINDS: list[tuple[str, str]] = [
    ("ml", r"machine learning|\bml\b|\bai\b|deep learning|\bnlp\b|computer vision|perception|"
           r"research scientist|model|inference|\brl\b"),
    # "Data Center Engineer" is a facilities/hardware job, not data engineering - it has to
    # be caught before the \bdata\b rule or Adyen's rack-and-cable role reads as "data".
    ("infra", r"data\s?cent(er|re)|datacent"),
    ("data", r"\bdata\b|analytic|\betl\b|warehouse|pipeline|database|\bsql\b"),
    ("infra", r"infra|platform|\bsre\b|site reliability|devops|cloud|kubernetes|systems engineer|"
              r"compute|network|storage"),
    ("security", r"security|privacy|trust|compliance|cryptograph|identity|\biam\b"),
    ("mobile", r"\bios\b|android|mobile|kotlin|swift"),
    ("frontend", r"front[\s-]?end|\bui\b|\bux\b|web engineer|javascript|typescript|react"),
    ("fullstack", r"full[\s-]?stack"),
    ("backend", r"back[\s-]?end|server|distributed|\bapi\b|microservice|payments|billing"),
]
_KINDS_RE = [(name, re.compile(pat, re.I)) for name, pat in _KINDS]


def role_kind(title: str | None) -> str:
    t = title or ""
    for name, rx in _KINDS_RE:
        if rx.search(t):
            return name
    return "other"


def stock_status(rows: list[dict], cfg: dict, today: str | None = None) -> dict[str, dict]:
    """Per company: how much of its application budget is spent inside the rolling window.

    `rows` is every role in the DB (all statuses) — spend is counted from real outcomes,
    not from what happens to be visible today.

    The window (`stock.window_days`) exists because the first version counted for life, and
    that punished the wrong thing: Apple's 9 applications all went out on 2026-07-23/24, a
    two-day burst, and a lifetime cap would have closed Apple off permanently for it. Both
    real costs of concentration decay with time — nine applications in one week are ~one
    independent sample, nine spread over months are not (different reqs, different readers,
    a different résumé), and every published ATS anti-abuse rule we've actually hit is
    rate-shaped (Ashby 3/60d, Coinbase 3/180d), never a lifetime count. Omit window_days
    to keep the old lifetime behaviour.
    """
    b = _cfg(cfg).get("stock") or {}
    default = int(b.get("default", 8))
    overrides = {k.lower(): int(v) for k, v in (b.get("overrides") or {}).items()}
    counts_as = set(b.get("counts") or ["applied", "interviewing", "rejected"])
    window = b.get("window_days")
    since = _days_ago(today or datetime.date.today().isoformat(), int(window)) if window else None

    spent: dict[str, int] = {}
    name: dict[str, str] = {}
    for r in rows:
        cn = r["company_norm"]
        name.setdefault(cn, r["company"])
        if (r.get("own_status") or "new") not in counts_as:
            continue
        # No applied_date means we can't age it out; count it (fail closed, never refund a
        # slot just because the date is missing).
        if since and (r.get("applied_date") or "9999") < since:
            continue
        spent[cn] = spent.get(cn, 0) + 1

    out = {}
    for cn, disp in name.items():
        limit = overrides.get(cn, default)
        used = spent.get(cn, 0)
        out[cn] = {"company": disp, "limit": limit, "used": used,
                   "left": max(0, limit - used), "retired": used >= limit,
                   "window_days": int(window) if window else None}
    return out


def blocked_status(rows: list[dict], cfg: dict, today: str) -> dict[str, dict]:
    """Companies that interviewed her and said no — do not apply again for a while.

    Distinct from stock (volume) and pace (rate): those two ration a company that might
    still say yes. This one is a company that already answered. Amazon's 2026-07-21 result
    was an explicit "recycle in 6 months", which is the industry norm and the default here.
    """
    b = _cfg(cfg).get("blocked") or {}
    days = int(b.get("days", 180))
    out = {}
    for key, val in (b.get("companies") or {}).items():
        cn = key.lower()
        when = str(val.get("since") if isinstance(val, dict) else val)
        note = (val.get("reason", "") if isinstance(val, dict) else "")
        try:
            until = (datetime.date.fromisoformat(when)
                     + datetime.timedelta(days=int(
                         val.get("days", days) if isinstance(val, dict) else days))).isoformat()
        except ValueError:
            continue
        if until > today:
            disp = next((r["company"] for r in rows if r["company_norm"] == cn), key)
            out[cn] = {"company": disp, "since": when, "until": until, "reason": note}
    return out


def pace_status(rows: list[dict], cfg: dict, today: str) -> dict[str, dict]:
    """Per strict company: is it inside its own rate limit right now?"""
    out = {}
    for key, rule in (_cfg(cfg).get("pace") or {}).items():
        cn = key.lower()
        window = int(rule.get("window_days", 30))
        limit = int(rule.get("max", 3))
        since = _days_ago(today, window)
        recent = [r for r in rows
                  if r["company_norm"] == cn and (r.get("applied_date") or "") >= since]
        if len(recent) >= limit:
            oldest = min(r["applied_date"] for r in recent)
            until = (datetime.date.fromisoformat(oldest)
                     + datetime.timedelta(days=window)).isoformat()
            out[cn] = {"blocked": True, "until": until, "limit": limit,
                       "window_days": window, "recent": len(recent),
                       "reason": rule.get("reason", "")}
    return out

def select_daily(candidates: list[dict], all_rows: list[dict], cfg: dict,
                 today: str, rot: int = 0) -> tuple[list[dict], dict]:
    """One row per company, company order rotated by `rot`.

    Returns (selected_rows, info) - ALL companies, not a truncated top-N. The old version
    handed over 45 rows chosen by score buckets; the owner's call (2026-07-30) is that the
    list should be every eligible company, one role each, with the "change batch" button
    rotating who leads. `total` is now the ROUND SIZE (how many lead the page before you
    rotate), not a cap.

    Three things `rot` controls, all deterministic - refresh must never move the page:
      - which company leads     companies[off:] + companies[:off],  off = rot*round % n
      - which role each shows   that company's pool rotated by rot % len(pool)
      - nothing else            no randomness anywhere, no stored state

    Why a per-company pool of 3 rather than "whatever role, at random" (the owner's first
    instinct): within ONE company the score spread is large and real - Snowflake's 43
    eligible roles run 70.9 down to 32.6, OpenAI's 196 run 76.8 down to 46.9 (median
    company spread 14.2). Picking at random would routinely surface a 47 while the 77 sat
    next to it. Top-3 keeps quality tight and still unblocks a company's #2 and #3, which
    is the actual problem: with one row per company, #2 is invisible until #1 leaves the
    queue, and #1 only leaves when it is marked applied/skip.
    """
    d = _cfg(cfg).get("daily") or {}
    round_size = max(1, int(d.get("total", 50)))
    pool_n = max(1, int(d.get("pool", 3)))
    tie = float(d.get("tie_break", 3))

    stock = stock_status(all_rows, cfg, today)
    pace = pace_status(all_rows, cfg, today)
    dead = blocked_status(all_rows, cfg, today)

    # --- hard filters, applied before anything is grouped. Order is deliberate: a company
    # that already interviewed and passed is reported as such even if it also happens to be
    # out of stock — "they said no" is the more useful reason to see. ---
    eligible, blocked = [], {}
    for r in candidates:
        cn = r["company_norm"]
        if cn in dead:
            blocked.setdefault(r["company"], {"reason": "blocked", **dead[cn]})
            continue
        st = stock.get(cn)
        if st and st["retired"]:
            blocked.setdefault(r["company"], {"reason": "stock", **st})
            continue
        if cn in pace:
            blocked.setdefault(r["company"], {"reason": "pace", **pace[cn]})
            continue
        eligible.append(r)

    by_co: dict[str, list[dict]] = {}
    for r in eligible:
        by_co.setdefault(r["company_norm"], []).append(r)
    for rs in by_co.values():
        rs.sort(key=lambda r: (-(r.get("rank_score") or 0), r.get("first_seen") or ""))

    # Company ring: ordered by each company's best role, then rotated. Sorting the ring by
    # score (not by name) means round 1 still leads with the strongest companies; rotation
    # is what guarantees every company eventually leads.
    companies = sorted(by_co, key=lambda cn: (-(by_co[cn][0].get("rank_score") or 0), cn))
    n_co = len(companies)
    rounds = max(1, -(-n_co // round_size))
    rot = int(rot or 0)
    off = (rot * round_size) % n_co if n_co else 0
    companies = companies[off:] + companies[:off]

    # Round 1: each company's lead role, chosen with the flavour tie-break below.
    lead_of: dict[str, dict] = {}
    kind_used: dict[str, int] = {}
    for cn in companies:
        pool = by_co[cn][:pool_n]
        start = rot % len(pool)
        rp = pool[start:] + pool[:start]
        # Tie-break: inside the noise band of whatever `rot` put at the head (<= `tie` points;
        # the score measures keyword coverage, so 2 points is not a real difference), prefer
        # the flavour least used so far on this page. Without it, "each company's best role"
        # means "each company's backend role", because her keyword pool IS backend - 50
        # different companies but one single bet. Deterministic: depends only on `rot`.
        head = rp[0].get("rank_score") or 0
        band = [r for r in rp if (r.get("rank_score") or 0) >= head - tie]
        best = None
        for i, r in enumerate(band):
            key = (kind_used.get(role_kind(r.get("title")), 0), i)
            if best is None or key < best[0]:
                best = (key, r)
        pick = best[1]
        kind = role_kind(pick.get("title"))
        kind_used[kind] = kind_used.get(kind, 0) + 1
        lead_of[cn] = pick

    # Then INTERLEAVE the rest: company A's #1, company B's #1, … then everyone's #2, and so
    # on. Owner's call (2026-07-31), replacing "one role per company, the rest held back":
    # holding roles back is what made the queue feel like it kept growing — you cleared a row
    # and that company's #2 appeared where it had never been, so a refresh looked like new
    # arrivals. Nothing is withheld now, so a refresh is genuinely the same page; and because
    # round 1 leads, working top-down still hits a different company every row.
    # Per company: its lead first, then the rest in score order (by_co is already sorted).
    seq_of = {cn: [lead_of[cn]] + [r for r in by_co[cn] if r is not lead_of[cn]]
              for cn in companies}
    picked: list[dict] = []
    for depth in range(max(len(s) for s in seq_of.values()) if seq_of else 0):
        picked.extend(seq_of[cn][depth] for cn in companies if depth < len(seq_of[cn]))
    for r in picked:
        kind = role_kind(r.get("title"))
        r["role_kind"] = kind
        r["pool_size"] = len(by_co[r["company_norm"]])
        r["bucket"] = kind

    lead = picked[:round_size]
    info = {
        "total": round_size, "shown": len(picked), "eligible": len(eligible),
        "candidates": len(candidates), "companies": n_co,
        "rot": rot, "rounds": rounds, "round_of": (rot % rounds) + 1,
        "next_rot": rot + 1,
        # what the LEADING round looks like by flavour - the readout that tells you whether
        # the tie-break is buying variety or you are sending 50 identical bets.
        "buckets": {k: sum(1 for r in lead if r.get("role_kind") == k)
                    for k in sorted({r.get("role_kind") for r in lead})},
        "blocked_companies": blocked,
        "retired": sorted([s["company"] for s in stock.values() if s["retired"]]),
    }
    return picked, info
