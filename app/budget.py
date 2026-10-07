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

  ④ DEPTH  — OFF by default (`depth.enabled: false`). The floor under ①: once a company is
             worth applying to at all, queue 4-10 of its reqs back to back instead of one
             (`target` 6, and ① stock is still the hard ceiling above it).
             ① caps the top, ③ spreads across companies, and nothing until now pushed a
             company UP off 1. See the evidence block on `depth_plan` below and the
             matching comment in config.yaml. This is a strategy change to her job search,
             not a bug fix, so it ships dark and she turns it on.
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
    # Owner 2026-09-22: `enabled: false` switches the cap OFF. After the 9/3–9/16 application
    # burst it had retired 62 companies and hidden 484 candidate roles, and none of those
    # companies had actually refused an application — the ones that do go in `cooldowns`.
    # An empty map is what every consumer already reads as "nobody is capped"; pace and
    # blocked are separate knobs and keep working.
    if not b.get("enabled", True):
        return {}
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
        if r.get("duplicate_of"):
            continue    # li: shadow of a linked pair — the canonical ATS row already spends the slot
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
                  if r["company_norm"] == cn and (r.get("applied_date") or "") >= since
                  and not r.get("duplicate_of")]   # linked pair = one application, not two
        if len(recent) >= limit:
            oldest = min(r["applied_date"] for r in recent)
            until = (datetime.date.fromisoformat(oldest)
                     + datetime.timedelta(days=window)).isoformat()
            out[cn] = {"blocked": True, "until": until, "limit": limit,
                       "window_days": window, "recent": len(recent),
                       "reason": rule.get("reason", "")}
    return out


def depth_enabled(cfg: dict) -> bool:
    return bool((_cfg(cfg).get("depth") or {}).get("enabled"))


def depth_plan(by_co: dict[str, list[dict]], stock: dict[str, dict],
               cfg: dict) -> dict[str, int]:
    """{company_norm: how many of its reqs to queue back-to-back today}. Empty when OFF.

    EVIDENCE (one-off conversion analysis, 840 applications / 246 companies, 2026-07-18 → 08-09;
    the reporting tool was removed 2026-08-12 at the owner's request — numbers kept for the record,
    replicated independently 2026-08-10 against both DBs read-only):

        applied to exactly 1 req there   112 companies    0 replies
        applied to 4-15 reqs there        61 companies    7 replies   (11.5%)
        applied to 16+ reqs there          5 companies    0 replies
        Fisher exact, 1 vs 4-15:  p = 0.0005   (reproduced: p = 0.000536)

    Every other cut tested flat: weighted_score, posting freshness, tailored-vs-base résumé
    (p = 0.74). Same-company volume is the only slice that moved.

    CONFOUNDER WE COULD NOT RULE OUT — read this before trusting the number.
    How many reqs a company HAS is not independent of how many she applied to, and supply
    predicts replies on its own: companies with >= 16 eligible reqs replied 6/56 (10.7%),
    companies with fewer replied 2/190 (1.1%), p = 0.002. Controlling for it costs most of
    the significance:
        restricted to companies where >= 4 eligible reqs existed:  0/29 vs 7/61,  p = 0.092
        restricted to companies where >= 16 existed:  applied 1-3 0/16 vs 4-15 6/35, p = 0.16
        stratified permutation over supply bands (200k draws): one-sided p = 0.043
    So "depth causes replies" and "companies that are hiring hard both post more reqs AND
    answer more mail" fit the data equally well. 66 of those 112 one-application companies
    only ever had ONE eligible req — she could not have gone deeper there, and this knob
    deliberately does not try to. That is why eligibility below is `can it REACH the floor`
    and not `did she stop at 1`.
    Both causal stories recommend the same move, which is the only reason this ships at all:
    under the first, depth does the work; under the second, picking req-rich companies does.
    What neither story supports is spending a slot at a company that has one job open.

    Honest counterexample kept in view: Maincode replied after 2 applications and only ever
    had 2 eligible reqs, so this knob would have ranked it below the focus block. It is not
    excluded — nothing is excluded, see select_daily — it just does not lead.

    The four conditions a company must clear to get depth today:
      reachable   used + min(qualified reqs, stock left) >= `floor`. A company that cannot
                  get to 4 is not made to look like it can.
      below band  used < `floor`. At 4 or more it is ALREADY inside the sweet spot the data
                  found, and topping it up 5 → 6 buys nothing measurable — the finding is
                  about 1 vs 4-15, not about 5 vs 6. This is why the knob is a floor and not
                  a second cap: it pushes companies UP to the band and then lets go, and ①
                  stock is what still stops them at the top.
      wanted      the company's best weighted_score >= `min_company_score`. Company-level and
                  deliberately low: the score is a "do I want to go there" filter, and the
                  funnel showed it has no measurable power to predict who answers (85+ scored
                  roles: 41 applications, 0 replies; the 8 companies that DID answer scored
                  48-81). A high bar here would just re-run the `auto_tailor.min_score`
                  mistake one layer down.
      not junk    a queued req may not sit more than `max_score_drop` below that company's
                  own best. `daily.pool` already caps how deep ROTATION reaches into a
                  company for exactly this reason (median within-company spread 14.2, and
                  Snowflake's eligible roles run 70.9 down to 32.6); depth needs the same
                  guard because it reaches further than pool does.

    Note this never gates ELIGIBILITY — the module rule at the top ("the score is never a
    gate") still holds. Every role select_daily was handed is still returned; the score only
    decides how many of a company's reqs get queued consecutively at the head of the page.
    """
    d = _cfg(cfg).get("depth") or {}
    if not d.get("enabled"):
        return {}
    target = max(1, int(d.get("target", 6)))
    floor_ = max(1, int(d.get("floor", 4)))
    bar = float(d.get("min_company_score", 55))
    drop = float(d.get("max_score_drop", 15))

    plan: dict[str, int] = {}
    for cn, pool in by_co.items():
        st = stock.get(cn) or {}
        used = int(st.get("used", 0))
        left = int(st.get("left", len(pool)))
        if used >= floor_ or left <= 0:
            continue
        best = max((r.get("weighted_score") or 0) for r in pool)
        if best < bar:
            continue
        ok = sum(1 for r in pool if (r.get("weighted_score") or 0) >= best - drop)
        if used + min(ok, left) < floor_:
            continue
        want = min(target - used, left, ok)
        if want > 0:
            plan[cn] = want
    return plan


def _depth_rows(seq: list[dict], n: int, drop: float) -> list[dict]:
    """The first `n` of a company's reqs that are within `drop` of its own best."""
    best = max((r.get("weighted_score") or 0) for r in seq)
    return [r for r in seq if (r.get("weighted_score") or 0) >= best - drop][:n]


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

    # --- ④ DEPTH (off by default; evidence and the confounder are on depth_plan). ---------
    # A pure RE-ORDER of `picked`, never a filter: same rows, same count, same job_ids. The
    # 2026-07-31 rule that nothing may be withheld is the reason it is written this way — a
    # depth floor implemented by holding roles back would bring straight back the bug that
    # rule was written for (clear a row, a company's #2 appears where it had never been, and
    # a refresh reads as new arrivals). With the flag off this block does not run at all, so
    # `picked` is byte-identical to what it was before this knob existed.
    #
    # Why re-ordering is enough, and why nothing here needs app/store.py: the 1-application
    # dead zone is not manufactured by withholding, it is manufactured by POSITION. With ~410
    # eligible companies interleaved one-deep, a company's #2 sits ~410 rows below its #1 and
    # she works top-down, so 50 applications = 50 companies × 1. Moving those reqs up is the
    # whole intervention.
    depth_info: dict = {}
    plan = depth_plan(by_co, stock, cfg)
    if plan:
        d = _cfg(cfg).get("depth") or {}
        target = max(1, int(d.get("target", 6)))
        drop = float(d.get("max_score_drop", 15))
        # Ring order: CLOSEST TO THE FLOOR FIRST — a company already at 3 leads an untouched
        # one, because 3 more applications rescue it and 6 are needed to build the other from
        # scratch. That is the cheaper half of the same finding: 68 companies sat parked at
        # 2-3 applications (159 applications, 1 reply), one short push from the 4-15 band,
        # while 112 sat at 1. Ties fall back to the ring's existing rule (best role first),
        # then name, so the order is total and deterministic. Built from the UNROTATED list
        # on purpose: `rot` rotates this ring by its own step below, and inheriting the
        # tail's 50-company step would scramble the ordering on the first click.
        focus = sorted(plan, key=lambda cn: (plan[cn], -(by_co[cn][0].get("rank_score") or 0), cn))
        # 「换一批」 must advance by a SCREEN OF WORK, not by `round_size` companies — at ~5
        # reqs each a 50-row screen holds ~10 of them, and stepping 50 would skip 40 she
        # never saw. The step is counted off the unrotated ring so it is the same integer at
        # every `rot`; otherwise the cursor advances by a different amount each click and
        # coverage stops being provable.
        per_round, acc = 0, 0
        for cn in focus:
            if acc >= round_size:
                break
            acc += plan[cn]
            per_round += 1
        per_round = max(1, per_round)
        n_f = len(focus)
        focus = focus[(rot * per_round) % n_f:] + focus[:(rot * per_round) % n_f]

        head: list[dict] = []
        for cn in focus:
            head.extend(_depth_rows(seq_of[cn], plan[cn], drop))
        seen_ids = {r["job_id"] for r in head}
        picked = head + [r for r in picked if r["job_id"] not in seen_ids]
        d_rounds = max(1, -(-n_f // per_round))
        depth_info = {
            "target": target, "per_round": per_round, "companies": n_f,
            "rounds": d_rounds, "round_of": (rot % d_rounds) + 1,
            "reqs": sum(plan.values()),
            # what the first screen actually asks her to do, company by company
            "lead": [{"company": by_co[cn][0]["company"], "reqs": plan[cn],
                      "already": (stock.get(cn) or {}).get("used", 0)}
                     for cn in focus[:per_round]],
        }

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
    # Only present when the depth floor is ON, so the flag-off `info` is unchanged too.
    if depth_info:
        info["depth"] = depth_info
    return picked, info
