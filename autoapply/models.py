"""Shared datatypes for the unattended nightly apply module.

Design: AUTOAPPLY_DESIGN.md. The flow is read-questions → resolve-answers → fill →
pre-submit gate → submit → verify. Every stage passes these types; every decision
records which layer produced it so the morning report can show provenance per field.
"""
from __future__ import annotations

import dataclasses
import enum
from typing import Any


class QType(str, enum.Enum):
    TEXT = "text"                 # single-line free text
    TEXTAREA = "textarea"         # multi-line free text
    SELECT = "select"             # native <select> or custom dropdown with options
    MULTISELECT = "multiselect"
    RADIO = "radio"
    CHECKBOX = "checkbox"         # single consent-style checkbox
    FILE = "file"                 # résumé / cover-letter upload
    DATE = "date"
    NUMBER = "number"
    UNKNOWN = "unknown"


class Source(str, enum.Enum):
    PROFILE = "profile"           # layer 1: config.yaml profile / education facts
    POLICY = "policy"             # layer 2: deterministic sensitive-topic policy table
    LLM = "llm"                   # layer 3: ai.answer_questions / pick_option (sonnet)
    SKIP = "skip"                 # optional field deliberately left blank


class Verdict(str, enum.Enum):
    SUBMITTED = "submitted"
    QUARANTINED = "quarantined"   # filled what we could, did NOT submit; human follows up
    SKIPPED = "skipped"           # never opened / pre-flight rejected (budget, stale resume…)
    ERROR = "error"               # crash / timeout mid-flight; nothing submitted
    DRY_RUN = "dry_run"           # filled + validated + screenshotted, submit intentionally not clicked


@dataclasses.dataclass
class Question:
    label: str                    # human-visible label text, normalized whitespace
    qtype: QType
    required: bool = False
    options: list[str] = dataclasses.field(default_factory=list)
    selector: str = ""            # adapter-private locator hint to find the control again
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Answer:
    question: Question
    value: Any                    # str for text/select label; bool for checkbox; path for FILE
    source: Source
    note: str = ""                # provenance detail ("jd range low end", "eeo decline set"…)


class Quarantine(Exception):
    """Raised anywhere in the per-job flow to abort THIS job without submitting.

    The runner catches it, records the reason + evidence, marks the role's notes, and
    moves on. Never crashes the batch (2026-08-01 ingest silent-dead-batch lesson)."""

    def __init__(self, reason: str, question: str = ""):
        self.reason = reason
        self.question = question
        super().__init__(f"{reason}" + (f" — {question!r}" if question else ""))


@dataclasses.dataclass
class ApplyResult:
    job_id: str
    company: str
    title: str
    ats: str
    apply_url: str
    verdict: Verdict
    reason: str = ""              # quarantine/skip/error cause, human-readable
    evidence_dir: str = ""
    answers: list[Answer] = dataclasses.field(default_factory=list)
    confirmed: str = ""           # what proved the submit landed ("thank-you page", "email")
    started_at: str = ""
    finished_at: str = ""

    def brief(self) -> str:
        mark = {"submitted": "✅", "quarantined": "🔶", "skipped": "⏭️",
                "error": "❌", "dry_run": "🧪"}[self.verdict.value]
        tail = f" — {self.reason}" if self.reason else ""
        return f"{mark} {self.company} · {self.title} [{self.ats}]{tail}"
