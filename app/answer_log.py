"""Persist every AI autofill output to data/logs/answers.jsonl (gitignored).

Why: until 2026-08-20 the generated answers lived only in the extension panel's DOM and died
with the page — there was no way to audit quality after the fact ("it once answered 'I can't
call that API'" was unrecoverable). Every generator in app/ai.py now appends one JSON record
per output here, and tools/answer_eval.py judges the day's records each night.

This module must NEVER raise into the form-filling path — every write is best-effort.
"""
from __future__ import annotations

import json
import time

from . import config as cfgmod

LOG_REL = "data/logs/answers.jsonl"


def log(kind: str, role: dict | None, **fields) -> None:
    """Append one record. kind: answer | cover_letter | pick_option | salary | unanswered.
    `fields` carries the payload (question/answer/…); role supplies job identity."""
    try:
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,
            "job_id": (role or {}).get("job_id"),
            "company": (role or {}).get("company"),
            "title": (role or {}).get("title"),
        }
        rec.update(fields)
        p = cfgmod.resolve_path(LOG_REL)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # logging must never break form filling
