"""Explicit shared application memory; never loads credentials or unrelated notes."""
from __future__ import annotations
from .agent_review import digest

IDENTITY_BINDINGS = {"sample-5fbfd918@example.com": {"first_name": "Casey", "last_name": "Example", "preferred_name": "Casey"}}

def validate_identity(profile: dict) -> None:
    expected = IDENTITY_BINDINGS.get(str(profile.get("email", "")).strip().lower())
    if expected and any(str(profile.get(k, "")).strip() != v for k, v in expected.items()):
        raise ValueError("sample-5fbfd918@example.com 必须使用 Casey Example；请修正投递名片。法定姓名字段仍使用法定姓名。")

def memory_pack(profile: dict, base: dict) -> dict:
    validate_identity(profile)
    pack = {
        "source": "JobScan selected profile + resume + user-confirmed rules (2026-11-03)",
        "identity_bindings": {k: v for k, v in IDENTITY_BINDINGS.items() if k == str(profile.get("email", "")).strip().lower()},
        "identity": {k: profile[k] for k in ("first_name", "last_name", "preferred_name", "legal_first_name", "email") if k in profile},
        "submission_policy": {
            "authorization": "standing" if str(profile.get("email", "")).strip().lower() == "sample-5fbfd918@example.com" else "per-task",
            "confirmed_by_user": "2026-11-03",
            "scope": "Casey / new / tailored resume ready / no account; only after independent review passes",
            "phase": "testing",
            "pause_on": ["login", "captcha", "unknown required facts", "binding agreement", "employer application limit", "ambiguous submission result"],
        },
        "rules": [
            "Use the selected email's ordinary/preferred name; legal-name fields use the legal name.",
            "Use only confirmed facts. Unknown required facts need user input; never invent them.",
            "A reviewer pass applies only to the exact current form and fact version.",
            "Only an explicit employer success message means submitted; a click or disabled button does not.",
            "Correct explicit validation errors and review again; never retry an ambiguous submission.",
            "Do not automatically accept contracts, solve CAPTCHA, evade application limits, or opt into optional recording/marketing.",
        ],
        "resume_projects": base.get("projects", []),
    }
    return {**pack, "version": digest(pack)}
