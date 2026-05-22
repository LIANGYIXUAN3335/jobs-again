"""Config + company-notes loading. All relative paths resolve against the repo root."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path = "config.yaml") -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


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
