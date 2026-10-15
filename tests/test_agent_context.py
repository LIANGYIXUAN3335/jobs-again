"""Local-only queue and review-context integration regressions."""
import datetime
import json
import os
import re
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import agent_lab, ai, application_gatekeeper, application_identity, resume_evidence, store, tailoring
from app.agent_review import digest
from app.config import norm_key, norm_title


def test_runner_uses_unpaced_ready_easy_new_queue_and_excludes_other_cards(tmp_path, monkeypatch):
    pdf = tmp_path / "ready.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    calls, closed = [], []
    conn = SimpleNamespace(close=lambda: closed.append(True))
    cfg = {"synthetic": True}
    monkeypatch.setattr(store, "connect_view", lambda actual: conn if actual is cfg else None)
    monkeypatch.setattr(application_identity, "companies_under_other_cards", lambda _: set())
    monkeypatch.setattr(store, "active_cooldowns", lambda *_: {})
    monkeypatch.setattr(store, "last_applied_by_company", lambda _: {})
    monkeypatch.setattr(agent_lab.interview_ledger, "blocked_keys", lambda *_: set())

    def fetch(actual_conn, actual_cfg, **filters):
        calls.append((actual_conn, actual_cfg, filters))
        return {"rows": [
            {"job_id": identity or "default", "company": "Example", "title": "Backend Engineer",
             "apply_profile": identity, "tailored_resume_path": str(pdf), "location": "San Francisco, CA",
             "apply_url": f"https://jobs.ashbyhq.com/example/{identity or 'default'}"}
            for identity in [None, "casey", "alex", "other"]]}

    monkeypatch.setattr(store, "fetch_view", fetch)
    candidates = agent_lab.runner_candidates(cfg)
    assert calls == [(conn, cfg, {"status": "new", "seen": "all", "identity": "",
                                "tailored": "ready", "pace": False})]
    assert [row["job_id"] for row in candidates] == ["default", "casey"]
    assert all(row["apply_profile"] == "casey" and row["resume_ready"] is True
               and row["status"] == "new" for row in candidates)
    assert [row["url"] for row in candidates] == ["https://jobs.ashbyhq.com/example/default/application", "https://jobs.ashbyhq.com/example/casey/application"]
    assert closed == [True]


def _rows(monkeypatch, rows, other_cards=(), cooled=(), applied=(), interviewing=()):
    rows = [{"location": "San Francisco, CA", **row} for row in rows]
    monkeypatch.setattr(store, "connect_view", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(store, "fetch_view", lambda *_args, **_kwargs: {"rows": rows})
    monkeypatch.setattr(application_identity, "companies_under_other_cards", lambda _: set(other_cards))
    monkeypatch.setattr(store, "active_cooldowns", lambda *_: dict.fromkeys(cooled))
    monkeypatch.setattr(store, "last_applied_by_company", lambda _: dict(applied))
    monkeypatch.setattr(agent_lab.interview_ledger, "blocked_keys", lambda *_: set(interviewing))


def test_runner_leaves_alone_companies_it_should_not_write_to_and_starts_with_new_ones(tmp_path, monkeypatch):
    pdf = tmp_path / "ready.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    row = lambda company, title="Backend Engineer": {"job_id": company + title, "company": company, "company_norm": company.lower(), "title": title,
                                                     "apply_profile": "casey", "tailored_resume_path": str(pdf),
                                                     "apply_url": f"https://jobs.ashbyhq.com/{company}/{len(title)}", "location": "Sunnyvale"}
    today = datetime.date.today()
    _rows(monkeypatch, [row("Oldco"), row("Cooled"), row("Busy"), row("Recent"), row("Fresh"), row("Fresh", "Contracts Coordinator"),
                        row("Finance", "Senior FP&A Analyst - Engineering & Product")],
          cooled=["cooled"], interviewing=["busy"],
          applied={"recent": (today - datetime.timedelta(days=10)).isoformat(), "oldco": (today - datetime.timedelta(days=60)).isoformat()})
    jobs = agent_lab.runner_candidates({})
    assert [job["company"] for job in jobs] == ["Fresh", "Oldco"]
    assert "从没投过" in jobs[0]["selection_reason"] and "上次投这家公司是" in jobs[1]["selection_reason"]


def _seed_role(cfg, job_id, company, **state):
    value = {key: None for key in store.ROLE_COLS}
    value.update(job_id=job_id, ats="ashby", company=company, company_norm=norm_key(company), title="Backend Engineer",
                 title_norm=norm_title("Backend Engineer"), apply_url=f"https://jobs.ashbyhq.com/{company}/{job_id}", weighted_score=80,
                 verdict="consider", first_seen=store.la_today(), last_seen=store.la_today(), location="Remote, United States", is_remote=1)
    with store.connect_jobs(cfg) as conn:
        store.upsert_roles(conn, [value])
    with store.connect_view(cfg) as conn:
        store.upsert_state(conn, job_id, **state)


def test_last_applied_reads_the_date_on_every_row_not_the_status(cfg):
    _seed_role(cfg, "a", "Applied", status="applied", applied_date="2026-01-05")
    _seed_role(cfg, "b", "Rejected", status="rejected", applied_date="2026-02-05")
    _seed_role(cfg, "c", "Talking", status="interviewing", applied_date="2026-03-05")
    _seed_role(cfg, "d", "Skipped", status="skip", applied_date=None)
    _seed_role(cfg, "e", "Blank", status="rejected", applied_date="")
    _seed_role(cfg, "f", "Twice", status="rejected", applied_date="2026-04-01")
    _seed_role(cfg, "g", "Twice", status="applied", applied_date="2026-05-01")
    with store.connect_view(cfg) as conn:
        assert store.last_applied_by_company(conn) == {
            "applied": "2026-01-05", "rejected": "2026-02-05", "talking": "2026-03-05", "twice": "2026-05-01"}


def test_a_rejected_role_counts_for_the_gap_and_the_reason_text(cfg, tmp_path, monkeypatch):
    pdf = tmp_path / "ready.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    today = datetime.date.today()
    _seed_role(cfg, "old", "Merge", status="rejected", applied_date=(today - datetime.timedelta(days=60)).isoformat())
    _seed_role(cfg, "recent", "Recentco", status="interviewing", applied_date=(today - datetime.timedelta(days=10)).isoformat())
    rows = [{"job_id": company + "-new", "company": company, "company_norm": company.lower(), "title": "Backend Engineer", "location": "Sunnyvale",
             "tailored_resume_path": str(pdf), "apply_url": f"https://jobs.ashbyhq.com/{company}/1"} for company in ("Merge", "Recentco", "Fresh")]
    monkeypatch.setattr(store, "fetch_view", lambda *_args, **_kwargs: {"rows": rows})
    monkeypatch.setattr(agent_lab.interview_ledger, "blocked_keys", lambda *_: set())
    jobs = agent_lab.runner_pool(cfg)
    assert [job["company"] for job in jobs] == ["Fresh", "Merge"]
    assert "从没投过" in jobs[0]["selection_reason"].split("；")[1]
    assert jobs[1]["selection_reason"].split("；")[1] == f"上次投这家公司是 {today - datetime.timedelta(days=60)}"


def _pool_of(tmp_path, monkeypatch, rows, resolved):
    pdf = tmp_path / "ready.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    cfg = {"synthetic": True}
    rows = [{"company": "Mintive", "company_norm": "mintive", "title": "Backend Engineer", "location": "San Francisco, CA",
             "tailored_resume_path": str(pdf), **row} for row in rows]
    monkeypatch.setattr(store, "connect_view", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(application_identity, "companies_under_other_cards", lambda _: set())
    monkeypatch.setattr(store, "active_cooldowns", lambda *_: {})
    monkeypatch.setattr(store, "last_applied_by_company", lambda _: {})
    monkeypatch.setattr(agent_lab.interview_ledger, "blocked_keys", lambda *_: set())
    monkeypatch.setattr(store, "fetch_view", lambda *_args, **_kwargs: {"rows": rows})
    monkeypatch.setattr(store, "lookup_role_by_ids", lambda _conn, _candidates: None)
    monkeypatch.setattr(store, "lookup_role_by_apply_url", lambda _conn, _urls: resolved and {"job_id": resolved})
    return agent_lab.runner_pool(cfg)


ROLE_URL = "https://jobs.ashbyhq.com/mintive/11111111-1111-1111-1111-111111111111"


def test_several_rows_of_one_posting_keep_the_one_its_page_resolves_to(tmp_path, monkeypatch):
    rows = [{"job_id": job_id, "apply_url": ROLE_URL} for job_id in ("li:1", "li:2", "li:3")] + [{"job_id": "li:9", "apply_url": ROLE_URL.replace("1", "2")}]
    assert [job["job_id"] for job in _pool_of(tmp_path, monkeypatch, rows, "li:2")] == ["li:2", "li:9"]


def test_rows_whose_page_resolves_to_none_of_them_all_stay_out(tmp_path, monkeypatch):
    rows = [{"job_id": job_id, "apply_url": ROLE_URL} for job_id in ("li:1", "li:2")]
    assert _pool_of(tmp_path, monkeypatch, rows, "ab:elsewhere") == []
    assert _pool_of(tmp_path, monkeypatch, rows, None) == []


@pytest.mark.parametrize("clicked, refused", [(False, 0), (True, 1)])
def test_a_refusal_closes_the_role_only_when_submit_was_clicked(tmp_path, monkeypatch, clicked, refused):
    job = {"job_id": "job-1", "company": "Example", "title": "Backend Engineer", "apply_profile": "casey", "resume_ready": True,
           "status": "new", "url": "https://jobs.ashbyhq.com/example/job-1/application"}
    calls = []
    monkeypatch.setattr(agent_lab, "runner_candidates", lambda _: [job])
    monkeypatch.setattr(application_identity, "prepare_casey_claim", lambda _cfg, value: value)
    monkeypatch.setattr(application_gatekeeper, "record_refused", lambda *args: calls.append(args))
    app = FastAPI()
    app.include_router(agent_lab.build_router({}, context_loader=lambda *_: {}, run_dir=tmp_path))
    with TestClient(app) as client:
        token = re.search(r'name="review-token" content="([^"]+)"', client.get("/agent-lab").text)[1]
        client.headers.update({"Origin": "http://testserver", "X-JobScan-Review": token})
        client.post("/api/agent-lab/runner/start", json={"apply_profile": "casey", "max_jobs": 2})
        claim = client.post("/api/agent-lab/runner/claim", json={"worker_id": "w"}).json()
        if clicked:   # submit-permit writes this before any click
            (tmp_path / ("submission-" + digest(["job-1", "real"]) + ".json")).write_text(json.dumps({"attempts": 1}))
        finished = client.post("/api/agent-lab/runner/finish", json={
            "worker_id": "w", "lease_id": claim["lease_id"], "token": claim["token"],
            "outcome": "blocked", "evidence": "雇主拒收：We receive too many applications to respond to everyone."})
    assert finished.status_code == 200 and len(calls) == refused


def test_runner_takes_only_forms_the_preflight_cleared(tmp_path, monkeypatch):
    pdf = tmp_path / "ready.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    row = lambda job_id: {"job_id": job_id, "company": job_id, "company_norm": job_id, "title": "Backend Engineer", "apply_profile": "casey",
                          "tailored_resume_path": str(pdf), "apply_url": f"https://jobs.ashbyhq.com/example/{job_id}", "location": "Sunnyvale"}
    _rows(monkeypatch, [row(j) for j in ("ready", "essay", "ask", "never", "unread")])
    verdicts = {j: {"verdict": j} for j in ("ready", "essay", "ask", "never")}
    monkeypatch.setattr(agent_lab.form_preflight, "assess", lambda *_: verdicts)
    monkeypatch.setattr(agent_lab.form_preflight, "allow_essays", lambda *_: False)
    cfg = {"agent_runner": {"preflight": True}}
    assert [job["job_id"] for job in agent_lab.runner_candidates(cfg)] == ["ready"]
    monkeypatch.setattr(agent_lab.form_preflight, "allow_essays", lambda *_: True)
    assert [job["job_id"] for job in agent_lab.runner_candidates(cfg)] == ["ready", "essay"]
    def broken(*_):
        raise RuntimeError("node not found")
    monkeypatch.setattr(agent_lab.form_preflight, "assess", broken)
    assert agent_lab.runner_candidates(cfg) == []


def test_runner_stays_in_the_bay_area_or_remote(tmp_path, monkeypatch):
    pdf = tmp_path / "ready.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    rows = [{"job_id": location, "company": "Example", "title": "Backend Engineer", "apply_profile": "casey",
             "tailored_resume_path": str(pdf), "apply_url": f"https://jobs.ashbyhq.com/example/{index}", "location": location}
            for index, location in enumerate(["Sunnyvale", "USA - Remote", "San Francisco, New York", "New York, NY", "Austin, TX",
                                              "KSA - RYD G", "Remote - Canada", ""])]
    _rows(monkeypatch, rows)
    assert [row["job_id"] for row in agent_lab.runner_candidates({})] == ["Sunnyvale", "USA - Remote", "San Francisco, New York"]


def test_runner_requires_existing_pdf_and_a_verified_https_board(tmp_path, monkeypatch):
    pdf = tmp_path / "ready.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    urls = ["https://jobs.ashbyhq.com/example/id", "https://jobs.ashbyhq.com/example/other/application",
            "https://jobs.lever.co/example/id", "https://boards.greenhouse.io/example/jobs/1",
            "https://job-boards.greenhouse.io/embed/job_app?for=example&token=2",
            "http://jobs.ashbyhq.com/example/id", "https://jobs.ashbyhq.com.evil.test/example/id",
            "https://unknown.test/example/id", "https://job-boards.greenhouse.io.evil.test/example/jobs/3"]
    rows = [{"job_id": str(i), "company": "Example", "title": "Backend Engineer",
             "apply_profile": "casey", "tailored_resume_path": str(pdf), "apply_url": url}
            for i, url in enumerate(urls)]
    rows.append({**rows[0], "job_id": "missing", "tailored_resume_path": str(tmp_path / "missing.pdf")})
    _rows(monkeypatch, rows)
    result = agent_lab.runner_candidates({})
    assert [row["job_id"] for row in result] == ["0", "1", "3", "4"]
    # A board's own job path can redirect to the employer's careers site; the embed page stays on Greenhouse.
    assert [row["url"] for row in result] == [urls[0] + "/application", urls[1],
                                              "https://job-boards.greenhouse.io/embed/job_app?for=example&token=1", urls[4]]


def test_runner_skips_cards_the_owner_routed_and_same_title_postings(tmp_path, monkeypatch):
    pdf = tmp_path / "ready.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    def row(job_id, company_norm="example", **changes):
        return {"job_id": job_id, "company": "Example", "company_norm": company_norm, "title": "Backend Engineer",
                "apply_profile": None, "tailored_resume_path": str(pdf),
                "apply_url": f"https://jobs.ashbyhq.com/{company_norm}/{job_id}", **changes}
    _rows(monkeypatch, [
        row("free"), row("to-small-card", "openai"), row("to-a2", "amazon"),
        row("by-hand", apply_profile="alex"),
        row("other-name-already-applied", "temporal"),
        row("repost", badge_ref={"job_id": "old", "status": "applied", "date": "2026-10-01"}),
        row("mapped-to-casey", "acme"),
    ], other_cards={"temporal"})
    cfg = {"apply_profiles": {"companies": {"openai": "alex", "amazon": "casey2", "acme": "casey"}}}
    assert [r["job_id"] for r in agent_lab.runner_candidates(cfg)] == ["free", "mapped-to-casey"]


def test_runner_explains_the_selection_from_the_row(tmp_path, monkeypatch):
    pdf = tmp_path / "ready.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    _rows(monkeypatch, [{"job_id": "1", "company": "Example", "title": "Backend Engineer", "apply_profile": None,
                         "tailored_resume_path": str(pdf), "apply_url": "https://jobs.ashbyhq.com/example/1",
                         "weighted_score": 84.1, "resume_score": 85, "verdict": "strong"}])
    reason = agent_lab.runner_candidates({})[0]["selection_reason"]
    assert "岗位分 84" in reason and "简历匹配 85" in reason and "strong" in reason


@pytest.fixture
def real_context_source(monkeypatch, tmp_path):
    # Importing the module defines routes; all request-time I/O is replaced below.
    from app import web
    uuid = "11111111-2222-3333-4444-555555555555"
    role = {"job_id": f"ab:example:{uuid}", "company": "Example", "company_norm": "example",
            "title": "Backend Engineer", "apply_url": f"https://jobs.ashbyhq.com/example/{uuid}",
            "tailor_status": "ready", "tailored_resume_path": str(tmp_path / "resume.pdf"),
            "status": "new", "jd_text": "Build reliable backend services."}
    profile = {"first_name": "Casey", "last_name": "Example", "preferred_name": "Casey",
               "email": "casey@example.test", "phone": "202-555-0142"}
    evidence = {"name": "Casey Example - Example.pdf", "size": 100, "sha256": "a" * 64,
                "text": "Casey Example\nBackend Engineer", "identity_verified": True}
    closed, inspected = [], []
    conn = object()

    def connection():
        try:
            yield conn
        finally:
            closed.append(True)

    def inspect(path, name, actual_profile):
        inspected.append((str(path), name, actual_profile))
        return evidence

    monkeypatch.setattr(web, "view_conn", connection)
    monkeypatch.setattr(store, "lookup_role_by_ids", lambda *_: role)
    monkeypatch.setattr(store, "lookup_role_by_apply_url", lambda *_: role)
    monkeypatch.setattr(store, "effective_profile_key", lambda *_: "casey")
    monkeypatch.setattr(web, "_profile_for_role", lambda _: profile)
    monkeypatch.setattr(web, "outbound_pdf_name", lambda _: evidence["name"])
    monkeypatch.setattr(tailoring, "load_base", lambda _: {"experience": [{"company": "Example"}]})
    monkeypatch.setattr(ai, "_facts", lambda *_: "Remote work experience: hybrid")
    monkeypatch.setattr(store, "active_cooldowns", lambda *_: {})
    monkeypatch.setattr(resume_evidence, "inspect_resume", inspect)
    return SimpleNamespace(role=role, evidence=evidence, profile=profile, closed=closed, inspected=inspected)


@pytest.mark.parametrize("suffix", ["", "/application", "/application/?utm_source=test#apply"])
def test_real_context_binds_exact_role_url_and_actual_pdf(real_context_source, suffix):
    source = real_context_source
    result = agent_lab.real_context({}, source.role["job_id"], "casey", source.role["apply_url"] + suffix)
    assert result["job_id"] == source.role["job_id"]
    assert result["job_description"] == source.role["jd_text"]
    assert result["expected_resume"] == source.evidence
    assert source.inspected == [(source.role["tailored_resume_path"], source.evidence["name"], source.profile)]
    assert next(f for f in result["facts"] if f["id"] == "resume-uploaded")["value"] == source.evidence["text"]
    # Reviewer sees what Filler drafts from, and the standing answers say what they do not cover.
    facts = {f["id"]: f["value"] for f in result["facts"]}
    assert facts["candidate-facts"] == "Remote work experience: hybrid"
    assert "Job board" in facts["standing-answers"] and "international travel" in facts["standing-answers"]
    assert result["submission_blocker"] == "" and result["requires_attachment_review"] is True
    assert source.closed == [True]


@pytest.mark.parametrize("change", [
    lambda url: url.replace("jobs.ashbyhq.com", "jobs.lever.co"),
    lambda url: url.replace("https://", "http://"),
    lambda url: url.replace("/example/", "/other/"),
    lambda url: "https://jobs.ashbyhq.com/other/role?ref=" + url.rsplit("/", 1)[1],
    lambda url: url + "?job_id=another",
    lambda url: url.replace("jobs.ashbyhq.com", "user:sample-bf53c609@example.com"),
])
def test_real_context_rejects_different_target_even_when_raw_uuid_matches(real_context_source, change):
    source = real_context_source
    with pytest.raises(HTTPException) as error:
        agent_lab.real_context({}, source.role["job_id"], "casey", change(source.role["apply_url"]))
    assert error.value.status_code == 409
    assert source.inspected == [] and source.closed == [True]


def test_real_context_rejects_identity_changed_after_fill(real_context_source):
    source = real_context_source
    with pytest.raises(HTTPException) as error:
        agent_lab.real_context({}, source.role["job_id"], "alex", source.role["apply_url"])
    assert error.value.status_code == 409 and source.inspected == []
    assert source.closed == [True]


def test_real_context_blocks_unverified_pdf_identity(real_context_source):
    source = real_context_source
    source.evidence["identity_verified"] = False
    result = agent_lab.real_context({}, source.role["job_id"], "casey", source.role["apply_url"])
    assert result["submission_blocker"] and result["expected_resume"]["identity_verified"] is False


# ---- what the board says about a stopped job, read from a real database ----
def _ready_role(cfg, tmp_path, job_id, company, title="Backend Engineer", **state):
    pdf = tmp_path / "ready.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    _seed_role(cfg, job_id, company, **{"tailor_status": "ready", "tailored_resume_path": str(pdf), **state})
    if title != "Backend Engineer":
        with store.connect_jobs(cfg) as conn:
            conn.execute("UPDATE roles SET title=?, title_norm=? WHERE job_id=?", (title, norm_title(title), job_id))
    return {"job_id": job_id, "company": company, "title": title}


def test_each_stopped_job_gets_its_own_true_reason(cfg, tmp_path, monkeypatch):
    cfg["agent_runner"] = {}
    jobs = [_ready_role(cfg, tmp_path, "free", "Freeco"),
            _ready_role(cfg, tmp_path, "busy", "Busyco"),
            _ready_role(cfg, tmp_path, "frozen", "Frozenco"),
            _ready_role(cfg, tmp_path, "recent", "Recentco"),
            _ready_role(cfg, tmp_path, "policy", "Policyco", title="Member of Technical Staff (AI Policy)"),
            _ready_role(cfg, tmp_path, "noresume", "Paperco", tailored_resume_path=str(tmp_path / "gone.pdf")),
            _ready_role(cfg, tmp_path, "done", "Doneco", status="applied", applied_date="2026-11-05"),
            _ready_role(cfg, tmp_path, "unready", "Draftco", tailor_status="generated")]
    _seed_role(cfg, "recent-other", "Recentco", status="applied", applied_date=datetime.date.today().isoformat())
    with store.connect_view(cfg) as conn:
        until = (datetime.date.today() + datetime.timedelta(days=40)).isoformat()
        store.set_company_cooldown(conn, "Frozenco", until=until, reason="雇主拒收：x")
    monkeypatch.setattr(agent_lab.interview_ledger, "blocked_keys", lambda *_: {"busyco"})
    facts = agent_lab.stopped_job_facts(cfg, jobs)
    assert facts["free"] == {"verdict": None}
    assert facts["done"] == {"closed": True}
    assert "正在技术面" in facts["busy"]["why"]
    assert "冷却期" in facts["frozen"]["why"] and until in facts["frozen"]["why"]
    assert "30 天" in facts["recent"]["why"] and datetime.date.today().isoformat() in facts["recent"]["why"]
    assert "不是工程岗" in facts["policy"]["why"]
    assert "简历" in facts["noresume"]["why"] and "简历" in facts["unready"]["why"]


def test_a_job_the_preflight_has_not_read_says_so_when_the_preflight_is_on(cfg, tmp_path, monkeypatch):
    job = _ready_role(cfg, tmp_path, "x", "Xco")
    cfg["agent_runner"] = {"preflight": True}
    monkeypatch.setattr(agent_lab.form_preflight, "assess", lambda _cfg: {})
    assert agent_lab.stopped_job_facts(cfg, [job])["x"] == {"verdict": "unread"}
    cfg["agent_runner"] = {}
    assert agent_lab.stopped_job_facts(cfg, [job])["x"] == {"verdict": None}


def test_the_ledger_comes_before_the_cooldown_when_a_company_has_both(cfg, tmp_path, monkeypatch):
    job = _ready_role(cfg, tmp_path, "both", "Bothco")
    with store.connect_view(cfg) as conn:
        store.set_company_cooldown(conn, "Bothco", days=30)
    monkeypatch.setattr(agent_lab.interview_ledger, "blocked_keys", lambda *_: {"bothco"})
    assert "正在技术面" in agent_lab.stopped_job_facts(cfg, [job])["both"]["why"]


@pytest.mark.parametrize("kind, expected", [
    ("ready", {"verdict": "ready"}),
    ("never", {"verdict": "never", "why": "投不了：要另外传文件：Writing sample"}),
    ("gone", {"verdict": "gone", "why": "这个岗位的表单已经读不到了，多半下架了。"}),
    ("ask", {"verdict": "ask", "waits": "开跑前检查说这张表还有题没答，在下面「开跑前要你答的题」。答完才能重跑。"}),
    ("essay", {"verdict": "essay", "waits": "这张表有短答题。打开下面「由模型写」那个开关，才能重跑。"}),
])
def test_a_job_in_the_queue_takes_its_reason_from_the_preflight(cfg, tmp_path, monkeypatch, kind, expected):
    job = _ready_role(cfg, tmp_path, "x", "Xco")
    cfg["agent_runner"] = {"preflight": True}
    monkeypatch.setattr(agent_lab.form_preflight, "assess", lambda _cfg: {"x": {"verdict": kind, "blocks": ["要另外传文件：Writing sample"]}})
    monkeypatch.setattr(agent_lab.form_preflight, "allow_essays", lambda _cfg: False)
    assert agent_lab.stopped_job_facts(cfg, [job])["x"] == expected
    monkeypatch.setattr(agent_lab.form_preflight, "allow_essays", lambda _cfg: True)
    if kind == "essay":
        assert agent_lab.stopped_job_facts(cfg, [job])["x"] == {"verdict": "essay"}


def test_facts_that_cannot_be_read_say_nothing_rather_than_take_the_board_down(cfg, tmp_path, monkeypatch):
    job = _ready_role(cfg, tmp_path, "x", "Xco")
    def broken(_cfg):
        raise OSError("database is locked")
    monkeypatch.setattr(agent_lab, "runner_pool", broken)
    assert agent_lab.stopped_job_facts(cfg, [job]) == {}


def test_the_queue_gate_gives_no_reason_for_a_role_it_lets_through(cfg, tmp_path):
    cfg["agent_runner"] = {}
    job = _ready_role(cfg, tmp_path, "x", "Xco")
    assert [row["job_id"] for row in agent_lab.runner_pool(cfg)] == ["x"]
    assert agent_lab.stopped_job_facts(cfg, [job])["x"] == {"verdict": None}


def test_the_code_stamp_is_the_later_of_the_page_and_the_reviewer():
    from app.config import ROOT
    assert agent_lab.code_changed_at() == max((ROOT / "extension" / "content.js").stat().st_mtime, (ROOT / "app" / "agent_review.py").stat().st_mtime)


# ---- a refusal on the panel path and on a hand submit ----
def _client(cfg, tmp_path, monkeypatch, context=None):
    monkeypatch.setattr(agent_lab, "runner_candidates", lambda _: [])
    monkeypatch.setattr(application_identity, "prepare_casey_claim", lambda _cfg, value: value)
    app = FastAPI()
    loader = context or (lambda job_id, profile, url: {"job_id": job_id, "company": "Example", "application_status": "new", "submission_blocker": "", "facts": []})
    app.include_router(agent_lab.build_router(cfg, context_loader=loader, run_dir=tmp_path,
                                              reviewer=lambda snapshot, ctx, c: {"decision": "pass", "model_status": "completed", "model": "fixture", "summary": "ok", "issues": [],
                                                                                 "fingerprint": snapshot.fingerprint(), "facts_version": digest(ctx)}))
    client = TestClient(app)
    token = re.search(r'name="review-token" content="([^"]+)"', client.get("/agent-lab").text)[1]
    client.headers.update({"Origin": "http://testserver", "X-JobScan-Review": token})
    return client


def _pair(client):
    code = client.post("/api/agent-lab/pair-code", json={}).json()["code"]
    token = client.post("/api/agent-lab/pair", json={"code": code}, headers={"Origin": "chrome-extension://abc"}).json()["token"]
    return {"Origin": "chrome-extension://abc", "X-JobScan-Review": token}


def _permit(client, job_id, url, lease=None):
    body = {"job_id": job_id, "apply_profile": "casey", "demo": False, "submit_authorized": True, "runner_lease": lease,
            "snapshot": {"url": url, "document_id": "d", "coverage": "complete", "fields": [{"id": "a", "label": "Name", "kind": "text", "value": "A"}]}}
    run = client.post("/api/agent-lab/review", json=body).json()
    assert client.post("/api/agent-lab/confirm", json={**body, "run_id": run["id"]}).status_code == 200
    reply = client.post("/api/agent-lab/submit-permit", json={**body, "run_id": run["id"]})
    assert reply.status_code == 200, reply.text
    return reply.json()


def test_the_shared_fixtures_keep_real_settings_and_tokens_out(cfg, tmp_path):
    # With the real settings every /runner/start ran caffeinate, a Mac notification and a calendar refresh that can rewrite the real token.
    from app import gcal
    assert cfg["agent_runner"] == {}
    assert gcal.TOKEN_PATH.startswith(str(tmp_path))


def _board_rows(client):
    return client.get("/api/agent-lab/answers").json()


def _batch_job(cfg, tmp_path, monkeypatch, job_id="job-1", company="Example"):
    _seed_role(cfg, job_id, company)
    url = f"https://jobs.ashbyhq.com/{company}/{job_id}/application"
    client = _client(cfg, tmp_path, monkeypatch)
    job = {"job_id": job_id, "company": company, "title": "Backend Engineer", "url": url, "apply_profile": "casey", "resume_ready": True, "status": "new"}
    monkeypatch.setattr(agent_lab, "runner_candidates", lambda _: [job])
    client.post("/api/agent-lab/runner/start", json={"apply_profile": "casey", "max_jobs": 1})
    claim = client.post("/api/agent-lab/runner/claim", json={"worker_id": "w"}).json()
    return client, url, {"lease_id": claim["lease_id"], "token": claim["token"], "worker_id": "w"}


CODE_STOP = "页面显示验证码已发送，并有验证码输入框；需要处理验证步骤。"


def test_a_code_stop_she_then_finished_by_hand_stops_asking_for_the_code(cfg, tmp_path, monkeypatch):
    client, url, lease = _batch_job(cfg, tmp_path, monkeypatch)
    run = _permit(client, "job-1", url, lease)
    client.post("/api/agent-lab/submission-result", json={"run_id": run["id"], "attempt_id": run["attempt_id"], "outcome": "blocked", "evidence": CODE_STOP})
    client.post("/api/agent-lab/runner/finish", json={**lease, "outcome": "blocked", "evidence": CODE_STOP})
    row = _board_rows(client)["batch"][0]
    assert row["needs_code"] is True and row["went_out"] is False
    with store.connect_view(cfg) as conn:
        store.upsert_state(conn, "job-1", status="applied", applied_date=store.la_today())
    row = _board_rows(client)["batch"][0]
    assert row["clicked"] is True and row["needs_code"] is False and row["went_out"] is True


def test_a_refusal_after_that_code_step_reads_as_the_employers_no(cfg, tmp_path, monkeypatch):
    client, url, lease = _batch_job(cfg, tmp_path, monkeypatch)
    run = _permit(client, "job-1", url, lease)
    client.post("/api/agent-lab/submission-result", json={"run_id": run["id"], "attempt_id": run["attempt_id"], "outcome": "blocked", "evidence": CODE_STOP})
    client.post("/api/agent-lab/runner/finish", json={**lease, "outcome": "blocked", "evidence": CODE_STOP})
    assert client.post("/api/agent-lab/hand-refusal", json={"job_id": "job-1", "evidence": "You have already applied.", "url": url}, headers=_pair(client)).status_code == 200
    row = _board_rows(client)["batch"][0]
    assert row["evidence"] == "雇主拒收：You have already applied." and row["needs_code"] is False and row["went_out"] is False


def test_a_hand_refusal_on_a_stopped_job_says_so_on_the_board(cfg, tmp_path, monkeypatch):
    client, url, lease = _batch_job(cfg, tmp_path, monkeypatch)
    client.post("/api/agent-lab/runner/finish", json={**lease, "outcome": "needs_user", "evidence": "必答题没有答案。"})
    assert client.post("/api/agent-lab/hand-refusal", json={"job_id": "job-1", "evidence": "You have already applied.", "url": url}, headers=_pair(client)).status_code == 200
    view = _board_rows(client)
    assert view["paused"] == []
    assert view["batch"][0]["evidence"] == "雇主拒收：You have already applied."
    assert "雇主说不收" in view["batch"][0]["note"] and "比如你自己投完了" not in view["batch"][0]["note"]


def test_a_stopped_job_she_finished_by_hand_says_only_that(cfg, tmp_path, monkeypatch):
    client, _url, lease = _batch_job(cfg, tmp_path, monkeypatch)
    client.post("/api/agent-lab/runner/finish", json={**lease, "outcome": "needs_user", "evidence": "必答题没有答案。"})
    with store.connect_view(cfg) as conn:
        store.upsert_state(conn, "job-1", status="applied", applied_date=store.la_today())
    assert "比如你自己投完了" in _board_rows(client)["batch"][0]["note"]


def test_a_click_on_a_role_outside_the_queue_holds_the_retry_of_its_sibling(cfg, tmp_path, monkeypatch):
    # Role a is no candidate (preflight will not run it), and she pressed Submit on the panel, so all that is left of that click is its receipt.
    client, _url, lease = _batch_job(cfg, tmp_path, monkeypatch, job_id="b", company="Acme")
    client.post("/api/agent-lab/runner/finish", json={**lease, "outcome": "needs_user", "evidence": "必答题没有答案。"})
    _seed_role(cfg, "a", "Acme")
    assert client.post("/api/agent-lab/runner/retry", json={"job_id": "b"}).status_code == 200
    client.post("/api/agent-lab/runner/pause", json={})
    (tmp_path / ("submission-" + digest(["a", "real"]) + ".json")).write_text("{}")
    refused = client.post("/api/agent-lab/runner/retry", json={"job_id": "b"})
    assert refused.status_code == 409 and "可能已经投出去了" in refused.json()["detail"]


def test_clicked_new_roles_finds_receipts_on_any_new_role_within_the_gap(cfg, tmp_path):
    _seed_role(cfg, "open", "Acme")
    _seed_role(cfg, "done", "Beta", status="applied", applied_date=store.la_today())
    _seed_role(cfg, "old", "Gamma")
    _seed_role(cfg, "plain", "Delta")
    for job_id in ("open", "done", "old"):
        (tmp_path / ("submission-" + digest([job_id, "real"]) + ".json")).write_text("{}")
    long_ago = time.time() - (agent_lab.SAME_COMPANY_GAP_DAYS + 1) * 86400
    os.utime(tmp_path / ("submission-" + digest(["old", "real"]) + ".json"), (long_ago, long_ago))
    assert [(job["job_id"], job["company"]) for job in agent_lab.clicked_new_roles(cfg, tmp_path)] == [("open", "Acme")]


def test_where_a_job_lands_skips_a_company_held_by_such_a_click():
    candidates = [{"job_id": "b", "company": "Acme"}, {"job_id": "c", "company": "Beta"}]
    records = [{"job": job, "status": "needs_user", "finished_at": time.time()} for job in candidates]
    assert agent_lab.next_batch_places(candidates, records, lambda _: True) == {"b": 1, "c": 2}
    held = agent_lab.next_batch_places(candidates, records, lambda job_id: job_id != "a", clicked_roles=[{"job_id": "a", "company": "ACME, "}])
    assert held == {"c": 1}


REFUSAL = "雇主拒收：We couldn't submit your application. You recently applied. Please wait 6 months."


def test_a_refusal_on_the_panel_path_closes_the_role_and_freezes_the_company(cfg, tmp_path, monkeypatch):
    _seed_role(cfg, "job-1", "Example")
    url = "https://jobs.ashbyhq.com/Example/job-1/application"
    client = _client(cfg, tmp_path, monkeypatch)
    run = _permit(client, "job-1", url)
    reply = client.post("/api/agent-lab/submission-result", json={"run_id": run["id"], "attempt_id": run["attempt_id"], "outcome": "blocked", "evidence": REFUSAL})
    assert reply.status_code == 200 and reply.json()["status"] == "blocked"
    with store.connect_view(cfg) as conn:
        assert store.get_role(conn, "job-1")["status"] == "skip"
        cooldown = store.active_cooldowns(conn, cfg)[norm_key("Example")]
    assert cooldown["until"] == (datetime.date.fromisoformat(store.la_today()) + datetime.timedelta(days=180)).isoformat()


@pytest.mark.parametrize("outcome, evidence", [("submitted", "Thank you for applying."), ("unknown", "没有读到回执。"), ("validation_error", "Name: required"),
                                                ("blocked", "页面上写着这句话，但还没有点提交，不算雇主拒收，请你看一眼：too many applications")])
def test_other_results_on_the_panel_path_close_nothing(cfg, tmp_path, monkeypatch, outcome, evidence):
    _seed_role(cfg, "job-1", "Example")
    client = _client(cfg, tmp_path, monkeypatch)
    run = _permit(client, "job-1", "https://jobs.ashbyhq.com/Example/job-1/application")
    assert client.post("/api/agent-lab/submission-result", json={"run_id": run["id"], "attempt_id": run["attempt_id"], "outcome": outcome, "evidence": evidence}).status_code == 200
    with store.connect_view(cfg) as conn:
        assert store.get_role(conn, "job-1")["status"] == "new" and norm_key("Example") not in store.active_cooldowns(conn, cfg)


def test_a_refusal_on_a_batch_is_recorded_once_by_finish(cfg, tmp_path, monkeypatch):
    _seed_role(cfg, "job-1", "Example")
    url = "https://jobs.ashbyhq.com/Example/job-1/application"
    client = _client(cfg, tmp_path, monkeypatch)
    job = {"job_id": "job-1", "company": "Example", "title": "Backend Engineer", "url": url, "apply_profile": "casey", "resume_ready": True, "status": "new"}
    monkeypatch.setattr(agent_lab, "runner_candidates", lambda _: [job])
    client.post("/api/agent-lab/runner/start", json={"apply_profile": "casey", "max_jobs": 1})
    claim = client.post("/api/agent-lab/runner/claim", json={"worker_id": "w"}).json()
    lease = {"lease_id": claim["lease_id"], "token": claim["token"], "worker_id": "w"}
    run = _permit(client, "job-1", url, lease)
    assert client.post("/api/agent-lab/submission-result", json={"run_id": run["id"], "attempt_id": run["attempt_id"], "outcome": "blocked", "evidence": REFUSAL}).status_code == 200
    with store.connect_view(cfg) as conn:
        assert store.get_role(conn, "job-1")["status"] == "new", "the batch's own callback records it next"
    finished = client.post("/api/agent-lab/runner/finish", json={**lease, "outcome": "blocked", "evidence": REFUSAL})
    assert finished.status_code == 200 and finished.json()["pause_reason"] != "terminal_callback_failed"
    record = next(j for j in finished.json()["jobs"] if j["job"]["job_id"] == "job-1")
    assert record["status"] == "blocked"
    with store.connect_view(cfg) as conn:
        assert store.get_role(conn, "job-1")["status"] == "skip" and norm_key("Example") in store.active_cooldowns(conn, cfg)


def test_a_panel_refusal_that_cannot_be_recorded_still_answers_the_panel(cfg, tmp_path, monkeypatch):
    # The page was another address by the time the employer answered: the URL check refuses, the receipt stands.
    _seed_role(cfg, "job-1", "Example")
    client = _client(cfg, tmp_path, monkeypatch)
    run = _permit(client, "job-1", "https://jobs.ashbyhq.com/Other/elsewhere/application")
    reply = client.post("/api/agent-lab/submission-result", json={"run_id": run["id"], "attempt_id": run["attempt_id"], "outcome": "blocked", "evidence": REFUSAL})
    assert reply.status_code == 200 and "没能设冷却" in reply.json()["events"][-1]["text"]
    with store.connect_view(cfg) as conn:
        assert store.get_role(conn, "job-1")["status"] == "new" and norm_key("Example") not in store.active_cooldowns(conn, cfg)


def test_a_refusal_after_her_own_submit_closes_the_role_and_freezes_the_company(cfg, tmp_path, monkeypatch):
    _seed_role(cfg, "job-1", "Example")
    client = _client(cfg, tmp_path, monkeypatch)
    headers = _pair(client)
    reply = client.post("/api/agent-lab/hand-refusal", json={"job_id": "job-1", "evidence": "Your application is frozen. You may reapply after 90 days."}, headers=headers)
    assert reply.status_code == 200 and reply.json()["status"] == "skip"
    assert reply.json()["cooldown_until"] == (datetime.date.fromisoformat(store.la_today()) + datetime.timedelta(days=90)).isoformat()
    with store.connect_view(cfg) as conn:
        role = store.get_role(conn, "job-1")
        assert role["status"] == "skip" and "雇主拒收" in role["notes"] and norm_key("Example") in store.active_cooldowns(conn, cfg)
    # The role is no longer new, so a second report changes nothing.
    assert client.post("/api/agent-lab/hand-refusal", json={"job_id": "job-1", "evidence": REFUSAL}, headers=headers).status_code == 409


def test_only_the_extension_can_report_a_hand_refusal(cfg, tmp_path, monkeypatch):
    _seed_role(cfg, "job-1", "Example")
    client = _client(cfg, tmp_path, monkeypatch)
    assert client.post("/api/agent-lab/hand-refusal", json={"job_id": "job-1", "evidence": REFUSAL}).status_code == 403
    assert client.post("/api/agent-lab/hand-refusal", json={"job_id": "job-1", "evidence": REFUSAL}, headers={"X-JobScan-Review": "nope"}).status_code == 403
    with store.connect_view(cfg) as conn:
        assert store.get_role(conn, "job-1")["status"] == "new"


def test_a_hand_refusal_about_another_page_or_role_closes_nothing(cfg, tmp_path, monkeypatch):
    _seed_role(cfg, "job-1", "Example")
    _seed_role(cfg, "job-2", "Applied", status="applied", applied_date="2026-11-01")
    client = _client(cfg, tmp_path, monkeypatch)
    headers = _pair(client)
    assert client.post("/api/agent-lab/hand-refusal", json={"job_id": "ghost", "evidence": REFUSAL}, headers=headers).status_code == 404
    assert client.post("/api/agent-lab/hand-refusal", json={"job_id": "job-2", "evidence": REFUSAL}, headers=headers).status_code == 409
    other = {"job_id": "job-1", "evidence": REFUSAL, "url": "https://jobs.ashbyhq.com/Other/elsewhere/application"}
    assert client.post("/api/agent-lab/hand-refusal", json=other, headers=headers).status_code == 409
    with store.connect_view(cfg) as conn:
        assert store.get_role(conn, "job-1")["status"] == "new" and norm_key("Example") not in store.active_cooldowns(conn, cfg)
    same = {**other, "url": "https://jobs.ashbyhq.com/Example/job-1/application?utm_source=x"}
    assert client.post("/api/agent-lab/hand-refusal", json=same, headers=headers).status_code == 200
