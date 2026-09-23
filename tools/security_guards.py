#!/usr/bin/env python3
"""Supply-chain and personal-data guards for jobscan.

    python tools/security_guards.py            # exit 0 = clean, 1 = findings
    python tools/security_guards.py --verbose

Idea borrowed from MadsLorentzen/ai-job-search `tools/security_guards.py`
(2026-08-19), rewritten for this repo's actual exposure.

The point is to make the dangerous changes LOUD, not impossible: a change that
genuinely needs one of these must update the allowlist in this file in the same
diff, so it shows up in review instead of being buried.

.gitignore already carries most of the personal-data rules (2026-08-10 audit).
These are the four things it cannot check by itself:

1. Ignore rules can be deleted, or silently re-included with a `!pattern`
   negation. Check they are all still there and none is negated.
2. .gitignore is a statement of intent; `git ls-files` is the truth. A file
   committed BEFORE its ignore rule existed stays tracked forever. Check what
   is actually in the index.
3. secrets.yaml / secrets/ must not be group- or world-readable.
4. requirements.txt must contain only the packages this tool actually needs —
   a new transitive-looking line is how a dependency gets smuggled in.
5. Claude Code permission files must not carry blanket Bash permissions or
   hooks (a hook fires automatically, with no prompt, so it is strictly more
   dangerous than a pre-approved permission).

Stdlib only.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── 1. ignore rules that must never disappear ──────────────────────────────
REQUIRED_IGNORE_RULES = [
    "secrets.yaml",
    "secrets/",
    ".env",
    "data/*.db",
    "/output/",
    "**/token*.json",
    "**/*credential*.json",
    "**/*client_secret*.json",
    "*.pem",
    ".backups/",
]

# ── 2. nothing matching these may be tracked by git ────────────────────────
FORBIDDEN_TRACKED = [
    (r"(^|/)secrets\.yaml$",            "the real secrets file"),
    (r"(^|/)\.env(\..*)?$",             "an env file"),
    (r"token.*\.json$",                 "an OAuth token"),
    (r"credential.*\.json$",            "a credential file"),
    (r"client_secret.*\.json$",         "a Google client secret"),
    (r"\.(pem|p12)$",                   "a private key"),
    (r"\.(db|sqlite3?)$",               "a database (contains scraped JDs + your notes)"),
    (r"^output/",                       "generated résumés / prompts"),
]
# Templates are tracked on purpose.
TRACKED_EXEMPT = {"secrets.example.yaml", "config.example.yaml",
                  "company_notes.example.yaml"}

# ── 3. files that must stay owner-only ─────────────────────────────────────
OWNER_ONLY = ["secrets.yaml", "secrets"]

# ── 4. the exact dependency set ────────────────────────────────────────────
ALLOWED_REQUIREMENTS = {"fastapi", "uvicorn", "jinja2", "httpx", "pyyaml", "pytest"}

# ── 5. permission entries that auto-approve too much ───────────────────────
BROAD_PERMISSION = re.compile(
    r"^Bash\(\s*\*|^Bash\((curl|wget|ssh|scp|nc|chmod|sudo|rm)\b"
    r"|^Bash\(pip\s+install|^Bash\(.*\bpip\s+install\s+\*"
    r"|^(WebFetch|Write|Edit)\(\s*\*", re.I)

errors: list[str] = []
warnings: list[str] = []


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def check_gitignore() -> None:
    path = ROOT / ".gitignore"
    if not path.exists():
        errors.append(".gitignore is missing entirely")
        return
    lines = [l.strip() for l in _read(path).splitlines()]
    present = set(lines)
    for rule in REQUIRED_IGNORE_RULES:
        if rule not in present:
            errors.append(f".gitignore lost the rule `{rule}`")
    for line in lines:
        if line.startswith("!"):
            errors.append(f".gitignore re-includes ignored files with `{line}`")


def check_tracked() -> None:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                             capture_output=True, text=True, timeout=30)
    except Exception as exc:                       # noqa: BLE001
        warnings.append(f"could not run `git ls-files` ({type(exc).__name__})")
        return
    if out.returncode != 0:
        warnings.append("`git ls-files` failed; tracked-file check skipped")
        return
    for f in out.stdout.splitlines():
        if Path(f).name in TRACKED_EXEMPT:
            continue
        for pattern, what in FORBIDDEN_TRACKED:
            if re.search(pattern, f):
                errors.append(f"git tracks {what}: {f}")
                break


def check_modes() -> None:
    for name in OWNER_ONLY:
        p = ROOT / name
        if not p.exists():
            continue
        if p.stat().st_mode & 0o077:
            want = "700" if p.is_dir() else "600"
            errors.append(f"{name} is group/world accessible "
                          f"(mode {p.stat().st_mode & 0o777:o}); run `chmod {want} {name}`")


def check_requirements() -> None:
    path = ROOT / "requirements.txt"
    if not path.exists():
        warnings.append("requirements.txt is missing")
        return
    for raw in _read(path).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        pkg = re.split(r"[<>=!\[;\s]", line, 1)[0].strip().lower()
        if pkg and pkg not in ALLOWED_REQUIREMENTS:
            errors.append(f"requirements.txt has an un-allowlisted package: `{pkg}` "
                          f"(add it to ALLOWED_REQUIREMENTS in this file if intended)")


def check_claude_settings() -> None:
    for path in sorted(ROOT.parent.parent.glob(".claude/settings*.json")):
        try:
            data = json.loads(_read(path))
        except Exception:                          # noqa: BLE001
            warnings.append(f"{path} is not valid JSON; skipped")
            continue
        if "hooks" in data:
            errors.append(f"{path.name} defines hooks — a hook runs automatically "
                          f"with no prompt; review it deliberately")
        for entry in (data.get("permissions") or {}).get("allow", []):
            if BROAD_PERMISSION.match(str(entry)):
                warnings.append(f"{path.name} auto-approves a broad command: {entry}")


def main() -> int:
    verbose = "--verbose" in sys.argv
    for fn in (check_gitignore, check_tracked, check_modes,
               check_requirements, check_claude_settings):
        fn()
        if verbose:
            print(f"  ran {fn.__name__}")

    for w in warnings:
        print(f"⚠️  {w}")
    for e in errors:
        print(f"❌ {e}")
    if not errors:
        print(f"✅ security guards clean"
              f"{f' ({len(warnings)} warning(s))' if warnings else ''}")
        return 0
    print(f"\n{len(errors)} finding(s).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
