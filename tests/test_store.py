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
