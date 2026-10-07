from pathlib import Path

from app import store
from app.config import norm_key, norm_title


def role_row(job_id="gh:x:1", company="X", title="Backend Engineer", score=70.0,
             first_seen="2026-07-01", last_seen="2026-07-01"):
    base = {c: None for c in store.ROLE_COLS}
    base.update({
        "job_id": job_id, "ats": "greenhouse", "company": company,
        "company_norm": norm_key(company), "title": title, "title_norm": norm_title(title),
        "department": "Engineering", "location": "Remote, United States", "is_remote": 1,
        "seniority": "mid", "apply_url": "https://x/1", "jd_text": "jd", "jd_hash": "h",
        "first_seen": first_seen, "last_seen": last_seen,
        "s_stability": 3.0, "s_culture": 3.0, "s_depth": 4.0, "s_wlb": 3.0,
        "weighted_score": score, "verdict": "consider", "why": "", "caution": "",
        "scored_at": "t", "updated_at": "t",
    })
    return base


def test_upsert_is_idempotent_and_preserves_first_seen(cfg):
    conn = store.connect_jobs(cfg)
    store.upsert_roles(conn, [role_row()])
    store.upsert_roles(conn, [role_row(first_seen="2026-07-02", last_seen="2026-07-02", score=75.0)])
    rows = conn.execute("SELECT * FROM roles").fetchall()
    assert len(rows) == 1
    assert rows[0]["first_seen"] == "2026-07-01"   # never overwritten
    assert rows[0]["last_seen"] == "2026-07-02"    # refreshed
    assert rows[0]["weighted_score"] == 75.0


def test_user_state_lives_in_a_separate_file(cfg):
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [role_row()])
    view = store.connect_view(cfg)
    store.set_status(view, "gh:x:1", "applied", "payments")
    assert Path(cfg["db_path"]) != Path(cfg["user_db_path"])
    # the scanner's file contains no user tables, the user file no role facts
    jobs_tables = {r["name"] for r in jobs.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "role_state" not in jobs_tables
    state = view.execute("SELECT * FROM role_state WHERE job_id='gh:x:1'").fetchone()
    assert state["status"] == "applied"
    assert state["applied_with_resume"] == "payments"
    assert state["applied_date"] == store.la_today()


def test_review_hides_own_status_and_badges_signature_repost(cfg):
    # A signature IS company+title, so this test needs three roles at ONE company. The review
    # queue shows one role per company (application_budget.daily), which would hide two of
    # them for reasons that have nothing to do with what's under test — so the budget is off
    # here and exercised by test_review_rotation_* instead.
    cfg = {**cfg, "application_budget": {"enabled": False}}
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("gh:x:1", title="Backend Engineer"),
        role_row("gh:x:2", title="Backend Engineer"),   # repost, new id, same signature
        role_row("gh:x:3", title="Platform Engineer"),
    ])
    view = store.connect_view(cfg)

    data = store.fetch_view(view, cfg)
    assert {r["job_id"] for r in data["rows"]} == {"gh:x:1", "gh:x:2", "gh:x:3"}

    store.set_status(view, "gh:x:1", "applied")
    data = store.fetch_view(view, cfg)                  # default review view
    ids = {r["job_id"] for r in data["rows"]}
    assert "gh:x:1" not in ids                          # own applied → hidden
    badge = next(r for r in data["rows"] if r["job_id"] == "gh:x:2")["badge_ref"]
    assert badge and badge["job_id"] == "gh:x:1"        # repost stays, badged
    assert next(r for r in data["rows"] if r["job_id"] == "gh:x:3")["badge_ref"] is None
    assert data["counts"]["applied"] == 1

    hide_cfg = {**cfg, "signature_match": "hide"}       # spec's original behavior
    ids = {r["job_id"] for r in store.fetch_view(view, hide_cfg)["rows"]}
    assert ids == {"gh:x:3"}


def test_applied_company_hides_all_roles(cfg):
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [role_row("gh:x:1"), role_row("gh:y:9", company="Other")])
    view = store.connect_view(cfg)
    store.add_applied_company(view, "X")
    ids = {r["job_id"] for r in store.fetch_view(view, cfg)["rows"]}
    assert ids == {"gh:y:9"}
    store.remove_applied_company(view, "X")
    assert len(store.fetch_view(view, cfg)["rows"]) == 2


def test_back_to_new_and_notes(cfg):
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [role_row()])
    view = store.connect_view(cfg)
    store.set_status(view, "gh:x:1", "skip")
    store.set_notes(view, "gh:x:1", "talked to a friend there")
    store.set_status(view, "gh:x:1", "new")
    data = store.fetch_view(view, cfg)
    row = next(r for r in data["rows"] if r["job_id"] == "gh:x:1")
    assert row["status"] == "new"
    assert row["notes"] == "talked to a friend there"   # notes survive status churn


def test_fresh_posting_outranks_a_higher_scoring_stale_one(cfg):
    import datetime
    today = store.la_today()
    old = (datetime.date.fromisoformat(today) - datetime.timedelta(days=90)).isoformat()
    jobs = store.connect_jobs(cfg)
    # Different companies: the review queue shows one role per company, and putting both at
    # one company would make this test about queue selection rather than about freshness.
    stale = role_row("gh:x:1", company="X", title="Stale Engineer", score=78.0, first_seen=old)
    stale["posted_at"] = old
    fresh = role_row("gh:y:2", company="Y", title="Fresh Engineer", score=72.0, first_seen=today)
    fresh["posted_at"] = today
    store.upsert_roles(jobs, [stale, fresh])

    rows = store.fetch_view(store.connect_view(cfg), cfg)["rows"]
    by_id = {r["job_id"]: r for r in rows}
    assert by_id["gh:y:2"]["fresh_bonus"] == 10.0
    assert by_id["gh:x:1"]["fresh_bonus"] == 0.0
    # The stale role still scores higher on its own merits — only the ORDER changes.
    assert by_id["gh:x:1"]["weighted_score"] > by_id["gh:y:2"]["weighted_score"]
    assert [r["job_id"] for r in rows] == ["gh:y:2", "gh:x:1"]


# --- review rotation (application_budget.daily, 2026-07-30) -------------------------------
# The queue is one role per company, rotated by `rot`. Three properties are load-bearing and
# each has burned us before: coverage (a shuffle silently starves companies), determinism
# (a refresh that reorders the page makes it unusable), and pool rotation (without it a
# company's #2 role is invisible until #1 is marked, so the company sits there stuck).

def _rot_cfg(cfg, total=2, pool=3, tie=3):
    return {**cfg, "application_budget": {
        "enabled": True, "stock": {"default": 8}, "pace": {},
        "daily": {"total": total, "per_company": 1, "pool": pool, "tie_break": tie}}}


def test_review_interleaves_by_company_and_withholds_nothing(cfg):
    """Owner's call (2026-07-31), replacing 'one role per company, rest held back'.

    Holding roles back is what made the queue feel like it kept growing: you cleared a row and
    that company's #2 appeared where it had never been, so a refresh read as new arrivals.
    Now every eligible role is on the page, ordered round-robin — so the top of the list is
    still one-per-company (work top-down and every row is a different company), but nothing
    is hidden and a refresh really is the same page."""
    c = _rot_cfg(cfg)
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("gh:a:1", company="A", title="Backend Engineer", score=80.0),
        role_row("gh:a:2", company="A", title="Data Engineer", score=79.0),
        role_row("gh:a:3", company="A", title="ML Engineer", score=78.0),
        role_row("gh:b:1", company="B", title="Backend Engineer", score=70.0),
    ])
    rows = store.fetch_view(store.connect_view(cfg), c)["rows"]
    assert len(rows) == 4                                  # nothing withheld
    assert [r["company"] for r in rows[:2]] == ["A", "B"]  # round 1: one each, best first
    assert [r["company"] for r in rows[2:]] == ["A", "A"]  # then A's remaining two
    assert len({r["job_id"] for r in rows}) == 4           # and never the same role twice


def test_rotation_is_deterministic_and_moves_only_with_rot(cfg):
    c = _rot_cfg(cfg)
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row(f"gh:c{i}:1", company=f"C{i}", title="Backend Engineer", score=90.0 - i)
        for i in range(6)])
    view = store.connect_view(cfg)
    first = [r["job_id"] for r in store.fetch_view(view, c, rot=1)["rows"]]
    again = [r["job_id"] for r in store.fetch_view(view, c, rot=1)["rows"]]
    assert first == again                                # refresh never moves the page
    assert first != [r["job_id"] for r in store.fetch_view(view, c, rot=0)["rows"]]


def test_every_company_leads_within_one_full_rotation(cfg):
    """The point of rotating instead of shuffling: coverage is guaranteed, not probable."""
    c = _rot_cfg(cfg, total=2)                            # 2 companies lead per round
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row(f"gh:c{i}:1", company=f"C{i}", title="Backend Engineer", score=90.0 - i)
        for i in range(7)])
    view = store.connect_view(cfg)
    info = store.fetch_view(view, c)["budget"]
    assert info["companies"] == 7 and info["rounds"] == 4   # ceil(7/2)
    led = set()
    for rot in range(info["rounds"]):
        led |= {r["company"] for r in store.fetch_view(view, c, rot=rot)["rows"][:2]}
    assert led == {f"C{i}" for i in range(7)}


def test_rot_surfaces_a_companys_second_role(cfg):
    """Without pool rotation, #2 is unreachable until #1 is marked applied/skip."""
    c = _rot_cfg(cfg, tie=0)                              # tie-break off: pure rotation
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("gh:a:1", company="A", title="Backend Engineer", score=80.0),
        role_row("gh:a:2", company="A", title="Data Engineer", score=60.0),
    ])
    view = store.connect_view(cfg)
    seen = {store.fetch_view(view, c, rot=r)["rows"][0]["job_id"] for r in range(2)}
    assert seen == {"gh:a:1", "gh:a:2"}


def test_pool_caps_how_far_down_a_company_rotation_reaches(cfg):
    """Rotation must not reach a company's genuinely-bad roles: within one company the score
    spread is real (Snowflake's eligible roles run 70.9 down to 32.6)."""
    c = _rot_cfg(cfg, pool=2, tie=0)
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("gh:a:1", company="A", title="Backend Engineer", score=80.0),
        role_row("gh:a:2", company="A", title="Data Engineer", score=78.0),
        role_row("gh:a:3", company="A", title="Sales Engineer", score=35.0),
    ])
    view = store.connect_view(cfg)
    seen = {store.fetch_view(view, c, rot=r)["rows"][0]["job_id"] for r in range(6)}
    assert seen == {"gh:a:1", "gh:a:2"}                  # the 35 never surfaces


def test_tie_break_prefers_an_under_represented_role_kind(cfg):
    """Within the noise band, prefer the flavour this page has used least — otherwise
    'each company's best role' means 'each company's backend role' for every company."""
    c = _rot_cfg(cfg, tie=3)
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("gh:a:1", company="A", title="Backend Engineer", score=90.0),
        role_row("gh:b:1", company="B", title="Backend Engineer", score=80.0),
        role_row("gh:b:2", company="B", title="Data Engineer", score=79.0),   # within 3
    ])
    # Round 1 is the leads — that's where the tie-break applies. B's #2 still appears later
    # in the interleave; what the tie-break decides is which role LEADS for B.
    rows = store.fetch_view(store.connect_view(cfg), c)["rows"]
    assert [r["job_id"] for r in rows[:2]] == ["gh:a:1", "gh:b:2"]
    # ...but a gap wider than the band is a real quality difference and must win.
    jobs.execute("UPDATE roles SET weighted_score=60.0 WHERE job_id='gh:b:2'")
    jobs.commit()
    rows = store.fetch_view(store.connect_view(cfg), c)["rows"]
    assert [r["job_id"] for r in rows[:2]] == ["gh:a:1", "gh:b:1"]


def test_sort_reorders_but_never_changes_which_roles(cfg):
    """Regression: select_daily re-ordered by its own company ring AFTER fetch_view had
    sorted, which silently made the sort dropdown a no-op — all four options returned
    byte-identical lists in the review tab. Sorting must change the ORDER and nothing else."""
    c = _rot_cfg(cfg)
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("gh:a:1", company="Zeta", title="Backend Engineer", score=80.0),
        role_row("gh:b:1", company="Alpha", title="Backend Engineer", score=70.0),
        role_row("gh:b:2", company="Alpha", title="Data Engineer", score=65.0),
    ])
    view = store.connect_view(cfg)
    by = {s: store.fetch_view(view, c, sort=s)["rows"]
          for s in ("diversity", "score", "company")}
    sets = {s: {r["job_id"] for r in rows} for s, rows in by.items()}
    assert sets["diversity"] == sets["score"] == sets["company"]     # same roles…
    assert by["company"][0]["company"] == "Alpha"                    # …different order
    assert by["diversity"][0]["company"] == "Zeta"                   # best company leads


def test_autotailor_takes_one_role_per_company(cfg):
    """The queue lists every eligible role now, so 'tailor whatever the queue shows' would be
    ~1000 résumés a night. The owner's ask is company-level — one lead role per company, which
    the interleave puts first."""
    c = _rot_cfg(cfg)
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("gh:a:1", company="A", title="Backend Engineer", score=80.0),
        role_row("gh:a:2", company="A", title="Data Engineer", score=79.0),
        role_row("gh:b:1", company="B", title="Backend Engineer", score=70.0),
    ])
    rows = store.fetch_view(store.connect_view(cfg), c)["rows"]
    seen: set[str] = set()
    leads = [r for r in rows
             if not (r["company_norm"] in seen or seen.add(r["company_norm"]))]
    assert [r["job_id"] for r in leads] == ["gh:a:1", "gh:b:1"]
    assert len(rows) == 3            # the queue still shows A's second role


# ---- duplicate rows: li: shadow ↔ ATS canonical (2026-08-01) --------------------------------
# LinkedIn discovery and the board scanner both find the same physical posting. The extension
# marks the ATS row (it resolves by the employer page's URL); the UI shows the li: row — so an
# application "didn't stick" from the owner's seat, and funnel/budget counted the pair twice.

def test_marking_either_twin_applied_cascades_to_the_other(cfg):
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("li:1", company="Precisely", title="Associate Software Engineer"),
        role_row("gh:precisely:9", company="Precisely", title="Associate Software Engineer"),
        # same title at ANOTHER company — must never be touched by the cascade
        role_row("gh:other:7", company="Other Co", title="Associate Software Engineer"),
    ])
    view = store.connect_view(cfg)

    store.set_status(view, "gh:precisely:9", "applied", resume_variant="tailored")

    li = view.execute("SELECT * FROM role_state WHERE job_id='li:1'").fetchone()
    assert li["status"] == "applied"                      # cascaded
    assert li["applied_with_resume"] == "tailored"
    assert li["duplicate_of"] == "gh:precisely:9"         # link self-healed, shadow side only
    assert view.execute("SELECT * FROM role_state WHERE job_id='gh:other:7'").fetchone() is None


def test_ambiguous_or_containment_titles_never_link(cfg):
    """Two same-title ATS rows (multi-location) → no safe answer → no link. And 'Software
    Engineer' must not link to 'Senior Software Engineer' — containment cascades an
    application onto a job never applied to."""
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("li:2", company="Gamma", title="Software Engineer"),
        role_row("gh:gamma:1", company="Gamma", title="Software Engineer"),
        role_row("gh:gamma:2", company="Gamma", title="Software Engineer"),   # second candidate
        role_row("li:3", company="Melio", title="Software Engineer"),
        role_row("gh:melio:1", company="Melio", title="Senior Software Engineer"),
    ])
    view = store.connect_view(cfg)
    assert store.find_twin(view, "li:2") is None          # ambiguous
    assert store.find_twin(view, "li:3") is None          # containment ≠ same job
    store.set_status(view, "li:3", "applied")
    assert view.execute("SELECT status FROM role_state WHERE job_id='gh:melio:1'").fetchone() is None


def test_reconcile_backfills_history_without_restamping_dates(cfg):
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("li:4", company="Atticus", title="Fullstack Engineer"),
        role_row("ab:atticus:x", company="Atticus", title="Fullstack Engineer"),
    ])
    view = store.connect_view(cfg)
    # history: the li: row was applied days ago and carries the tailored PDF
    store.upsert_state(view, "li:4", status="applied", applied_date="2026-07-20",
                       applied_with_resume="tailored", tailor_status="ready",
                       prompt_path="/p/x.md", tailored_resume_path="/r/x.pdf")

    stats = store.reconcile_duplicates(view)
    assert stats["linked"] == 1

    ab = view.execute("SELECT * FROM role_state WHERE job_id='ab:atticus:x'").fetchone()
    assert ab["status"] == "applied"
    assert ab["applied_date"] == "2026-07-20"             # history copied, NOT today
    assert ab["tailored_resume_path"] == "/r/x.pdf"       # extension needs it on the ATS row
    # idempotent: second run changes nothing
    again = store.reconcile_duplicates(view)
    assert again["status_mirrored"] == 0 and again["tailor_copied"] == 0


def test_twins_link_across_divergent_title_norms(cfg):
    """The LinkedIn ingest and the board scanner normalised titles differently at different
    times ('fullstackengineer' vs 'fullstack engineer' — the real Atticus pair), so strict
    title_norm equality silently missed most true twins."""
    jobs = store.connect_jobs(cfg)
    a = role_row("li:5", company="Atticus", title="Fullstack Engineer")
    a["title_norm"] = "fullstackengineer"                 # old LinkedIn-ingest style
    b = role_row("ab:atticus:y", company="Atticus", title="Fullstack Engineer")
    b["title_norm"] = "fullstack engineer"                # scanner style
    store.upsert_roles(jobs, [a, b])
    view = store.connect_view(cfg)
    assert store.find_twin(view, "li:5") == "ab:atticus:y"


# ── 手动 cooldown（owner 2026-09-22）──────────────────────────────────────────────────────
# "这家不让投了" from the review page: same effect as a config.yaml `cooldowns:` entry
# (scanner skips, review hides) without editing YAML; expires by itself.

def test_manual_cooldown_hides_the_company_and_expires_by_itself(cfg):
    cfg = {**cfg, "application_budget": {"enabled": False}, "cooldowns": {}}
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [role_row("gh:x:1", company="X"), role_row("gh:y:1", company="Y")])
    view = store.connect_view(cfg)
    row = store.set_company_cooldown(view, "X", days=30, reason="Ashby 5/180")
    assert row["source"] == "manual" and row["company_norm"] == "x"
    d = store.fetch_view(view, cfg, status="new")
    assert {r["company"] for r in d["rows"]} == {"Y"}
    assert d["cooldowns"]["x"]["reason"] == "Ashby 5/180"
    allv = {r["job_id"]: r for r in store.fetch_view(view, cfg, status="all")["rows"]}
    assert allv["gh:x:1"]["cooldown"]["source"] == "manual" and not allv["gh:x:1"]["in_review"]
    # expired rows stop applying on their own — no cleanup job needed
    view.execute("UPDATE company_cooldowns SET until='2020-01-01'")
    view.commit()
    assert {r["company"] for r in store.fetch_view(view, cfg, status="new")["rows"]} == {"X", "Y"}
    # clearing removes the row; clearing again reports nothing to clear
    store.set_company_cooldown(view, "X", days=7)
    assert store.clear_company_cooldown(view, "x") is True
    assert store.clear_company_cooldown(view, "X") is False
    assert {r["company"] for r in store.fetch_view(view, cfg, status="new")["rows"]} == {"X", "Y"}


def test_manual_and_config_cooldowns_merge_with_the_later_date_winning(cfg):
    view = store.connect_view(cfg)
    today = store.la_today()
    cfg = {**cfg, "companies": [{"name": "Plaid Inc", "ats": "ashby", "slug": "plaid"}],
           "cooldowns": {"Plaid Inc": {"until": "2099-01-01", "reason": "config"}}}
    store.set_company_cooldown(view, "plaid inc", days=10, reason="manual, earlier")
    merged = store.active_cooldowns(view, cfg, today)
    assert merged["plaidinc"]["source"] == "config" and merged["plaidinc"]["company"] == "Plaid Inc"
    store.set_company_cooldown(view, "Plaid Inc", until="2099-06-01", reason="manual, later")
    merged = store.active_cooldowns(view, cfg, today)
    assert merged["plaidinc"]["source"] == "manual" and merged["plaidinc"]["until"] == "2099-06-01"
    # validation: garbage, past, out-of-range
    import pytest
    for bad in ({"until": "not-a-date"}, {"until": "2000-01-01"}, {"days": 0}, {"days": 999}):
        with pytest.raises(ValueError):
            store.set_company_cooldown(view, "Z", **bad)
    with pytest.raises(ValueError):
        store.set_company_cooldown(view, "   ", days=30)


def test_scanner_reads_manual_cooldowns_read_only_and_never_creates_the_user_db(cfg, tmp_path):
    cfg = {**cfg, "cooldowns": {"Cfgco": {"until": "2099-01-01", "reason": "cfg"}}}
    # no user DB yet → config only, and the file must NOT appear (the scanner is not a writer)
    ro = store.active_cooldowns_readonly(cfg)
    assert set(ro) == {"cfgco"} and not Path(cfg["user_db_path"]).exists()
    view = store.connect_view(cfg)
    store.set_company_cooldown(view, "Manualco", days=30, reason="board refused")
    ro = store.active_cooldowns_readonly(cfg)
    assert ro["manualco"]["source"] == "manual" and ro["manualco"]["reason"] == "board refused"
    assert ro["cfgco"]["source"] == "config"
    # an old user DB without the table → config only, no exception
    old = tmp_path / "old_user.db"
    import sqlite3
    sqlite3.connect(old).execute("CREATE TABLE role_state (job_id TEXT)").connection.commit()
    ro = store.active_cooldowns_readonly({**cfg, "user_db_path": str(old)})
    assert set(ro) == {"cfgco"}


def test_location_pref_reorders_the_queue_and_powers_the_bay_filter(cfg):
    """优先远程或湾区 (owner 2026-09-22): a Bay Area / remote role outranks a slightly higher-
    scored Austin role; weighted_score is untouched; the 'bay' filter keeps remote + Bay Area."""
    cfg = {**cfg, "application_budget": {"enabled": False},
           "scoring": {**cfg["scoring"], "location_pref": {"enabled": True, "bonus": 8},
                       "freshness": {"enabled": False}, "diversify": {"enabled": False}}}
    jobs = store.connect_jobs(cfg)
    a = role_row("gh:a:1", company="A", score=72.0); a["location"] = "Austin, TX"; a["is_remote"] = 0
    b = role_row("gh:b:1", company="B", score=70.0); b["location"] = "Palo Alto, CA"; b["is_remote"] = 0
    c = role_row("gh:c:1", company="C", score=69.0); c["location"] = "New York, NY"; c["is_remote"] = 1
    store.upsert_roles(jobs, [a, b, c])
    view = store.connect_view(cfg)
    d = store.fetch_view(view, cfg, status="new", sort="score")
    by = {r["job_id"]: r for r in d["rows"]}
    assert by["gh:b:1"]["rank_score"] == 78.0 and by["gh:b:1"]["weighted_score"] == 70.0
    assert by["gh:c:1"]["loc_pref"] == {"kind": "remote", "bonus": 8.0}
    assert by["gh:a:1"]["loc_pref"] == {"kind": None, "bonus": 0.0}
    assert [r["job_id"] for r in d["rows"]][0] == "gh:b:1"       # 78 > 77 > 72
    bay = store.fetch_view(view, cfg, status="new", remote="bay")
    assert {r["job_id"] for r in bay["rows"]} == {"gh:b:1", "gh:c:1"}
    only_remote = store.fetch_view(view, cfg, status="new", remote="remote")
    assert {r["job_id"] for r in only_remote["rows"]} == {"gh:c:1"}


def test_workspace_eligibility_hides_no_sponsorship_jds_and_outside_area_roles(cfg):
    """Second workspace (owner 2026-09-23): a candidate who needs sponsorship never sees a JD that
    says 'we do not sponsor'; with location_pref.review_only the review tab shows only remote and
    Bay Area roles. Both are view-time rules — the shared jobs.db keeps one score per role."""
    cfg = {**cfg, "application_budget": {"enabled": False},
           "profile": {**cfg["profile"], "needs_sponsorship": True},
           "scoring": {**cfg["scoring"], "location_pref": {"enabled": True, "bonus": 8, "review_only": True},
                       "freshness": {"enabled": False}, "diversify": {"enabled": False}}}
    jobs = store.connect_jobs(cfg)
    a = role_row("gh:a:1", company="A"); a.update(location="Palo Alto, CA", is_remote=0, jd_text="Great team. We are unable to sponsor visas for this role.")
    b = role_row("gh:b:1", company="B"); b.update(location="Palo Alto, CA", is_remote=0, jd_text="We sponsor H-1B visas.")
    c = role_row("gh:c:1", company="C"); c.update(location="Austin, TX", is_remote=0, jd_text="Anything.")
    d = role_row("gh:d:1", company="D"); d.update(location="Remote - US", is_remote=1, jd_text="Anything.")
    store.upsert_roles(jobs, [a, b, c, d])
    view = store.connect_view(cfg)
    shown = {r["job_id"] for r in store.fetch_view(view, cfg, status="new")["rows"]}
    assert shown == {"gh:b:1", "gh:d:1"}
    allv = {r["job_id"]: r for r in store.fetch_view(view, cfg, status="all")["rows"]}
    assert allv["gh:a:1"]["no_sponsor"] and allv["gh:c:1"]["loc_excluded"] and not allv["gh:b:1"]["no_sponsor"]
    # the owner's config (no sponsorship needed, no review_only) shows all four
    plain = {**cfg, "profile": {**cfg["profile"], "needs_sponsorship": False},
             "scoring": {**cfg["scoring"], "location_pref": {"enabled": True, "bonus": 8}}}
    assert len(store.fetch_view(view, plain, status="new")["rows"]) == 4


def test_config_extends_merges_dicts_and_replaces_lists(tmp_path):
    from app import config as cfgmod
    import yaml
    parent = tmp_path / "parent.yaml"; child = tmp_path / "child.yaml"
    parent.write_text(yaml.safe_dump({"a": {"x": 1, "y": 2}, "l": [1, 2], "keep": "p"}), encoding="utf-8")
    child.write_text(yaml.safe_dump({"extends": str(parent), "a": {"y": 20, "z": 3}, "l": [9]}), encoding="utf-8")
    c = cfgmod.load_config(child)
    # a derived config carries the parent path under EXTENDS_KEY (mailcode uses it to keep the
    # owner's shared app password out of another candidate's workspace); the merge itself is exact
    assert c.pop(cfgmod.EXTENDS_KEY) == str(parent)
    assert c == {"a": {"x": 1, "y": 20, "z": 3}, "l": [9], "keep": "p"}
    assert cfgmod.EXTENDS_KEY not in cfgmod.load_config(parent)
    assert cfgmod.workspace_name({"workspace": {"name": "Taylor"}}) == "Taylor" and cfgmod.workspace_name({}) == ""


def test_fit_pref_ranks_by_base_coverage_and_hides_poor_fits(cfg, tmp_path, monkeypatch):
    """Per-workspace fit rank (owner 2026-09-23): the shared jobs.db scores roles with the owner's
    weights, so a second candidate re-ranks by how many of the JD's hard skills HIS base names;
    under review_min the role leaves review (still under 'all'). JDs naming <3 skills are neutral."""
    import yaml
    from app import resume_score as rs
    monkeypatch.setattr(rs, "_JD_CACHE_REL", str(tmp_path / "jdcache.json"))
    monkeypatch.setattr(rs, "_jd_cache", None)
    base = {"name": "T", "contact": {"email": "t@x"}, "education": [], "skills_master": {"L": ["Java", "Spring Boot", "MySQL", "Kafka"]},
            "experience": [{"company": "A", "title": "SWE", "location": "x", "dates": "2020", "bullets_master": ["Built Java Spring Boot services on MySQL with Kafka."]}]}
    bp = tmp_path / "base.yaml"; bp.write_text(yaml.safe_dump(base), encoding="utf-8")
    cfg = {**cfg, "resume_base": str(bp), "application_budget": {"enabled": False},
           "scoring": {**cfg["scoring"], "fit_pref": {"enabled": True, "bonus": 15, "review_min": 0.30},
                       "freshness": {"enabled": False}, "diversify": {"enabled": False}}}
    jobs = store.connect_jobs(cfg)
    # distinct jd_hash per role: the cache is keyed by it (role_row's default "h" would collide)
    good = role_row("gh:g:1", company="Good", score=70.0); good.update(jd_text="We need Java, Spring Boot, MySQL and Kafka experience.", jd_hash="hg", is_remote=0, location="Austin, TX")
    poor = role_row("gh:p:1", company="Poor", score=72.0); poor.update(jd_text="Go, Kubernetes, Terraform, Prometheus, GCP and Rust required.", jd_hash="hp", is_remote=0, location="Austin, TX")
    thin = role_row("gh:t:1", company="Thin", score=71.0); thin.update(jd_text="Nice people. Python.", jd_hash="ht", is_remote=0, location="Austin, TX")
    store.upsert_roles(jobs, [good, poor, thin])
    view = store.connect_view(cfg)
    d = store.fetch_view(view, cfg, status="new", sort="score")
    ids = [r["job_id"] for r in d["rows"]]
    assert "gh:p:1" not in ids and ids[0] == "gh:g:1"                      # poor fit hidden, good fit leads
    by = {r["job_id"]: r for r in store.fetch_view(view, cfg, status="all")["rows"]}
    assert by["gh:g:1"]["fit"]["coverage"] == 1.0 and by["gh:g:1"]["rank_score"] == 85.0
    assert by["gh:p:1"]["fit_excluded"] and by["gh:p:1"]["fit"]["coverage"] == 0.0
    assert by["gh:t:1"]["fit"] is None and not by["gh:t:1"]["fit_excluded"]   # <3 skills → neutral
    assert (tmp_path / "jdcache.json").exists()                                # cache persisted
    off = {**cfg, "scoring": {**cfg["scoring"], "fit_pref": {"enabled": False}}}
    assert len(store.fetch_view(view, off, status="new")["rows"]) == 3
