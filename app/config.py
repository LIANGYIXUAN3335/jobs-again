"""Config + company-notes loading. All relative paths resolve against the repo root."""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# Which config a process runs under. Every entry point calls load_config() with no argument,
# so a second WORKSPACE (a second candidate: own user_state.db, own résumé base, own profile,
# same scanned jobs.db) is just `JOBSCAN_CONFIG=config.<name>.yaml` in that process's
# environment — the launchd plist for its web server / tailoring job sets it. Owner 2026-09-23.
ENV_CONFIG = "JOBSCAN_CONFIG"


REPLACE_MARK = "__replace__"
# Set on a loaded config that came through `extends:` (value = the parent path). Lets code tell
# a DERIVED workspace from the owner's base config: secrets.yaml is one shared file whose
# `gmail_app_password` is the owner's, and mailcode._cfg must not pair it with another
# candidate's address just because mailcode.* was inherited dict-by-dict.
EXTENDS_KEY = "_extends"


def _strip_marks(v):
    if isinstance(v, dict):
        return {k: _strip_marks(x) for k, x in v.items() if k != REPLACE_MARK}
    return v


def _deep_merge(base: dict, over: dict) -> dict:
    """`over` wins; dicts merge recursively, lists and scalars are replaced whole.

    A child dict carrying `__replace__: true` REPLACES the parent's dict instead of merging into
    it. Needed for per-person maps that must not leak between workspaces: apply_profiles
    (the owner's company→identity map put HER name on the second candidate's Nvidia résumés,
    2026-09-23), cooldowns, blocked companies, mailcode extra accounts, the whole profile."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and v.get(REPLACE_MARK):
            out[k] = _strip_marks(v)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = _strip_marks(v)
    return out


def load_config(path: str | Path | None = None) -> dict:
    """Load a config file. `extends: <path>` inherits another config (chains allowed): the
    child's keys override the parent's, dict-by-dict, so a workspace config lists only what
    differs — its DB, dirs, profile, résumé base — and keeps the shared companies/scoring."""
    p = Path(path or os.environ.get(ENV_CONFIG) or "config.yaml")
    if not p.is_absolute():
        p = ROOT / p
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    parent = cfg.pop("extends", None)
    if parent:
        cfg = _deep_merge(load_config(parent), cfg)
        cfg[EXTENDS_KEY] = str(parent)
    return cfg


def workspace_name(cfg: dict) -> str:
    """Display name of the candidate this config serves ('' for the owner's default)."""
    return str((cfg.get("workspace") or {}).get("name") or "")


def resolve_path(rel: str | Path) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else ROOT / p


def db_path(cfg: dict) -> Path:
    return resolve_path(cfg["db_path"])


def user_db_path(cfg: dict) -> Path:
    return resolve_path(cfg["user_db_path"])


def norm_key(name: str) -> str:
    """Canonical form for company names: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def norm_title(title: str) -> str:
    """Canonical form for titles: lowercase, punctuation collapsed to spaces."""
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def active_cooldowns(cfg: dict, today: str | None = None) -> dict[str, dict]:
    """Companies under an application cooldown (some ATSes cap apps per window, e.g. Plaid on
    Ashby: 3 per 60 days). Returns {norm_key: {until, reason}} for cooldowns not yet expired.
    While active, the scanner skips the company and the UI hides its roles."""
    import datetime
    today = today or datetime.date.today().isoformat()
    out: dict[str, dict] = {}
    for name, cd in (cfg.get("cooldowns") or {}).items():
        until = str((cd or {}).get("until", ""))
        if until and until >= today:
            out[norm_key(name)] = {"until": until, "reason": (cd or {}).get("reason", "")}
    return out


def load_company_notes(cfg: dict) -> dict[str, dict]:
    p = resolve_path(cfg.get("company_notes", "company_notes.yaml"))
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {norm_key(k): (v or {}) for k, v in raw.items()}
