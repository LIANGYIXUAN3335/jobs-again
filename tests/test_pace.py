"""投递节奏 (app/pace.py, owner 2026-09-28): a per-workspace switch for how many companies the
review page pushes per day and how many new résumés autotailor makes per day.

The contract these tests pin down:
  * sprint = the page exactly as before (no freeze, no cap)
  * a limited level freezes today's top-N companies (one lead role each) from the UNFILTERED
    queue; working a row off uses quota and is never back-filled (2026-07-31 rule)
  * roles landing after the freeze wait for tomorrow
  * raising the level tops up, lowering shows the first N, pause shows nothing
  * low levels auto-revert; custom takes her own numbers; autotailor honours the allowance
"""
import datetime
import json

import pytest

from app import pace, store
from tests.test_store import role_row

TODAY = "2026-09-28"


@pytest.fixture
def conn(cfg, monkeypatch):
    monkeypatch.setattr(store, "la_today", lambda: TODAY)
    c = store.connect_view(cfg)
    jobs = store.connect_jobs(cfg)
    # 6 companies, one role each, scores 90..65 → queue order A,B,C,D,E,F
    store.upsert_roles(jobs, [
        role_row(f"gh:{n.lower()}:1", company=n, title="Backend Engineer", score=s,
                 first_seen="2026-09-20", last_seen=TODAY)
        for n, s in (("Alpha", 90), ("Bravo", 85), ("Charlie", 80), ("Delta", 75),
                     ("Echo", 70), ("Foxtrot", 65))])
    return c


def _scanned_today(cfg):
    store.connect_jobs(cfg).execute("INSERT INTO scan_runs(run_at, mode) VALUES(?, 'scan')",
                                    (TODAY + "T09:15:00-07:00",)).connection.commit()


def _cfg(cfg):
    # budget on (the real queue path), no stock/pace/blocked noise from her real config
    return {**cfg, "application_budget": {"enabled": True, "stock": {"enabled": False},
                                          "pace": {}, "blocked": {"companies": {}},
                                          "daily": {"total": 50, "per_company": 1, "pool": 3}}}


def _view(conn, cfg, **kw):
    return store.fetch_view(conn, _cfg(cfg), status="new", pace=True, **kw)


def _cos(d):
    return [r["company"] for r in d["rows"]]


def test_sprint_is_the_page_as_before(conn, cfg):
    before = store.fetch_view(conn, _cfg(cfg), status="new")
    after = _view(conn, cfg)
    assert _cos(after) == _cos(before) and len(after["rows"]) == 6
    assert not after["pace"]["pace"]["limited"]
    assert conn.execute("SELECT COUNT(*) FROM pace_daily").fetchone()[0] == 0   # sprint never freezes


def test_custom_freezes_top_n_and_does_not_backfill(conn, cfg):
    pace.set_mode(conn, _cfg(cfg), "custom", companies_per_day=2, tailor_per_day=2)
    d = _view(conn, cfg)
    assert _cos(d) == ["Alpha", "Bravo"]
    assert d["pace"]["frozen"] == 2 and d["pace"]["remaining"] == 2
    assert d["counts"]["new"] == 2
    store.upsert_state(conn, "gh:alpha:1", status="applied", applied_date=TODAY)
    d = _view(conn, cfg)
    assert _cos(d) == ["Bravo"]                       # Charlie does NOT slide in
    assert d["pace"]["handled"] == 1 and d["pace"]["remaining"] == 1


def test_freeze_ignores_the_filter_that_was_on_at_first_open(conn, cfg):
    pace.set_mode(conn, _cfg(cfg), "custom", companies_per_day=2)
    d = _view(conn, cfg, q="Echo")                    # first open of the day, filtered to Echo
    assert _cos(d) == []                              # Echo is not in today's top 2
    assert _cos(_view(conn, cfg)) == ["Alpha", "Bravo"]


def test_roles_landing_after_the_freeze_wait_for_tomorrow(conn, cfg, monkeypatch):
    pace.set_mode(conn, _cfg(cfg), "custom", companies_per_day=2)
    _view(conn, cfg)
    store.upsert_roles(store.connect_jobs(cfg), [role_row("gh:zulu:1", company="Zulu", score=99,
                                                          first_seen=TODAY, last_seen=TODAY)])
    assert "Zulu" not in _cos(_view(conn, cfg))
    monkeypatch.setattr(store, "la_today", lambda: "2026-09-29")
    assert _cos(_view(conn, cfg))[0] == "Zulu"        # tomorrow's freeze picks it up first


def test_raise_tops_up_lower_shows_first_n(conn, cfg):
    c = _cfg(cfg)
    pace.set_mode(conn, c, "custom", companies_per_day=2)
    _view(conn, cfg)
    pace.set_mode(conn, c, "custom", companies_per_day=4)
    assert _cos(_view(conn, cfg)) == ["Alpha", "Bravo", "Charlie", "Delta"]
    pace.set_mode(conn, c, "custom", companies_per_day=1)
    assert _cos(_view(conn, cfg)) == ["Alpha"]


def test_pause_shows_nothing(conn, cfg):
    pace.set_mode(conn, _cfg(cfg), "pause")
    d = _view(conn, cfg)
    assert d["rows"] == [] and d["pace"]["quota"] == 0


def test_showall_escape_hatch_is_the_unpaced_view(conn, cfg):
    pace.set_mode(conn, _cfg(cfg), "slow")
    assert len(store.fetch_view(conn, _cfg(cfg), status="new", pace=False)["rows"]) == 6


def test_queue_smaller_than_quota_does_not_recompute_every_view(conn, cfg, monkeypatch):
    pace.set_mode(conn, _cfg(cfg), "custom", companies_per_day=50)
    _view(conn, cfg)
    calls = []
    real = store.fetch_view
    monkeypatch.setattr(store, "fetch_view", lambda *a, **k: calls.append(k) or real(*a, **k))
    store.fetch_view(conn, _cfg(cfg), status="new", pace=True)
    assert len(calls) == 1                            # only the outer call; no unfiltered rebuild


def test_auto_revert_defaults_and_expiry(conn, cfg):
    c = _cfg(cfg)
    e = pace.set_mode(conn, c, "slow")
    assert e["until"] == "2026-10-12"                 # 14 days: back to normal ON 10/12
    assert pace.set_mode(conn, c, "slow", auto_revert=False)["until"] is None
    assert pace.set_mode(conn, c, "sprint", until="2026-10-01")["until"] is None   # never for ≥ target
    assert pace.set_mode(conn, c, "normal")["until"] is None
    assert pace.set_mode(conn, c, "custom", companies_per_day=100)["until"] is None  # more than normal
    assert pace.set_mode(conn, c, "custom", companies_per_day=5)["until"] == "2026-10-12"
    conn.execute("UPDATE pace_settings SET mode='slow', until='2026-09-29'")
    assert pace.effective(conn, c)["mode"] == "slow"             # the day before: still slow
    conn.execute("UPDATE pace_settings SET mode='slow', until='2026-09-28'")
    e = pace.effective(conn, c)                                  # the until day itself: reverted
    assert e["mode"] == "normal" and e["reverted_from"] == "slow" and e["until"] is None


def test_set_mode_rejects_bad_input(conn, cfg):
    c = _cfg(cfg)
    for kw in ({"mode": "turbo"}, {"mode": "slow", "until": "2026-09-01"},
               {"mode": "slow", "until": TODAY},
               {"mode": "custom"}, {"mode": "custom", "companies_per_day": 501},
               {"mode": "custom", "companies_per_day": "ten"}):
        with pytest.raises(ValueError):
            pace.set_mode(conn, c, **kw)


def test_custom_numbers_survive_a_trip_through_a_preset(conn, cfg):
    c = _cfg(cfg)
    pace.set_mode(conn, c, "custom", companies_per_day=7, tailor_per_day=3)
    pace.set_mode(conn, c, "slow")
    custom = next(lv for lv in pace.effective(conn, c)["levels"] if lv["mode"] == "custom")
    assert (custom["companies_per_day"], custom["tailor_per_day"]) == (7, 3)
    assert pace.effective(conn, c)["companies_per_day"] == 10   # slow is still in force


def test_tailor_allowance(conn, cfg):
    c = _cfg(cfg)
    assert pace.tailor_allowance(conn, c) is None      # sprint
    pace.set_mode(conn, c, "custom", companies_per_day=5, tailor_per_day=3)
    pace.log_tailored(conn, ["a", "b"])
    assert pace.tailor_allowance(conn, c) == 1
    pace.log_tailored(conn, ["b", "c", "d"])            # b is not double-counted
    assert pace.tailor_allowance(conn, c) == 0


def test_prep_autotailor_honours_the_allowance(conn, cfg, tmp_path, monkeypatch):
    import tools.prep_autotailor as prep
    c = {**_cfg(cfg), "auto_tailor": {"enabled": True, "batch_cap": 150,
                                      "manifest": str(tmp_path / "m.json")}}
    pace.set_mode(conn, c, "custom", companies_per_day=2, tailor_per_day=3)
    monkeypatch.setattr(prep.cfgmod, "load_config", lambda: c)
    monkeypatch.setattr(prep.tailoring, "write_prompt", lambda cfg, conn, jid: tmp_path / f"{jid}.md")
    monkeypatch.setattr(prep.tailoring, "resume_is_stale", lambda *a: False)
    prep.main()                                        # 08:20 LinkedIn-chained run: no scan yet
    assert json.loads((tmp_path / "m.json").read_text()) == []
    assert not pace.has_freeze(conn)                   # and it never froze the day's list
    _scanned_today(cfg)
    prep.main()
    m = json.loads((tmp_path / "m.json").read_text())
    # today's two first, then the next company in queue order; capped at 3
    assert [x["company"] for x in m] == ["Alpha", "Bravo", "Charlie"]
    assert not pace.has_freeze(conn)                   # the prep reads, the page freezes
    prep.main()                                        # a later scan round the same day
    assert json.loads((tmp_path / "m.json").read_text()) == []


def test_prep_autotailor_pause_queues_nothing(conn, cfg, tmp_path, monkeypatch):
    import tools.prep_autotailor as prep
    c = {**_cfg(cfg), "auto_tailor": {"enabled": True, "batch_cap": 150,
                                      "manifest": str(tmp_path / "m.json")}}
    pace.set_mode(conn, c, "pause")
    monkeypatch.setattr(prep.cfgmod, "load_config", lambda: c)
    monkeypatch.setattr(prep.tailoring, "write_prompt", lambda cfg, conn, jid: tmp_path / f"{jid}.md")
    prep.main()
    assert json.loads((tmp_path / "m.json").read_text()) == []


def test_api_roundtrip(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    from app import web as webmod
    monkeypatch.setattr(store, "la_today", lambda: TODAY)
    monkeypatch.setattr(webmod, "cfg", _cfg(cfg))
    client = TestClient(webmod.app)
    assert client.get("/api/pace").json()["mode"] == "sprint"
    r = client.post("/api/pace", json={"mode": "custom", "companies_per_day": 12})
    assert r.status_code == 200 and r.json()["companies_per_day"] == 12
    assert r.json()["tailor_per_day"] == 12             # defaults to the same number
    assert client.post("/api/pace", json={"mode": "slow", "until": "2020-01-01"}).status_code == 422
    html = client.get("/settings").text
    assert "投递节奏" in html and "pace-btn on" in html


def test_review_page_banner_and_showall_way_back(conn, cfg, monkeypatch):
    from fastapi.testclient import TestClient
    from app import web as webmod
    c = _cfg(cfg)
    monkeypatch.setattr(webmod, "cfg", c)
    pace.set_mode(conn, c, "custom", companies_per_day=2)
    client = TestClient(webmod.app)
    page = client.get("/?status=new").text
    assert "投递节奏" in page and "还剩 <b>2</b>" in page and "showall=1" in page
    page = client.get("/?status=new&showall=1").text
    assert "正在看<b>全部</b>" in page and "回到今天的名单" in page


def test_rows_that_leave_the_queue_any_way_count_as_done(conn, cfg):
    c = _cfg(cfg)
    pace.set_mode(conn, c, "custom", companies_per_day=3)
    _view(conn, cfg)
    store.upsert_state(conn, "gh:alpha:1", status="applied", applied_date=TODAY)
    store.set_company_cooldown(conn, "Bravo", days=30)            # the row's 🚫, no status change
    store.add_applied_company(conn, "Charlie")                     # hidden company
    d = _view(conn, cfg)
    assert d["rows"] == []
    assert d["pace"]["remaining"] == 0 and d["pace"]["handled"] == 3
    assert "Delta" not in _cos(d)                                  # still no back-fill


def test_filter_hint_only_when_a_filter_narrows(conn, cfg):
    pace.set_mode(conn, _cfg(cfg), "custom", companies_per_day=2)
    d = _view(conn, cfg)
    assert d["pace"]["shown"] == d["pace"]["remaining"] == 2
    d = _view(conn, cfg, q="Alpha")
    assert d["pace"]["shown"] == 1 and d["pace"]["remaining"] == 2


def test_nav_badge_on_other_tabs_matches_the_paced_review_tab(conn, cfg):
    c = _cfg(cfg)
    pace.set_mode(conn, c, "custom", companies_per_day=2)
    applied = store.fetch_view(conn, c, status="applied", pace=True)
    assert applied["counts"]["new"] == 2
    assert not pace.has_freeze(conn)                   # another tab never creates the freeze
    _view(conn, cfg)
    store.upsert_state(conn, "gh:alpha:1", status="applied", applied_date=TODAY)
    assert store.fetch_view(conn, c, status="applied", pace=True)["counts"]["new"] == 1


def test_frozen_count_is_capped_at_the_level_in_force(conn, cfg):
    c = _cfg(cfg)
    pace.set_mode(conn, c, "custom", companies_per_day=4)
    _view(conn, cfg)
    pace.set_mode(conn, c, "custom", companies_per_day=2)
    assert pace.frozen_count(conn, c) == 2
    pace.set_mode(conn, c, "pause")
    assert pace.frozen_count(conn, c) == 0


def test_concurrent_first_freeze_does_not_merge_two_lists(conn, cfg):
    c = _cfg(cfg)
    pace.set_mode(conn, c, "custom", companies_per_day=2)
    other = store.connect_view(c)

    def ordered_rows_with_a_racing_freezer():
        # while "we" build our order, another process freezes a DIFFERENT list and commits
        pace.freeze_today(other, c, 2, lambda: [
            {"job_id": "gh:charlie:1", "company_norm": "charlie"},
            {"job_id": "gh:delta:1", "company_norm": "delta"}], TODAY)
        return [{"job_id": "gh:alpha:1", "company_norm": "alpha"},
                {"job_id": "gh:bravo:1", "company_norm": "bravo"}]

    got = pace.freeze_today(conn, c, 2, ordered_rows_with_a_racing_freezer, TODAY)
    assert [f["company_norm"] for f in got] == ["charlie", "delta"]   # theirs, not a merge
    assert conn.execute("SELECT COUNT(*) FROM pace_daily WHERE kind='shown'").fetchone()[0] == 2


def test_one_click_tailor_on_showall_page_covers_the_whole_list(cfg, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app import web as webmod
    monkeypatch.setattr(store, "la_today", lambda: TODAY)
    c = {**_cfg(cfg), "auto_tailor": {"manifest": str(tmp_path / "m.json"), "manual_batch_cap": 150}}
    monkeypatch.setattr(webmod, "cfg", c)
    conn = store.connect_view(c)
    store.upsert_roles(store.connect_jobs(c), [
        role_row(f"gh:{n.lower()}:1", company=n, score=s, first_seen="2026-09-20", last_seen=TODAY)
        for n, s in (("Alpha", 90), ("Bravo", 85), ("Charlie", 80))])
    pace.set_mode(conn, c, "custom", companies_per_day=1)
    client = TestClient(webmod.app)
    body = {"dry_run": True, "status": "new"}
    assert client.post("/api/tailor-batch", json=body).json()["queued"] == 1
    assert client.post("/api/tailor-batch", json={**body, "showall": "1"}).json()["queued"] == 3
    import pathlib
    js = pathlib.Path(webmod.__file__).parent.parent.joinpath("static/app.js").read_text()
    assert '"showall"' in js.split("BATCH_FILTER_KEYS")[1].split(";")[0]
