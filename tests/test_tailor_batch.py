"""「✎ 一键 tailor 当前列表」(owner 2026-09-23): POST /api/tailor-batch rebuilds the review
list from the page's filters, queues every row without a résumé, and hands the manifest to
tools/auto_tailor.sh detached. Exercised against the throwaway DBs with the launch FAKED —
the real script starts four headless claude sessions, which a test must never do."""
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config as cfgmod, store
from app import web as webmod
from tests.test_store import role_row


class FakePopen:
    calls: list = []

    def __init__(self, argv, **kw):
        self.argv, self.kw = argv, kw
        FakePopen.calls.append(self)

    def poll(self):
        return None


@pytest.fixture
def batch(cfg, tmp_path, monkeypatch):
    """TestClient on throwaway DBs + tmp manifest/lock/log; Popen faked. Budget off so
    fetch_view(status='new') returns every seeded row rather than a daily selection."""
    cfg = {**cfg, "application_budget": {"enabled": False},
           "auto_tailor": {**(cfg.get("auto_tailor") or {}),
                           "manifest": str(tmp_path / "scratch" / "autotailor_manifest.json"),
                           "manual_batch_cap": 150}}
    monkeypatch.setattr(webmod, "cfg", cfg)
    monkeypatch.setattr(webmod, "_AUTOTAILOR_LOGS", tmp_path / "logs")
    monkeypatch.setattr(webmod.subprocess, "Popen", FakePopen)
    monkeypatch.delenv("JOBSCAN_CONFIG", raising=False)
    monkeypatch.delenv("JOBSCAN_WS", raising=False)
    FakePopen.calls = []
    jobs = store.connect_jobs(cfg)
    drop = role_row("gh:d:1", company="Delta", title="Backend Engineer", score=95.0)
    drop["verdict"] = "drop"
    store.upsert_roles(jobs, [
        role_row("gh:a:1", company="Acme", title="Backend Engineer", score=80.0),
        role_row("gh:b:1", company="Bravo", title="Platform Engineer", score=70.0),
        role_row("gh:c:1", company="Charlie", title="Backend Engineer", score=90.0),
        drop,
    ])
    view = store.connect_view(cfg)
    store.set_tailor(view, "gh:c:1", tailor_status="ready", tailored_resume_path="/tmp/c.pdf")
    view.close()
    return TestClient(webmod.app), cfg, tmp_path


def test_dry_run_counts_only_roles_without_a_resume(batch):
    client, cfg, tmp = batch
    r = client.post("/api/tailor-batch", json={"dry_run": True})
    assert r.status_code == 200
    body = r.json()
    # Charlie is already `ready`, Delta is verdict=drop — neither gets a slot
    assert body["queued"] == 2 and body["capped"] == 0
    assert [tuple(x) for x in body["sample"]] == [("Acme", "Backend Engineer"),
                                                  ("Bravo", "Platform Engineer")]   # rank desc
    # a dry run writes NOTHING and starts nothing
    assert not (tmp / "scratch").exists()
    assert FakePopen.calls == []
    view = store.connect_view(cfg)
    # no role_state row yet → None; the point is it was NOT flipped to "queued"
    assert store.get_role(view, "gh:a:1")["tailor_status"] in (None, "none")


def test_cap_keeps_the_top_ranked_and_reports_the_rest(batch):
    client, cfg, tmp = batch
    cfg["auto_tailor"]["manual_batch_cap"] = 1
    body = client.post("/api/tailor-batch", json={"dry_run": True}).json()
    assert body["queued"] == 1 and body["capped"] == 1
    assert tuple(body["sample"][0]) == ("Acme", "Backend Engineer")


def test_real_call_writes_manifest_and_launches_script_in_the_workspace(batch, monkeypatch):
    client, cfg, tmp = batch
    # The second workspace's web server runs with JOBSCAN_CONFIG=config.robin.yaml. The cfg
    # fixture is the owner's (workspace.name Casey, no config.casey.yaml), so the suffix must
    # come from the env var's filename — exactly how tools/daily_scan.sh derives JOBSCAN_WS.
    monkeypatch.setenv("JOBSCAN_CONFIG", "config.robin.yaml")
    r = client.post("/api/tailor-batch", json={"dry_run": False, "status": "new"})
    assert r.status_code == 200
    body = r.json()
    assert body["queued"] == 2 and body["capped"] == 0 and body["started"] is True
    mpath = Path(body["manifest"])
    assert mpath == tmp / "scratch" / "autotailor_manifest.manual.json"   # nightly path → .manual.json
    items = json.loads(mpath.read_text())
    assert [it["job_id"] for it in items] == ["gh:a:1", "gh:b:1"]
    for it in items:                       # same shape prep_autotailor.py writes
        assert set(it) == {"job_id", "company", "title", "prompt", "resume_out", "score"}
        assert Path(it["prompt"]).exists()
        assert Path(it["resume_out"]).parent == Path(cfg["output_dirs"]["resumes"])
    view = store.connect_view(cfg)
    assert store.get_role(view, "gh:a:1")["tailor_status"] == "queued"
    assert store.get_role(view, "gh:c:1")["tailor_status"] == "ready"      # untouched

    assert len(FakePopen.calls) == 1
    call = FakePopen.calls[0]
    assert call.argv == ["bash", "tools/auto_tailor.sh"]
    assert call.kw["cwd"] == str(cfgmod.ROOT) and call.kw["start_new_session"] is True
    env = call.kw["env"]
    assert env["JOBSCAN_MANIFEST"] == str(mpath)
    assert env["JOBSCAN_CONFIG"] == "config.robin.yaml"     # inherited → child stays in workspace
    assert env["JOBSCAN_WS"] == "robin"
    assert (tmp / "logs" / "autotailor.robin.manual.log").exists()


def test_owner_workspace_launches_without_a_suffix(batch):
    client, cfg, tmp = batch
    body = client.post("/api/tailor-batch", json={}).json()
    assert body["started"] is True
    env = FakePopen.calls[0].kw["env"]
    assert "JOBSCAN_WS" not in env and "JOBSCAN_CONFIG" not in env
    assert (tmp / "logs" / "autotailor.manual.log").exists()


def test_409_when_a_batch_is_already_running(batch):
    client, cfg, tmp = batch
    (tmp / "logs" / ".autotailor.lock").mkdir(parents=True)     # the script's own lock dir
    r = client.post("/api/tailor-batch", json={})
    assert r.status_code == 409
    assert "已有一批在跑" in r.json()["detail"]
    assert FakePopen.calls == []
    assert not (tmp / "scratch" / "autotailor_manifest.manual.json").exists()
    view = store.connect_view(cfg)
    assert store.get_role(view, "gh:a:1")["tailor_status"] in (None, "none")   # nothing flipped to queued


def test_status_counts_written_and_ingested(batch):
    client, cfg, tmp = batch
    s = client.get("/api/tailor-batch/status").json()
    assert s == {"running": False, "total": 0, "written": 0, "ingested": 0,
                 "log_tail": [], "manifest_mtime": None}

    body = client.post("/api/tailor-batch", json={}).json()
    items = json.loads(Path(body["manifest"]).read_text())
    Path(items[0]["resume_out"]).write_text("job_id: gh:a:1\n")        # one worker finished one
    s = client.get("/api/tailor-batch/status").json()
    assert (s["total"], s["written"], s["ingested"], s["running"]) == (2, 1, 0, False)
    assert s["manifest_mtime"]

    view = store.connect_view(cfg)                                     # …and ingest marked it
    store.set_tailor(view, "gh:a:1", tailor_status="ready", tailored_resume_path="/tmp/a.pdf")
    view.close()
    (tmp / "logs" / ".autotailor.lock").mkdir()
    (tmp / "logs" / "autotailor.manual.log").write_text("l1\nl2\n\nl3\nl4\n", encoding="utf-8")
    s = client.get("/api/tailor-batch/status").json()
    assert (s["written"], s["ingested"], s["running"]) == (1, 1, True)
    assert s["log_tail"] == ["l2", "l3", "l4"]                           # blank lines skipped


def test_auto_tailor_sh_parses_and_honours_the_override():
    """bash -n on the real script, and the override lines are really there: with
    JOBSCAN_MANIFEST set the prep step must be skipped (the review page already wrote it)."""
    script = cfgmod.ROOT / "tools" / "auto_tailor.sh"
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    text = script.read_text()
    assert 'MANIFEST="$JOBSCAN_MANIFEST"' in text
    assert "skipping prep" in text
