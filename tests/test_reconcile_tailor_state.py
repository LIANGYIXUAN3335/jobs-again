"""Reconciling `role_state.tailor_status` against what is actually in output/resumes.

Every case here is a real drift class found in the live DB on 2026-08-10, when 47 rows had
been parked in `queued` for up to 16 days with no résumé and no way back into the pipeline.
The invariant the tool defends: the file on disk is the truth, the DB row is a cache of it,
and a status that outlives its evidence is a lie the review page repeats every day.
"""
import shutil
from pathlib import Path

import pytest
import yaml

from app import doctor, store, tailoring
from tests.test_store import role_row
from tools import reconcile_tailor_state as rts

TYPST = shutil.which("typst") or Path("/opt/homebrew/bin/typst").exists()


# ---------- fixtures ----------

def _seed(cfg, *roles):
    """roles: (job_id, company, title, last_seen) tuples."""
    jobs = store.connect_jobs(cfg)
    rows = []
    for job_id, company, title, last_seen in roles:
        r = role_row(job_id, company=company, title=title, last_seen=last_seen)
        r["jd_text"] = "Build backend services."
        rows.append(r)
    store.upsert_roles(jobs, rows)
    jobs.close()
    return store.connect_view(cfg)


def _pdf(cfg, company, title, job_id, score=88, body=b"%PDF-1.7 fake"):
    p = tailoring.friendly_pdf_path(cfg, company, title, job_id, score)
    p.write_bytes(body)
    return p


def _prompt(cfg, company, job_id):
    p = tailoring.prompt_path(cfg, company, job_id)
    p.write_text("# prompt", encoding="utf-8")
    return p


def _find(res, job_id):
    return next(f for f in res["findings"] if f["job_id"] == job_id)


def _tailor(conn, job_id):
    r = conn.execute("SELECT tailor_status, tailored_resume_path, prompt_path"
                     " FROM role_state WHERE job_id=?", (job_id,)).fetchone()
    return dict(r)


# ---------- promote: the file exists, the DB is wrong ----------

def test_promote_queued_row_whose_pdf_is_already_on_disk(cfg):
    conn = _seed(cfg, ("gh:stripe:1", "Stripe", "Backend Engineer, Payments", "2026-08-10"))
    store.set_tailor(conn, "gh:stripe:1", tailor_status="queued",
                     prompt_path=str(_prompt(cfg, "Stripe", "gh:stripe:1")))
    pdf = _pdf(cfg, "Stripe", "Backend Engineer, Payments", "gh:stripe:1")

    res = rts.scan(cfg, conn)
    f = _find(res, "gh:stripe:1")
    assert (f["action"], f["found_via"]) == ("promote", "on-disk")

    rts.apply_findings(cfg, conn, res["findings"])
    got = _tailor(conn, "gh:stripe:1")
    assert got["tailor_status"] == "ready"
    assert got["tailored_resume_path"] == str(pdf)


def test_promote_finds_the_legacy_job_id_named_pdf(cfg):
    """Résumés rendered before the 'Casey Example - Company - Role [id] - score.pdf' rename
    still sit next to the YAML under the job-id stem; they are real résumés, not drift."""
    conn = _seed(cfg, ("gh:stripe:2", "Stripe", "Backend Engineer", "2026-08-10"))
    store.set_tailor(conn, "gh:stripe:2", tailor_status="none")
    legacy = tailoring.resume_pdf_path(cfg, "Stripe", "gh:stripe:2")
    legacy.write_bytes(b"%PDF-1.7 old")

    res = rts.scan(cfg, conn)
    assert _find(res, "gh:stripe:2")["action"] == "promote"
    rts.apply_findings(cfg, conn, res["findings"])
    assert _tailor(conn, "gh:stripe:2")["tailored_resume_path"] == str(legacy)


def test_promote_never_borrows_another_roles_pdf(cfg):
    """The friendly prefix carries the raw ATS id, so a sibling posting at the same company
    can never satisfy this role — she would send a résumé aimed at the wrong JD."""
    conn = _seed(cfg,
                 ("gh:stripe:10", "Stripe", "Backend Engineer", "2026-08-10"),
                 ("gh:stripe:11", "Stripe", "Backend Engineer", "2026-08-10"))
    store.set_tailor(conn, "gh:stripe:11", tailor_status="queued")
    _pdf(cfg, "Stripe", "Backend Engineer", "gh:stripe:10")     # only :10 has a résumé

    assert _find(rts.scan(cfg, conn), "gh:stripe:11")["action"] != "promote"


def test_relink_repoints_a_ready_row_at_the_file_that_is_actually_there(cfg):
    conn = _seed(cfg, ("gh:stripe:3", "Stripe", "Backend Engineer", "2026-08-10"))
    real = _pdf(cfg, "Stripe", "Backend Engineer", "gh:stripe:3", score=91)
    store.set_tailor(conn, "gh:stripe:3", tailor_status="ready",
                     tailored_resume_path=str(real.with_name("gone - 70.pdf")))

    res = rts.scan(cfg, conn)
    assert _find(res, "gh:stripe:3")["action"] == "relink"
    rts.apply_findings(cfg, conn, res["findings"])
    got = _tailor(conn, "gh:stripe:3")
    assert (got["tailor_status"], got["tailored_resume_path"]) == ("ready", str(real))


def test_inherits_the_pdf_of_its_duplicate_of_twin(cfg):
    """A li: shadow and its ATS canonical are ONE posting sharing ONE PDF. When only the twin
    carries the pointer the résumé is not missing — this is the row that made her look like
    she applied with a tailored résumé she could not produce."""
    conn = _seed(cfg,
                 ("li:999", "MongoDB", "Software Engineer 3", "2026-08-10"),
                 ("gh:mongodb:8089124", "MongoDB", "Software Engineer 3", "2026-08-10"))
    twin_pdf = _pdf(cfg, "MongoDB", "Software Engineer 3", "gh:mongodb:8089124", score=93)
    store.set_tailor(conn, "gh:mongodb:8089124", tailor_status="ready",
                     tailored_resume_path=str(twin_pdf))
    store.upsert_state(conn, "li:999", duplicate_of="gh:mongodb:8089124",
                       tailor_status="none")
    store.set_status(conn, "li:999", "applied", "tailored")

    res = rts.scan(cfg, conn)
    f = _find(res, "li:999")
    assert (f["action"], f["found_via"]) == ("promote", "duplicate_of")
    assert not any(a["job_id"] == "li:999" for a in res["applied_no_resume"])

    rts.apply_findings(cfg, conn, res["findings"])
    assert _tailor(conn, "li:999")["tailored_resume_path"] == str(twin_pdf)


# ---------- demote: the DB claims a file that is not there ----------

def test_demote_ready_row_with_a_missing_file(cfg):
    conn = _seed(cfg, ("gh:stripe:4", "Stripe", "Backend Engineer", "2026-08-10"))
    store.set_tailor(conn, "gh:stripe:4", tailor_status="ready",
                     tailored_resume_path="/nope/never-rendered.pdf")

    res = rts.scan(cfg, conn)
    assert _find(res, "gh:stripe:4")["action"] == "demote"
    rts.apply_findings(cfg, conn, res["findings"])
    got = _tailor(conn, "gh:stripe:4")
    # back to 'none', which is what prep_autotailor re-picks (it takes everything != ready)
    assert (got["tailor_status"], got["tailored_resume_path"]) == ("none", None)


def test_demote_ready_row_whose_pdf_is_zero_bytes(cfg):
    """A killed typst run leaves a 0-byte PDF. It passes exists() and opens to nothing."""
    conn = _seed(cfg, ("gh:stripe:5", "Stripe", "Backend Engineer", "2026-08-10"))
    empty = _pdf(cfg, "Stripe", "Backend Engineer", "gh:stripe:5", body=b"")
    store.set_tailor(conn, "gh:stripe:5", tailor_status="ready",
                     tailored_resume_path=str(empty))

    assert _find(rts.scan(cfg, conn), "gh:stripe:5")["action"] == "demote"
    assert empty.exists()                    # never deletes a résumé file, even a broken one


# ---------- resolve: queued on a role nothing will ever come back for ----------

def test_resolve_queued_on_a_role_she_already_skipped(cfg):
    conn = _seed(cfg, ("gh:stripe:6", "Stripe", "Backend Engineer", "2026-08-10"))
    prompt = _prompt(cfg, "Stripe", "gh:stripe:6")
    store.set_tailor(conn, "gh:stripe:6", tailor_status="queued", prompt_path=str(prompt))
    store.set_status(conn, "gh:stripe:6", "skip")

    res = rts.scan(cfg, conn)
    f = _find(res, "gh:stripe:6")
    assert (f["action"], f["reason"]) == ("resolve", "status-skip")

    rts.apply_findings(cfg, conn, res["findings"])
    got = _tailor(conn, "gh:stripe:6")
    assert got["tailor_status"] == "none"
    # provenance kept: the prompt file and its pointer both survive
    assert got["prompt_path"] == str(prompt) and prompt.exists()


def test_resolve_queued_on_a_delisted_posting(cfg):
    """The scanner refreshes last_seen for every role still listed, so a role trailing its
    company's newest sighting is gone from the board (same signal as prune_delisted)."""
    conn = _seed(cfg,
                 ("gh:stripe:7", "Stripe", "Backend Engineer", "2026-07-20"),   # stale
                 ("gh:stripe:8", "Stripe", "Frontend Engineer", "2026-08-10"))  # rescanned
    store.set_tailor(conn, "gh:stripe:7", tailor_status="queued",
                     prompt_path=str(_prompt(cfg, "Stripe", "gh:stripe:7")))

    f = _find(rts.scan(cfg, conn), "gh:stripe:7")
    assert (f["action"], f["reason"]) == ("resolve", "delisted")


def test_resolve_queued_on_a_verdict_drop_role(cfg):
    """`drop` is kept out of the review queue entirely and is never auto-tailored, so a
    prompt behind one can only ever sit there."""
    conn = _seed(cfg, ("gh:stripe:9", "Stripe", "Backend Engineer", "2026-08-10"))
    conn.execute("UPDATE jobs.roles SET verdict='drop' WHERE job_id='gh:stripe:9'")
    conn.commit()
    store.set_tailor(conn, "gh:stripe:9", tailor_status="queued",
                     prompt_path=str(_prompt(cfg, "Stripe", "gh:stripe:9")))

    f = _find(rts.scan(cfg, conn), "gh:stripe:9")
    assert (f["action"], f["reason"]) == ("resolve", "verdict-drop")


def test_resolve_queued_whose_role_row_was_pruned(cfg):
    conn = _seed(cfg, ("gh:stripe:12", "Stripe", "Backend Engineer", "2026-08-10"))
    store.set_tailor(conn, "gh:stripe:12", tailor_status="queued")
    conn.execute("DELETE FROM jobs.roles WHERE job_id='gh:stripe:12'")
    conn.commit()

    f = _find(rts.scan(cfg, conn), "gh:stripe:12")
    assert (f["action"], f["reason"]) == ("resolve", "role-gone")


def test_a_live_reachable_queued_role_is_left_alone(cfg):
    """The daily prep can still finish this one. Reporting it is right; rewriting it is not."""
    conn = _seed(cfg, ("gh:stripe:13", "Stripe", "Backend Engineer", "2026-08-10"))
    store.set_tailor(conn, "gh:stripe:13", tailor_status="queued",
                     prompt_path=str(_prompt(cfg, "Stripe", "gh:stripe:13")))

    res = rts.scan(cfg, conn)
    assert _find(res, "gh:stripe:13")["action"] == "stuck-live"
    rts.apply_findings(cfg, conn, res["findings"])
    assert _tailor(conn, "gh:stripe:13")["tailor_status"] == "queued"


def test_a_queued_role_with_a_valid_yaml_is_never_resolved_away(cfg):
    """A validated tailored YAML is real work. Downgrading it to `none` would throw it away
    on the next run — it needs rendering, not resolving."""
    conn = _seed(cfg, ("gh:stripe:14", "Stripe", "Backend Engineer", "2026-08-10"))
    store.set_tailor(conn, "gh:stripe:14", tailor_status="queued")
    store.set_status(conn, "gh:stripe:14", "skip")            # otherwise resolvable
    tailoring.resume_yaml_path(cfg, "Stripe", "gh:stripe:14").write_text(yaml.safe_dump({
        "job_id": "gh:stripe:14",
        "summary": "Backend engineer moving toward distributed systems work.",
        "experience": [{"company": "TikTok", "bullets": ["Built Go/internal API flows."]}],
    }), encoding="utf-8")

    res = rts.scan(cfg, conn)
    assert _find(res, "gh:stripe:14")["action"] == "render"
    rts.apply_findings(cfg, conn, res["findings"])           # render=False → untouched
    assert _tailor(conn, "gh:stripe:14")["tailor_status"] == "queued"


def test_a_pre_cutover_yaml_is_not_rendered(cfg):
    """auto_tailor.stale_before marks the keyword-stuffing era. Rendering one of those would
    stamp `ready` on a résumé the owner already ruled untrustworthy (her call 2026-08-06:
    refresh on demand, never batch-regenerate)."""
    import os
    conn = _seed(cfg, ("gh:stripe:27", "Stripe", "Backend Engineer", "2026-08-10"))
    store.set_tailor(conn, "gh:stripe:27", tailor_status="queued")
    store.set_status(conn, "gh:stripe:27", "skip")
    yf = tailoring.resume_yaml_path(cfg, "Stripe", "gh:stripe:27")
    yf.write_text(yaml.safe_dump({
        "job_id": "gh:stripe:27",
        "summary": "Backend engineer moving toward distributed systems work.",
        "experience": [{"company": "TikTok", "bullets": ["Built Go/internal API flows."]}],
    }), encoding="utf-8")
    cutover = __import__("datetime").datetime.fromisoformat(
        cfg["auto_tailor"]["stale_before"]).timestamp()
    os.utime(yf, (cutover - 86400,) * 2)

    f = _find(rts.scan(cfg, conn), "gh:stripe:27")
    assert f["action"] == "resolve" and "stale YAML" in f["reason"]
    assert yf.exists()                       # the file itself is never touched


@pytest.mark.skipif(not TYPST, reason="typst not installed")
def test_render_finishes_a_yaml_that_never_became_a_pdf(cfg):
    conn = _seed(cfg, ("gh:stripe:15", "Stripe", "Backend Engineer", "2026-08-10"))
    store.set_tailor(conn, "gh:stripe:15", tailor_status="queued")
    tailoring.resume_yaml_path(cfg, "Stripe", "gh:stripe:15").write_text(yaml.safe_dump({
        "job_id": "gh:stripe:15",
        "summary": "Backend engineer moving toward distributed systems work.",
        "skills": [{"label": "Languages", "items": ["Go/Golang", "Python"]}],
        "experience": [{"company": "TikTok", "bullets": ["Built Go/internal API flows."]}],
    }), encoding="utf-8")

    res = rts.scan(cfg, conn)
    stats = rts.apply_findings(cfg, conn, res["findings"], render=True)
    assert stats["render"] == 1 and stats["failed"] == 0
    got = _tailor(conn, "gh:stripe:15")
    assert got["tailor_status"] == "ready"
    assert Path(got["tailored_resume_path"]).read_bytes()[:4] == b"%PDF"


# ---------- report-only cohorts ----------

def test_applied_without_a_resume_is_reported_not_repaired(cfg):
    conn = _seed(cfg, ("gh:stripe:16", "Stripe", "Backend Engineer", "2026-08-10"))
    store.set_status(conn, "gh:stripe:16", "applied", "base")

    res = rts.scan(cfg, conn)
    assert [a["job_id"] for a in res["applied_no_resume"]] == ["gh:stripe:16"]
    assert res["applied_no_resume"][0]["applied_with_resume"] == "base"
    assert _find(res, "gh:stripe:16")["action"] == "ok"       # nothing to repair, only to know


def test_orphan_yaml_for_a_pruned_role_is_reported(cfg):
    conn = _seed(cfg, ("gh:stripe:17", "Stripe", "Backend Engineer", "2026-08-10"))
    tailoring.resume_yaml_path(cfg, "Ghost Co", "gh:ghost:1").write_text(
        yaml.safe_dump({"job_id": "gh:ghost:1", "summary": "x"}), encoding="utf-8")

    orph = rts.scan(cfg, conn)["orphan_yaml"]
    assert [o["job_id"] for o in orph] == ["gh:ghost:1"]
    assert orph[0]["why"] == "no role_state row"


# ---------- CLI contract ----------

def test_default_run_is_a_dry_run_and_writes_nothing(cfg, tmp_path, monkeypatch, capsys):
    conn = _seed(cfg, ("gh:stripe:18", "Stripe", "Backend Engineer", "2026-08-10"))
    store.set_tailor(conn, "gh:stripe:18", tailor_status="ready",
                     tailored_resume_path="/nope/gone.pdf")
    conn.close()
    monkeypatch.setattr(rts.cfgmod, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(rts, "BACKUP_DIR", str(tmp_path / "backups"))

    assert rts.main([]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "demote" in out

    conn = store.connect_view(cfg)
    assert _tailor(conn, "gh:stripe:18")["tailor_status"] == "ready"   # untouched
    assert not (tmp_path / "backups").exists()                        # no backup for a no-op


def test_apply_backs_the_db_up_before_writing(cfg, tmp_path, monkeypatch):
    conn = _seed(cfg, ("gh:stripe:19", "Stripe", "Backend Engineer", "2026-08-10"))
    store.set_tailor(conn, "gh:stripe:19", tailor_status="ready",
                     tailored_resume_path="/nope/gone.pdf")
    conn.close()
    monkeypatch.setattr(rts.cfgmod, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(rts, "BACKUP_DIR", str(tmp_path / "backups"))

    assert rts.main(["--apply"]) == 0
    backups = list((tmp_path / "backups").rglob("user_state.db.bak-*"))
    assert len(backups) == 1 and backups[0].stat().st_size > 0

    conn = store.connect_view(cfg)
    assert _tailor(conn, "gh:stripe:19")["tailor_status"] == "none"
    # the snapshot still holds the pre-repair state
    import sqlite3
    b = sqlite3.connect(str(backups[0]))
    assert b.execute("SELECT tailor_status FROM role_state WHERE job_id='gh:stripe:19'"
                     ).fetchone()[0] == "ready"


def test_apply_and_dry_run_together_is_refused(cfg, monkeypatch):
    monkeypatch.setattr(rts.cfgmod, "load_config", lambda *a, **k: cfg)
    with pytest.raises(SystemExit):
        rts.main(["--apply", "--dry-run"])


def test_scan_is_idempotent(cfg):
    """Running it twice must find nothing the second time, or it is fighting itself."""
    conn = _seed(cfg,
                 ("gh:stripe:20", "Stripe", "Backend Engineer", "2026-08-10"),
                 ("gh:stripe:21", "Stripe", "Frontend Engineer", "2026-08-10"))
    store.set_tailor(conn, "gh:stripe:20", tailor_status="ready",
                     tailored_resume_path="/nope/gone.pdf")
    store.set_tailor(conn, "gh:stripe:21", tailor_status="queued")
    store.set_status(conn, "gh:stripe:21", "skip")

    first = rts.scan(cfg, conn)
    rts.apply_findings(cfg, conn, first["findings"])
    second = rts.scan(cfg, conn)
    assert {f["action"] for f in second["findings"]} == {"ok"}


# ---------- the guardrail (app/doctor.py) ----------

def test_stale_queued_ages_from_the_prompt_file(cfg):
    import os
    import time as _time
    conn = _seed(cfg,
                 ("gh:stripe:22", "Stripe", "Backend Engineer", "2026-08-10"),
                 ("gh:stripe:23", "Stripe", "Frontend Engineer", "2026-08-10"))
    old = _prompt(cfg, "Stripe", "gh:stripe:22")
    ten_days = _time.time() - 10 * 86400
    os.utime(old, (ten_days, ten_days))
    store.set_tailor(conn, "gh:stripe:22", tailor_status="queued", prompt_path=str(old))
    store.set_tailor(conn, "gh:stripe:23", tailor_status="queued",
                     prompt_path=str(_prompt(cfg, "Stripe", "gh:stripe:23")))

    stale = doctor.stale_queued(cfg, conn, days=3)
    assert [s["job_id"] for s in stale] == ["gh:stripe:22"]     # today's prompt is not stale
    assert stale[0]["age_days"] == 10


def test_queued_age_falls_back_to_status_updated_at_then_gives_up():
    import datetime
    five = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    assert doctor.queued_age_days(None, five + "T09:00:00-07:00") == 5
    assert doctor.queued_age_days("/nope/missing.md", five) == 5
    assert doctor.queued_age_days(None, None) is None
    assert doctor.queued_age_days(None, "not-a-date") is None


def test_a_queued_row_with_no_age_evidence_still_counts_as_stale(cfg):
    """No prompt file and no timestamp is not "fresh" — it is a row nothing can account for."""
    conn = _seed(cfg, ("gh:stripe:24", "Stripe", "Backend Engineer", "2026-08-10"))
    store.set_tailor(conn, "gh:stripe:24", tailor_status="queued")   # no prompt, no timestamp
    row = _tailor(conn, "gh:stripe:24")
    assert row["prompt_path"] is None

    stale = doctor.stale_queued(cfg, conn, days=3)
    assert [(s["job_id"], s["age_days"]) for s in stale] == [("gh:stripe:24", None)]


def test_check_tailor_queue_is_quiet_when_nothing_is_parked(cfg, capsys):
    conn = _seed(cfg, ("gh:stripe:25", "Stripe", "Backend Engineer", "2026-08-10"))
    store.set_tailor(conn, "gh:stripe:25", tailor_status="ready",
                     tailored_resume_path=str(_pdf(cfg, "Stripe", "Backend Engineer",
                                                   "gh:stripe:25")))
    assert doctor.check_tailor_queue(cfg, conn) == 0
    assert "OK" in capsys.readouterr().out


def test_check_tailor_queue_names_the_repair_tool(cfg, capsys):
    conn = _seed(cfg, ("gh:stripe:26", "Stripe", "Backend Engineer", "2026-08-10"))
    import os
    import time as _time
    p = _prompt(cfg, "Stripe", "gh:stripe:26")
    os.utime(p, (_time.time() - 9 * 86400,) * 2)
    store.set_tailor(conn, "gh:stripe:26", tailor_status="queued", prompt_path=str(p))

    assert doctor.check_tailor_queue(cfg, conn) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "reconcile_tailor_state.py" in out
