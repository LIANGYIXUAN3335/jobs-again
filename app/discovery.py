"""How many genuinely new roles turn up each day, and how many of them you actually get to see.

"Fewer jobs lately" is two different claims wearing one coat, and they have opposite fixes:

  discovery — how many new roles the scanner finds.  Falls when the company list stops growing.
  supply    — how many of those reach the review tab.  Falls when `per_company_cap` holds them
              back, which happens more as the corpus fills up, even while discovery is fine.

Reported apart, because conflating them sends you off to debug a scraper that isn't broken.

The one distinction that makes discovery readable is backfill vs flow. The first scan of a
company counts its ENTIRE open-req inventory as new — a one-time windfall that has nothing to
do with hiring activity. Every day after that you see only what was genuinely posted. Add ten
companies and the daily number leaps; that is not the pipeline improving. So a role is charged
to `backfill` when its first_seen lands on the day its company entered the corpus, and to
`flow` otherwise. Only `flow` is worth trending.

Honesty rules:
  - A day with no scan run is printed as "no scan", never as a zero. Nobody posting and nobody
    looking produce the same number and must not look the same.
  - Today is marked partial. It is compared against nothing until it is over.
  - A trend over fewer than `_MIN_DAYS` full flow-days is refused, not softened.

Read-only: opens the jobs DB through the standard view connection and writes no rows. Safe to
run while a scan is in flight (both DBs are WAL).

    python -m app.discovery                 # last 14 days
    python -m app.discovery --days 30
    python -m app.discovery --by company    # which companies actually still post
"""
from __future__ import annotations

import argparse
import datetime
import statistics

from . import config as cfgmod
from . import store

# Below this many complete flow-days, "is it trending down?" has no answer worth printing.
_MIN_DAYS = 6


def _rows(conn) -> list[dict]:
    """Every role, plus the review-relevant state. Mirrors the subset of store.fetch_view's
    in_review test that can be evaluated in aggregate — see `_IN_REVIEW_CAVEAT`."""
    return [dict(r) for r in conn.execute(
        "SELECT r.job_id, r.company, r.company_norm, r.first_seen, r.verdict,"
        "       COALESCE(s.status, 'new') AS status"
        "  FROM jobs.roles r LEFT JOIN role_state s ON s.job_id = r.job_id"
    )]


_IN_REVIEW_CAVEAT = ("reviewable = status new, verdict not drop, company not already applied to "
                     "and not in cooldown. Excludes the signature-dedup hide, so it reads a "
                     "little high.")


def collect(conn, cfg: dict, days: int) -> dict:
    rows = _rows(conn)
    today = store.la_now_iso()[:10]
    start = (datetime.date.fromisoformat(today) - datetime.timedelta(days=days - 1)).isoformat()

    applied_cos = {r["name_norm"] for r in conn.execute("SELECT name_norm FROM applied_companies")}
    cooldown_cos = set(cfgmod.active_cooldowns(cfg))

    def reviewable(r: dict) -> bool:
        return (r["status"] == "new" and r["verdict"] != "drop"
                and r["company_norm"] not in applied_cos
                and r["company_norm"] not in cooldown_cos)

    # The day a company entered the corpus. Roles first seen that day are its inventory
    # backfill, not that day's hiring.
    entered: dict[str, str] = {}
    for r in rows:
        d = (r["first_seen"] or "")[:10]
        if d and (r["company_norm"] not in entered or d < entered[r["company_norm"]]):
            entered[r["company_norm"]] = d

    runs: dict[str, int] = {}
    for (ts,) in conn.execute("SELECT run_at FROM jobs.scan_runs"):
        runs[(ts or "")[:10]] = runs.get((ts or "")[:10], 0) + 1

    by_day: dict[str, dict] = {}
    for r in rows:
        d = (r["first_seen"] or "")[:10]
        if not d or d < start:
            continue
        e = by_day.setdefault(d, {"new": 0, "backfill": 0, "flow": 0, "reviewable": 0})
        e["new"] += 1
        e["backfill" if entered.get(r["company_norm"]) == d else "flow"] += 1
        if reviewable(r):
            e["reviewable"] += 1

    days_out = []
    d = datetime.date.fromisoformat(start)
    end = datetime.date.fromisoformat(today)
    while d <= end:
        k = d.isoformat()
        e = by_day.get(k) or {"new": 0, "backfill": 0, "flow": 0, "reviewable": 0}
        days_out.append({"date": k, "runs": runs.get(k, 0), "partial": k == today, **e})
        d += datetime.timedelta(days=1)

    # Only a complete, scanned day with NO onboarding can carry a flow trend. On a day when a
    # company joins, every one of its openings is charged to backfill, so that day's flow is
    # measured over a corpus that did not yet include it — a low number there means "not
    # observable", not "nobody posted". Trending those together invents a slope.
    clean = [x for x in days_out if x["runs"] and not x["partial"] and not x["backfill"]]
    flows = [x["flow"] for x in clean]
    trend: dict = {"n": len(flows), "need": _MIN_DAYS}
    if len(flows) >= _MIN_DAYS:
        half = len(flows) // 2
        trend.update(median=statistics.median(flows),
                     older=statistics.mean(flows[:half]),
                     recent=statistics.mean(flows[-half:]))

    # --- supply: what the per-company cap is currently holding back ---
    cap_cfg = cfg.get("per_company_cap") or {}
    cap_max = int(cap_cfg.get("max", 0) or 0)
    pend: dict[str, int] = {}
    for r in rows:
        if reviewable(r):
            pend[r["company"]] = pend.get(r["company"], 0) + 1
    held = {c: n - cap_max for c, n in pend.items() if cap_max and n > cap_max}

    return {"days": days_out, "trend": trend, "today": today,
            "cap_max": cap_max, "window_days": int(cap_cfg.get("window_days", 7)),
            "pending": pend, "held": held,
            "reviewable_total": sum(pend.values()),
            "visible_total": sum(min(n, cap_max) if cap_max else n for n in pend.values())}


def by_company(conn, cfg: dict, days: int, limit: int = 20) -> list[dict]:
    """Flow only — who is genuinely still posting, ignoring the day they were onboarded."""
    rows = _rows(conn)
    today = store.la_now_iso()[:10]
    start = (datetime.date.fromisoformat(today) - datetime.timedelta(days=days - 1)).isoformat()
    entered: dict[str, str] = {}
    for r in rows:
        d = (r["first_seen"] or "")[:10]
        if d and (r["company_norm"] not in entered or d < entered[r["company_norm"]]):
            entered[r["company_norm"]] = d
    agg: dict[str, dict] = {}
    for r in rows:
        d = (r["first_seen"] or "")[:10]
        if not d or d < start:
            continue
        a = agg.setdefault(r["company"], {"company": r["company"], "flow": 0, "backfill": 0})
        a["backfill" if entered.get(r["company_norm"]) == d else "flow"] += 1
    out = sorted(agg.values(), key=lambda a: (-a["flow"], -a["backfill"]))
    return out[:limit]


def report(conn, cfg: dict, days: int, dims: str) -> None:
    o = collect(conn, cfg, days)

    print(f"\ndiscovery · last {days} days                              (today {o['today']})")
    print(f"  {'date':12} {'scans':>5} {'new':>6} {'backfill':>9} {'flow':>6} {'reviewable':>11}")
    for x in o["days"]:
        if not x["runs"] and not x["new"]:
            print(f"  {x['date']:12} {'—':>5} {'':>6} {'':>9} {'':>6} {'':>11}   no scan")
            continue
        note = "  ← partial, today is not over" if x["partial"] else (
            "  ← new companies onboarded" if x["backfill"] else "")
        runs = x["runs"] or "—"
        print(f"  {x['date']:12} {runs:>5} {x['new']:6d} {x['backfill']:9d} {x['flow']:6d} "
              f"{x['reviewable']:11d}{note}")
    print(f"\n  backfill = a company's existing openings on the day it entered the corpus "
          f"(one-time, not hiring activity).\n  {_IN_REVIEW_CAVEAT}")

    t = o["trend"]
    print("\nis flow actually falling?")
    if t["n"] < t["need"]:
        print(f"  not enough data yet — {t['n']} clean days (scanned, complete, no company "
              f"onboarded), need {t['need']}. No trend reported.")
    else:
        delta = t["recent"] - t["older"]
        word = "falling" if delta < -1 else ("rising" if delta > 1 else "flat")
        print(f"  {word}: earlier half {t['older']:.1f}/day vs recent half {t['recent']:.1f}/day "
              f"(median {t['median']:.0f}) over {t['n']} clean days")
        print("  raising flow means adding companies — it is bounded by who you track, "
              "not by scraper health.")

    print(f"\nsupply · what the review tab can show (cap {o['cap_max']}/company "
          f"per {o['window_days']}d)")
    print(f"  {o['reviewable_total']} reviewable roles → at most {o['visible_total']} visible "
          f"({o['reviewable_total'] - o['visible_total']} held by the cap)")
    if o["held"]:
        print(f"  {'company':24} {'pending':>8} {'held':>6}")
        for c, h in sorted(o["held"].items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {c[:24]:24} {o['pending'][c]:8d} {h:6d}")
        print("  a company at its cap goes quiet in review even when discovery is healthy — "
              "this, not the scanner, is usually why the tab feels empty.")
    else:
        print("  nothing held — every reviewable role is visible.")

    if "company" in dims:
        print(f"\nflow by company · last {days} days (who is genuinely still posting)")
        print(f"  {'company':24} {'flow':>6} {'backfill':>9}")
        for a in by_company(conn, cfg, days):
            if not a["flow"] and not a["backfill"]:
                continue
            print(f"  {a['company'][:24]:24} {a['flow']:6d} {a['backfill']:9d}")
    print()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=14, help="window to report (default 14)")
    ap.add_argument("--by", default="", help="add breakdowns: company")
    args = ap.parse_args(argv)

    cfg = cfgmod.load_config()
    conn = store.connect_view(cfg)
    try:
        report(conn, cfg, max(2, args.days), args.by)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
