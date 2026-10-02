"""ai.complete(): the one door every model call goes through (owner 2026-09-25). `cli` runs the
logged-in Claude Code on the subscription; any CLI failure falls back to the API key — loudly."""
import json
import sys
from types import SimpleNamespace

from app import ai


def _fake_api(monkeypatch, reply):
    calls = []
    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])
    mod = SimpleNamespace(Anthropic=lambda api_key=None: SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return calls


def _fake_cli(monkeypatch, *, result="from cli", rc=0, is_error=False, boom=False):
    runs = []
    def run(argv, **kw):
        runs.append((argv, kw))
        if boom:
            raise TimeoutError("slow")
        env = json.dumps({"result": result, "is_error": is_error})
        return SimpleNamespace(returncode=rc, stdout=env + "\n", stderr="")
    import subprocess
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(ai, "cli_available", lambda: True)
    return runs


def test_cli_backend_runs_claude_p_with_tools_off_and_own_system_prompt(cfg, monkeypatch):
    cfg = {**cfg, "ai": {**cfg["ai"], "backend": "cli", "cli_model": "claude-opus-5-5", "effort": "high"}}
    runs = _fake_cli(monkeypatch, result="42")
    api = _fake_api(monkeypatch, "should not be used")
    assert ai.complete(cfg, system="SYS", user="USR", max_tokens=100) == "42"
    argv, kw = runs[0]
    assert argv[0].endswith("/claude") and argv[1:3] == ["-p", "USR"]
    assert "--system-prompt" in argv and argv[argv.index("--system-prompt") + 1] == "SYS"
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == "" and "--max-turns" in argv
    assert "ANTHROPIC_API_KEY" not in kw["env"]                     # never bills the API by accident
    assert api == []
    assert ai.model_label(cfg) == "cli:claude-opus-5-5"


def test_cli_failure_falls_back_to_the_api_key(cfg, monkeypatch, capsys):
    cfg = {**cfg, "ai": {**cfg["ai"], "backend": "cli"}}
    _fake_cli(monkeypatch, boom=True)
    monkeypatch.setattr(ai, "load_key", lambda c: "k")
    api = _fake_api(monkeypatch, "from api")
    assert ai.complete(cfg, system="S", user="U", max_tokens=50) == "from api"
    assert api and api[0]["max_tokens"] == 50 and api[0]["system"] == "S"
    assert "claude CLI failed" in capsys.readouterr().err                # loud, not silent


def test_cli_error_envelope_and_no_key_raises(cfg, monkeypatch):
    cfg = {**cfg, "ai": {**cfg["ai"], "backend": "cli"}}
    _fake_cli(monkeypatch, result="quota", is_error=True)
    monkeypatch.setattr(ai, "load_key", lambda c: None)
    try:
        ai.complete(cfg, system="S", user="U")
    except RuntimeError as e:
        assert "quota" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_api_backend_is_unchanged_and_haiku_gets_no_thinking(cfg, monkeypatch):
    cfg = {**cfg, "ai": {**cfg["ai"], "backend": "api", "model": "claude-opus-4-8", "effort": "high"}}
    monkeypatch.setattr(ai, "load_key", lambda c: "k")
    api = _fake_api(monkeypatch, "ok")
    ai.complete(cfg, system="S", user="U", max_tokens=10)
    assert api[-1]["thinking"] == {"type": "adaptive"} and api[-1]["output_config"] == {"effort": "high"}
    ai.complete(cfg, system="S", user="U", max_tokens=10, model="claude-haiku-4-5")
    assert "thinking" not in api[-1] and "output_config" not in api[-1]
    assert ai.model_label(cfg) == "claude-opus-4-8"


def test_available_with_cli_but_no_key(cfg, monkeypatch):
    cfg = {**cfg, "ai": {**cfg["ai"], "backend": "cli", "enabled": True}}
    monkeypatch.setattr(ai, "load_key", lambda c: None)
    monkeypatch.setattr(ai, "cli_available", lambda: True)
    assert ai.available(cfg) is True
    monkeypatch.setattr(ai, "cli_available", lambda: False)
    assert ai.available(cfg) is False
