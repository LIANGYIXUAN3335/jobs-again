"""Extension workspace identity (2026-09-23) + ✎ queue starts tailoring immediately.

`identity=` on /api/lookup, /api/top-role, /api/profile pins a name card for the Chrome profile
(the owner's 小号 `alex`): the role is switched like the review page's badge does, and the
payload's profile carries that card's email. Applied roles are never switched. Queueing a role
with ✎ hands every queued role to tools/auto_tailor.sh at once (Popen is stubbed here — the
real script would start claude workers); when a batch is running (lock dir) it waits.
"""
from app import store
from tests.test_store import role_row


def _client(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    from app import web as webmod
    monkeypatch.setattr(webmod, "cfg", cfg)
    return webmod, TestClient(webmod.app)


def test_lookup_identity_switches_the_role_but_never_an_applied_one(cfg, monkeypatch):
    cfg = {**cfg, "apply_profiles": {"default": "casey", "profiles": {
        "casey": {"label": "A", "display_name": "Casey Example"},
        "alex": {"label": "Y", "display_name": "Alex Example", "first_name": "Alex",
                   "preferred_name": "Alex", "email": "sample-5803d105@example.com", "phone_national": "202-555-0142"}},
        "companies": {}}}
    webmod, client = _client(cfg, monkeypatch)
    jobs = store.connect_jobs(cfg)
    a = role_row("gh:acme:11", company="Acme"); a["apply_url"] = "https://job-boards.greenhouse.io/acme/jobs/11"
    b = role_row("gh:acme:22", company="Acme"); b["apply_url"] = "https://job-boards.greenhouse.io/acme/jobs/22"
    store.upsert_roles(jobs, [a, b])
    view = store.connect_view(cfg)
    store.set_status(view, "gh:acme:22", "applied", "base")

    r = client.get("/api/lookup", params={"url": a["apply_url"]}).json()
    assert r["apply_profile"] == "casey" and r["profile"]["email"] == cfg["profile"]["email"]
    r = client.get("/api/lookup", params={"url": a["apply_url"], "identity": "alex"}).json()
    assert r["apply_profile"] == "alex" and r["profile"]["email"] == "sample-5803d105@example.com"
    assert r["profile"]["first_name"] == "Alex"
    assert store.get_role(view, "gh:acme:11")["apply_profile"] == "alex"          # persisted
    # applied → the selected card is overlaid on THIS form but never written to the row
    # (2026-09-25: it used to be ignored, and a Alex Chrome filled Casey on applied postings)
    r = client.get("/api/lookup", params={"url": b["apply_url"], "identity": "alex"}).json()
    assert r["apply_profile"] == "alex" and r["profile"]["email"] == "sample-5803d105@example.com"
    assert store.get_role(view, "gh:acme:22")["apply_profile"] is None                 # history untouched
    # unknown identity → ignored
    r = client.get("/api/lookup", params={"url": a["apply_url"], "identity": "nobody"}).json()
    assert r["apply_profile"] == "alex"                                              # unchanged
    p = client.get("/api/profile", params={"identity": "alex"}).json()["profile"]
    assert p["email"] == "sample-5803d105@example.com" and p["first_name"] == "Alex"
    p0 = client.get("/api/profile").json()["profile"]
    assert p0["email"] == cfg["profile"]["email"]


def test_queue_tailoring_starts_the_workers_or_waits_for_the_lock(cfg, monkeypatch, tmp_path):
    cfg = {**cfg, "auto_tailor": {**(cfg.get("auto_tailor") or {}), "manifest": str(tmp_path / "m.json")},
           "output_dirs": {"prompts": str(tmp_path / "prompts"), "resumes": str(tmp_path / "resumes")}}
    webmod, client = _client(cfg, monkeypatch)
    monkeypatch.setattr(webmod, "_AUTOTAILOR_LOGS", tmp_path / "logs")
    webmod._queue_attempts.clear()
    launched = []
    class FakePopen:
        def __init__(self, argv, **kw): launched.append((argv, kw.get("env") or {}))
    monkeypatch.setattr(webmod.subprocess, "Popen", FakePopen)
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [role_row("gh:x:1", company="X"), role_row("gh:x:2", company="X")])

    r = client.post("/api/roles/gh:x:1/queue-tailoring", json={"angle": None}).json()
    assert r["ok"] and r["started"] and r["queued"] == 1
    argv, env = launched[-1]
    assert argv == ["bash", "tools/auto_tailor.sh"] and env["JOBSCAN_MANIFEST"].endswith("m.manual.json")
    import json
    items = json.loads((tmp_path / "m.manual.json").read_text())
    assert [i["job_id"] for i in items] == ["gh:x:1"] and items[0]["prompt"].endswith(".md")

    # a batch is running → the second role waits, nothing is launched
    lock = tmp_path / "logs" / ".autotailor.lock"; lock.mkdir(parents=True)
    n = len(launched)
    r = client.post("/api/roles/gh:x:2/queue-tailoring", json={"angle": None}).json()
    assert r["ok"] and not r["started"] and r["waiting"] and r["queued"] == 2
    assert len(launched) == n
    # lock clears → the runner's single tick launches both queued roles
    lock.rmdir()
    view = store.connect_view(cfg)
    res = webmod._start_queued(view, "test tick")
    assert res["started"] and res["queued"] == 2 and len(launched) == n + 1


def test_applied_freezes_the_effective_card_and_lookup_relettters_a_stale_pdf(cfg, monkeypatch, tmp_path):
    """2026-09-25: Nvidia/Nuro/Cohere were on the 法名 company list, the form filled as Alex,
    but the attached PDF still said Casey (rendered before the mapping) and the applied row
    recorded no card. Two guarantees now: (1) the card in force when a role is marked applied
    is written to the row; (2) lookup/resume.pdf re-render a PDF whose name prefix disagrees
    with the card before the extension can attach it."""
    cfg = {**cfg, "apply_profiles": {"default": "casey", "profiles": {
        "casey": {"label": "A", "display_name": "Casey Example"},
        "alex": {"label": "Y", "display_name": "Alex Example", "first_name": "Alex",
                   "preferred_name": "Alex", "email": "sample-5803d105@example.com", "phone_national": "202-555-0142"}},
        "companies": {"nvidia": "alex"}}}
    webmod, client = _client(cfg, monkeypatch)
    jobs = store.connect_jobs(cfg)
    a = role_row("gh:nvidia:1", company="Nvidia"); a["apply_url"] = "https://job-boards.greenhouse.io/nvidia/jobs/1"
    store.upsert_roles(jobs, [a])
    view = store.connect_view(cfg)
    # a PDF rendered under the OLD name, plus the YAML that would re-render it
    res_dir = tmp_path / "resumes"; res_dir.mkdir(exist_ok=True)
    stale = res_dir / "Casey Example - Nvidia - X [1] - 80.pdf"; stale.write_bytes(b"%PDF-1.4 stale")
    (res_dir / "nvidia_gh_nvidia_1.yaml").write_text("job_id: gh:nvidia:1\n")
    store.set_tailor(view, "gh:nvidia:1", tailor_status="ready", tailored_resume_path=str(stale))
    calls = []
    def fake_ingest(cfg_, conn_, ypath):
        calls.append(ypath.name)
        fresh = res_dir / "Alex Example - Nvidia - X [1] - 80.pdf"; fresh.write_bytes(b"%PDF-1.4 fresh")
        store.set_tailor(conn_, "gh:nvidia:1", tailored_resume_path=str(fresh))
        return {"pdf": str(fresh)}
    monkeypatch.setattr(webmod.tailoring, "ingest_file", fake_ingest)

    r = client.get("/api/lookup", params={"url": a["apply_url"]}).json()
    assert r["apply_profile"] == "alex" and r["profile"]["email"] == "sample-5803d105@example.com"
    assert calls == ["nvidia_gh_nvidia_1.yaml"]                                        # re-lettered once
    assert store.get_role(view, "gh:nvidia:1")["tailored_resume_path"].endswith("Alex Example - Nvidia - X [1] - 80.pdf")
    client.get("/api/lookup", params={"url": a["apply_url"]})
    assert calls == ["nvidia_gh_nvidia_1.yaml"]                                        # now consistent → no re-render

    client.post("/api/roles/gh:nvidia:1/status", json={"status": "applied", "resume_variant": "tailored"})
    row = store.get_role(view, "gh:nvidia:1")
    assert row["apply_profile"] == "alex"                                            # frozen on the row
    assert store.effective_profile_key(cfg, row) == "alex"                           # survives the applied flip
