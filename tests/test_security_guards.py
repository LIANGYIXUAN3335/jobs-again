"""Each guard must actually fire on the thing it claims to catch."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "security_guards", Path(__file__).resolve().parent.parent / "tools" / "security_guards.py")


def _load(root: Path):
    m = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(m)
    m.ROOT = root
    m.errors.clear()
    m.warnings.clear()
    return m


@pytest.fixture
def repo(tmp_path):
    """A minimal clean repo the guards should pass."""
    (tmp_path / ".gitignore").write_text(
        "\n".join(["secrets.yaml", "secrets/", ".env", "data/*.db", "/output/",
                   "**/token*.json", "**/*credential*.json",
                   "**/*client_secret*.json", "*.pem", ".backups/"]) + "\n")
    (tmp_path / "requirements.txt").write_text("fastapi\nhttpx\npytest\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def _commit(root: Path, rel: str, body: str = "x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    subprocess.run(["git", "add", "-f", rel], cwd=root, check=True)


# ---------------------------------------------------------------- gitignore

def test_clean_repo_passes(repo):
    m = _load(repo)
    m.check_gitignore(); m.check_requirements(); m.check_modes()
    assert m.errors == []


def test_deleted_ignore_rule_is_caught(repo):
    (repo / ".gitignore").write_text("data/*.db\n")
    m = _load(repo); m.check_gitignore()
    assert any("secrets.yaml" in e for e in m.errors)


def test_negation_reincluding_ignored_files_is_caught(repo):
    (repo / ".gitignore").write_text(
        (repo / ".gitignore").read_text() + "!secrets.yaml\n")
    m = _load(repo); m.check_gitignore()
    assert any("re-includes" in e for e in m.errors)


def test_missing_gitignore_is_caught(tmp_path):
    m = _load(tmp_path); m.check_gitignore()
    assert any("missing entirely" in e for e in m.errors)


# ---------------------------------------------------------------- tracked files

def test_tracked_secrets_file_is_caught_even_when_gitignored(repo):
    """The whole point: an ignore rule added AFTER the commit does not untrack."""
    _commit(repo, "secrets.yaml", "api_key: sk-real\n")
    m = _load(repo); m.check_tracked()
    assert any("secrets.yaml" in e for e in m.errors)


def test_tracked_oauth_token_is_caught(repo):
    _commit(repo, "data/token_gcal.json", "{}")
    m = _load(repo); m.check_tracked()
    assert any("OAuth token" in e for e in m.errors)


def test_tracked_database_is_caught(repo):
    _commit(repo, "data/jobs.db")
    m = _load(repo); m.check_tracked()
    assert any("database" in e for e in m.errors)


def test_example_templates_stay_allowed(repo):
    _commit(repo, "secrets.example.yaml", "api_key: PUT_YOURS_HERE\n")
    m = _load(repo); m.check_tracked()
    assert m.errors == []


# ---------------------------------------------------------------- file modes

def test_world_readable_secrets_file_is_caught(repo):
    p = repo / "secrets.yaml"; p.write_text("k: v"); p.chmod(0o644)
    m = _load(repo); m.check_modes()
    assert any("chmod 600" in e for e in m.errors)


def test_world_readable_secrets_dir_wants_700(repo):
    d = repo / "secrets"; d.mkdir(); d.chmod(0o755)
    m = _load(repo); m.check_modes()
    assert any("chmod 700" in e for e in m.errors)


def test_owner_only_mode_passes(repo):
    p = repo / "secrets.yaml"; p.write_text("k: v"); p.chmod(0o600)
    m = _load(repo); m.check_modes()
    assert m.errors == []


# ---------------------------------------------------------------- requirements

def test_smuggled_dependency_is_caught(repo):
    (repo / "requirements.txt").write_text("fastapi\nrequests\n")
    m = _load(repo); m.check_requirements()
    assert any("requests" in e for e in m.errors)


def test_version_pins_and_extras_still_parse(repo):
    (repo / "requirements.txt").write_text("fastapi>=0.100\nhttpx[http2]==0.27.0\n# note\n")
    m = _load(repo); m.check_requirements()
    assert m.errors == []


# ---------------------------------------------------------------- claude settings

def _settings(root: Path, payload: dict):
    d = root.parent.parent / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(json.dumps(payload))


def test_hooks_are_an_error(tmp_path):
    root = tmp_path / "a" / "b"; root.mkdir(parents=True)
    _settings(root, {"hooks": {"Stop": []}})
    m = _load(root); m.check_claude_settings()
    assert any("hooks" in e for e in m.errors)


def test_blanket_bash_permission_warns(tmp_path):
    root = tmp_path / "a" / "b"; root.mkdir(parents=True)
    _settings(root, {"permissions": {"allow": ["Bash(*)"]}})
    m = _load(root); m.check_claude_settings()
    assert any("broad command" in w for w in m.warnings)


def test_pip_install_wildcard_warns(tmp_path):
    root = tmp_path / "a" / "b"; root.mkdir(parents=True)
    _settings(root, {"permissions": {"allow": ["Bash(pip install *)"]}})
    m = _load(root); m.check_claude_settings()
    assert any("broad command" in w for w in m.warnings)


def test_narrow_permission_is_fine(tmp_path):
    root = tmp_path / "a" / "b"; root.mkdir(parents=True)
    _settings(root, {"permissions": {"allow": ["Bash(pytest:*)", "WebSearch"]}})
    m = _load(root); m.check_claude_settings()
    assert m.warnings == [] and m.errors == []
