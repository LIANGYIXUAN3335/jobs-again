"""The two ways a discovery report lies to you, pinned so they cannot come back.

Both were real: the first draft charged a company's onboarding inventory to that day's hiring
(so adding companies looked like the market heating up), and it trended flow across onboarding
days (so 40→79/day read as "rising" when the early days had no measurable flow at all)."""
import datetime

from app import discovery, store
from app.config import norm_key, norm_title


def role_row(job_id, company, first_seen, verdict="consider", score=70.0):
    base = {c: None for c in store.ROLE_COLS}
    base.update({
        "job_id": job_id, "ats": "greenhouse", "company": company,
        "company_norm": norm_key(company), "title": "Backend Engineer",
        "title_norm": norm_title("Backend Engineer"), "location": "Remote, United States",
        "is_remote": 1, "seniority": "mid", "apply_url": "https://x/" + job_id,
        "jd_text": "jd", "jd_hash": "h" + job_id, "first_seen": first_seen,
        "last_seen": first_seen, "weighted_score": score, "verdict": verdict,
        "scored_at": "t", "updated_at": "t",
    })
    return base


def _days_ago(n):
    return (datetime.date.fromisoformat(store.la_now_iso()[:10])
            - datetime.timedelta(days=n)).isoformat()


def _seed(cfg, rows, run_days):
    conn = store.connect_jobs(cfg)
    store.upsert_roles(conn, rows)
    for d in run_days:
        conn.execute("INSERT INTO scan_runs (run_at, mode, n_fetched, n_kept, n_new, n_errors)"
                     " VALUES (?, 'local', 0, 0, 0, 0)", (d + "T09:00:00-07:00",))
    conn.commit()
    conn.close()
    return store.connect_view(cfg)


def test_onboarding_inventory_is_backfill_not_flow(cfg):
    """Day 3 is Acme's first day: all 5 of its openings are inventory, not that day's hiring.
    Day 2's single new Acme role is flow, because Acme was already in the corpus."""
    d3, d2 = _days_ago(3), _days_ago(2)
    rows = [role_row(f"gh:acme:{i}", "Acme", d3) for i in range(5)]
    rows.append(role_row("gh:acme:new", "Acme", d2))
    conn = _seed(cfg, rows, [d3, d2])
    try:
        by_date = {x["date"]: x for x in discovery.collect(conn, cfg, days=5)["days"]}
        assert (by_date[d3]["backfill"], by_date[d3]["flow"]) == (5, 0)
        assert (by_date[d2]["backfill"], by_date[d2]["flow"]) == (0, 1)
    finally:
        conn.close()


def test_trend_refuses_onboarding_days_instead_of_averaging_them(cfg):
    """An onboarding day's flow is unobservable, not low. It must not enter the trend, and with
    too few clean days left the report declines to call a direction at all."""
    days = [_days_ago(n) for n in range(6, 0, -1)]
    rows = [role_row(f"gh:a:{i}", "Acme", days[0]) for i in range(3)]        # onboarding day
    rows += [role_row(f"gh:b:{i}", "Beta", days[2]) for i in range(3)]       # onboarding day
    rows += [role_row("gh:a:x", "Acme", days[1]), role_row("gh:a:y", "Beta", days[4])]
    conn = _seed(cfg, rows, days)
    try:
        t = discovery.collect(conn, cfg, days=8)["trend"]
        assert t["n"] == 4, "only the 4 days with no onboarding are trendable"
        assert "recent" not in t, "fewer clean days than _MIN_DAYS must report no direction"
    finally:
        conn.close()


def test_dropped_and_applied_roles_are_not_counted_as_reviewable(cfg):
    """`new` counts what the scanner found; `reviewable` counts what can still reach the tab.
    Conflating them is what makes a healthy pipeline look empty."""
    d = _days_ago(1)
    rows = [role_row("gh:c:1", "Cee", d), role_row("gh:c:2", "Cee", d, verdict="drop")]
    conn = _seed(cfg, rows, [d])
    try:
        day = {x["date"]: x for x in discovery.collect(conn, cfg, days=3)["days"]}[d]
        assert day["new"] == 2 and day["reviewable"] == 1
    finally:
        conn.close()


def test_cap_backlog_is_reported_as_held_supply(cfg):
    """20 reviewable roles at one company under a cap of 15 means 5 are invisible — the number
    that explains a quiet review tab while discovery is fine."""
    d = _days_ago(1)
    cfg = {**cfg, "per_company_cap": {"max": 15, "window_days": 7}}
    conn = _seed(cfg, [role_row(f"gh:big:{i}", "BigCo", d) for i in range(20)], [d])
    try:
        o = discovery.collect(conn, cfg, days=3)
        assert o["reviewable_total"] == 20
        assert o["visible_total"] == 15
        assert o["held"]["BigCo"] == 5
    finally:
        conn.close()
