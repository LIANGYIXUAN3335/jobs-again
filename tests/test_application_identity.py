import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from app import application_identity, store, tailoring
from app.application_identity import prepare_casey_claim
from app.config import norm_key, norm_title


@pytest.fixture
def prepared_case(cfg, tmp_path, monkeypatch):
    cfg = {**cfg, "profile": {"first_name": "Alex", "last_name": "Example", "preferred_name": "Alex",
                              "legal_first_name": "Alex", "email": "old@example.test", "phone": "555-123-4567"},
           "apply_profiles": {"default": "alex", "profiles": {
               "casey": {"display_name": "Casey Example", "first_name": "Casey", "preferred_name": "Casey",
                          "email": "sample-5fbfd918@example.com"},
               "alex": {"display_name": "Alex Example"}}}, "cooldowns": {}}
    value = {key: None for key in store.ROLE_COLS}
    value.update(job_id="gh:example:123", ats="greenhouse", company="Example", company_norm=norm_key("Example"),
                 title="Software Engineer", title_norm=norm_title("Software Engineer"),
                 apply_url="https://jobs.example.test/123", jd_hash="original-jd")
    with store.connect_jobs(cfg) as conn:
        store.upsert_roles(conn, [value])
    original = tmp_path / "Alex Example - Example [123] - 87.pdf"
    original.write_bytes(b"%PDF-original Alex identity")
    with store.connect_view(cfg) as conn:
        store.upsert_state(conn, value["job_id"], status="new", notes="Keep existing note.",
                           tailor_status="ready", tailored_resume_path=str(original))
    source = tmp_path / "tailored.yaml"
    source.write_text(yaml.safe_dump({"job_id": value["job_id"], "summary": "A grounded summary.", "experience": []}))
    base = {"name": "Alex Example", "contact": {"email": "old@example.test", "phone": "555-123-4567"},
            "education": [], "skills_master": {"Languages": ["Python"]}, "experience": [], "projects": []}
    rendered = []
    monkeypatch.setattr(tailoring, "resume_yaml_path", lambda *_: source)
    monkeypatch.setattr(tailoring, "load_base", lambda *_: copy.deepcopy(base))
    monkeypatch.setattr(tailoring, "validate_tailored", lambda *_args, **_kwargs: None)
    def render(merged, output):
        rendered.append((copy.deepcopy(merged), output))
        output.write_bytes(b"%PDF-" + json.dumps(merged).encode())
        return output
    monkeypatch.setattr(tailoring, "render_pdf", render)
    def inspect(path, _name, _profile):
        data = path.read_bytes()
        return {"identity_verified": b"Casey Example" in data and b"sample-5fbfd918@example.com" in data,
                "sha256": hashlib.sha256(data).hexdigest()}
    monkeypatch.setattr(application_identity, "inspect_resume", inspect)
    job = {"job_id": value["job_id"], "url": value["apply_url"], "status": "new",
           "apply_profile": "casey", "company": value["company"], "title": value["title"], "resume_ready": True}
    return {"cfg": cfg, "job": job, "original": original, "source": source, "base": base,
            "rendered": rendered, "render": render, "inspect": inspect}


def current(case):
    with store.connect_view(case["cfg"]) as conn:
        return store.get_role(conn, case["job"]["job_id"])


def update(case, **changes):
    with store.connect_view(case["cfg"]) as conn:
        store.upsert_state(conn, case["job"]["job_id"], **changes)


def assert_original(case):
    assert case["original"].read_bytes() == b"%PDF-original Alex identity"


def test_unlocked_new_role_is_relettered_without_mutating_facts_or_defaults(prepared_case):
    c = prepared_case
    before_cfg, before_base = copy.deepcopy(c["cfg"]), copy.deepcopy(c["base"])
    assert prepare_casey_claim(c["cfg"], c["job"]) == c["job"]
    saved = current(c)
    artifact = Path(saved["tailored_resume_path"])
    assert saved["apply_profile"] == "casey" and saved["status"] == "new"
    assert saved["notes"] == "Keep existing note." and saved["applied_date"] is None
    assert artifact.is_file() and artifact != c["original"]
    assert artifact.name.startswith("Casey Example - Example")
    assert store.resume_score_from_path(str(artifact)) == 87
    assert artifact.stat().st_mode & 0o777 == 0o600
    merged = c["rendered"][0][0]
    assert merged["name"] == "Casey Example" and merged["contact"]["email"] == "sample-5fbfd918@example.com"
    assert merged["contact"]["phone"] == c["base"]["contact"]["phone"]
    assert merged["summary"] == "A grounded summary."
    assert c["cfg"] == before_cfg and c["base"] == before_base
    assert c["cfg"]["profile"]["legal_first_name"] == "Alex"
    assert_original(c)


@pytest.mark.parametrize("changes", [{"apply_profile": "alex"}, {"apply_profile": "casey2"},
                                      {"status": "applied"}, {"status": "interviewing"},
                                      {"status": "expired"}, {"tailor_status": "none"}])
def test_locked_or_historical_roles_are_unchanged(prepared_case, changes):
    c = prepared_case
    update(c, **changes)
    before = current(c)
    with pytest.raises(ValueError):
        prepare_casey_claim(c["cfg"], c["job"])
    assert current(c) == before and not c["rendered"]
    assert_original(c)


def test_already_casey_valid_pdf_does_not_render_again(prepared_case):
    c = prepared_case
    prepare_casey_claim(c["cfg"], c["job"])
    first = current(c)
    prepare_casey_claim(c["cfg"], c["job"])
    assert current(c) == first and len(c["rendered"]) == 1
    assert_original(c)


@pytest.mark.parametrize("mode", ["exception", "bad_identity"])
def test_render_failure_does_not_publish_identity_or_delete_original(prepared_case, monkeypatch, mode):
    c = prepared_case
    before = current(c)
    if mode == "exception":
        def fail(_merged, output):
            output.write_bytes(b"partial")
            raise tailoring.TailorError("synthetic render failure")
        monkeypatch.setattr(tailoring, "render_pdf", fail)
    else:
        monkeypatch.setattr(application_identity, "inspect_resume", lambda *_: {"identity_verified": False})
    with pytest.raises((ValueError, tailoring.TailorError)):
        prepare_casey_claim(c["cfg"], c["job"])
    assert current(c) == before
    assert_original(c)
    assert not list((Path(c["cfg"]["output_dirs"]["resumes"]) / "agent_claims").glob("claim-*"))


def test_isolated_output_does_not_overwrite_an_existing_same_filename(prepared_case):
    c = prepared_case
    output_dir = Path(c["cfg"]["output_dirs"]["resumes"])
    output_dir.mkdir(parents=True)
    name = tailoring.friendly_pdf_prefix("Example", "Software Engineer", c["job"]["job_id"], "Casey Example") + "87.pdf"
    collision = output_dir / name
    collision.write_bytes(b"existing other artifact")
    prepare_casey_claim(c["cfg"], c["job"])
    assert collision.read_bytes() == b"existing other artifact"
    assert Path(current(c)["tailored_resume_path"]) != collision
    assert_original(c)


@pytest.mark.parametrize("changes", [{"apply_profile": "alex"}, {"apply_profile": "casey"},
                                      {"status": "applied"}, {"duplicate_of": "other-role"},
                                      {"status_updated_at": "different-time"},
                                      {"tailored_resume_path": "/tmp/synthetic-new-pointer.pdf"}])
def test_role_changed_during_render_is_not_overwritten(prepared_case, monkeypatch, changes):
    c = prepared_case
    def render(merged, output):
        c["render"](merged, output)
        update(c, **changes)
    monkeypatch.setattr(tailoring, "render_pdf", render)
    with pytest.raises(ValueError):
        prepare_casey_claim(c["cfg"], c["job"])
    with store.connect_view(c["cfg"]) as conn:
        row = dict(conn.execute("SELECT * FROM role_state WHERE job_id=?", (c["job"]["job_id"],)).fetchone())
    for key, expected in changes.items():
        assert row[key] == expected
    assert_original(c)
    assert not c["rendered"][0][1].exists()


@pytest.mark.parametrize("target", ["source", "original"])
def test_file_changed_during_render_is_not_replaced(prepared_case, monkeypatch, target):
    c = prepared_case
    def render(merged, output):
        c["render"](merged, output)
        c[target].write_bytes(b"concurrent file update")
    monkeypatch.setattr(tailoring, "render_pdf", render)
    with pytest.raises(ValueError):
        prepare_casey_claim(c["cfg"], c["job"])
    assert current(c)["apply_profile"] is None
    assert current(c)["tailored_resume_path"] == str(c["original"])
    assert c[target].read_bytes() == b"concurrent file update"
    assert not c["rendered"][0][1].exists()


def test_verified_fast_path_rechecks_mutable_role_under_lock(prepared_case, monkeypatch):
    c = prepared_case
    prepare_casey_claim(c["cfg"], c["job"])
    def inspect(*args):
        evidence = c["inspect"](*args)
        update(c, status="applied")
        return evidence
    monkeypatch.setattr(application_identity, "inspect_resume", inspect)
    with pytest.raises(ValueError):
        prepare_casey_claim(c["cfg"], c["job"])
    assert current(c)["status"] == "applied"
    assert len(c["rendered"]) == 1


def test_missing_yaml_wrong_job_and_cooldown_stop_before_publication(prepared_case):
    c = prepared_case
    original_yaml = c["source"].read_bytes()
    c["source"].unlink()
    with pytest.raises(FileNotFoundError):
        prepare_casey_claim(c["cfg"], c["job"])
    c["source"].write_text("job_id: wrong-job\n")
    with pytest.raises(ValueError):
        prepare_casey_claim(c["cfg"], c["job"])
    c["source"].write_bytes(original_yaml)
    with store.connect_view(c["cfg"]) as conn:
        store.set_company_cooldown(conn, "Example", days=1)
    with pytest.raises(ValueError):
        prepare_casey_claim(c["cfg"], c["job"])
    assert current(c)["apply_profile"] is None and not c["rendered"]
    assert_original(c)


def test_company_routed_to_another_card_is_never_prepared_as_casey(prepared_case):
    cfg = {**prepared_case["cfg"]}
    cfg["apply_profiles"] = {**cfg["apply_profiles"], "companies": {norm_key("Example"): "alex"}}
    with pytest.raises(ValueError):
        prepare_casey_claim(cfg, prepared_case["job"])
    assert current(prepared_case)["apply_profile"] in {None, ""}
    assert prepared_case["rendered"] == []


def test_company_with_an_application_under_another_card_is_never_prepared(prepared_case):
    value = {key: None for key in store.ROLE_COLS}
    value.update(job_id="gh:example:999", ats="greenhouse", company="Example", company_norm=norm_key("Example"),
                 title="Other Role", title_norm=norm_title("Other Role"), apply_url="https://jobs.example.test/999")
    cfg = prepared_case["cfg"]
    with store.connect_jobs(cfg) as conn:
        store.upsert_roles(conn, [value])
    with store.connect_view(cfg) as conn:
        store.upsert_state(conn, value["job_id"], status="applied", apply_profile="alex")
    with pytest.raises(ValueError):
        prepare_casey_claim(cfg, prepared_case["job"])
    assert prepared_case["rendered"] == []


def test_every_card_has_its_own_phone_so_it_cannot_inherit_the_default_cards():
    from app import config as cfgmod
    apply_profiles = cfgmod.load_config()["apply_profiles"]
    for key, card in apply_profiles["profiles"].items():
        if key != apply_profiles.get("default"):
            assert card.get("phone") and card.get("phone_national"), key
    assert "408" in apply_profiles["profiles"]["casey"]["phone_national"]
