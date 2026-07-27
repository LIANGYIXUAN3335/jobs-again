"""Diversify: the bounded exploration bonus. One wildcard per never-applied low-mean company,
rank-only, self-retiring on first application."""
from app import scoring, store
from tests.test_store import role_row


def _seed(cfg, *, company, scores, applied_one=False):
    conn = store.connect_view(cfg)
    jids = []
    for i, sc in enumerate(scores):
        jid = f"gh:{company.lower()}:{i}"
        conn.execute("INSERT OR REPLACE INTO jobs.roles (" + ", ".join(store.ROLE_COLS) + ") VALUES ("
                     + ", ".join(f":{c}" for c in store.ROLE_COLS) + ")",
                     role_row(job_id=jid, company=company, title=f"Backend Engineer {i}", score=sc))
        jids.append(jid)
    if applied_one:
        store.upsert_state(conn, jids[0], status="applied", applied_date="2026-07-01")
    conn.commit()
    return conn


def _cfg(cfg, **over):
    c = dict(cfg)
    c["scoring"] = dict(c.get("scoring") or {})
    c["scoring"]["diversify"] = {"enabled": True, "bonus": 15, "max_picks": 6,
                                 "company_mean_below": 62, "min_role_score": 50, **over}
    return c


def _picks(conn, cfg):
    d = store.fetch_view(conn, cfg, status="new")
    return d["diversify_picks"], d


def test_never_applied_low_mean_company_gets_one_boosted_wildcard(cfg):
    cfg = _cfg(cfg)
    conn = _seed(cfg, company="Tesla", scores=[52.0, 58.0, 55.0])
    picks, d = _picks(conn, cfg)
    assert [p["company"] for p in picks] == ["Tesla"]
    assert picks[0]["score"] == 58.0                      # the company's BEST role, only that one
    boosted = [r for r in d["rows"] if r.get("diversify")]
    assert len(boosted) == 1
    assert boosted[0]["rank_score"] >= 58.0 + 15
    # rank moved; the stored score did not
    assert boosted[0]["weighted_score"] == 58.0


def test_first_application_retires_the_subsidy(cfg):
    """Her design's elegant half: the bonus buys exactly one probe per company."""
    cfg = _cfg(cfg)
    conn = _seed(cfg, company="Tesla", scores=[52.0, 58.0], applied_one=True)
    picks, _ = _picks(conn, cfg)
    assert picks == []


def test_high_mean_companies_are_not_subsidised(cfg):
    cfg = _cfg(cfg)
    conn = _seed(cfg, company="Stripe", scores=[75.0, 80.0])
    picks, _ = _picks(conn, cfg)
    assert picks == []


def test_junk_is_not_exploration(cfg):
    """A wildcard below min_role_score would be an application she can't win — no bonus."""
    cfg = _cfg(cfg)
    conn = _seed(cfg, company="Grim", scores=[41.0, 44.0])
    picks, _ = _picks(conn, cfg)
    assert picks == []


def test_exploration_budget_is_capped_and_worst_priced_go_first(cfg):
    cfg = _cfg(cfg, max_picks=2)
    conn = None
    for co, sc in [("A", 61.0), ("B", 60.0), ("C", 59.0), ("D", 58.0)]:
        conn = _seed(cfg, company=co, scores=[sc, sc - 5])
    picks, _ = _picks(conn, cfg)
    assert len(picks) == 2
    # LOWEST company mean first — the probe with the most information about mispriced priors,
    # and the owner's literal ask ("culture 特别差的也投几个"). D mean 55.5, C 56.5.
    assert [p["company"] for p in picks] == ["D", "C"]


def test_diversify_tuning_never_triggers_a_rescore(cfg):
    """Rank-time knob: changing it must not change config_hash (the auto-rescore trigger)."""
    base = scoring.config_hash(_cfg(cfg, bonus=15))
    assert scoring.config_hash(_cfg(cfg, bonus=25)) == base
    assert scoring.config_hash(_cfg(cfg, max_picks=1)) == base
