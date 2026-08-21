"""Conservative fallback for boards without a verified dedicated adapter.

It reads only unambiguous standard fields (name / email / phone / résumé). If the form has
any custom or required question it can't confidently classify, it quarantines the job for
the human queue instead of guessing. This is deliberately cautious: an un-verified board
must never silently submit (AUTOAPPLY_DESIGN.md §三/§十).
"""
from __future__ import annotations

from playwright.sync_api import Page

from ..models import Question, Quarantine


def matches(url: str) -> bool:
    return False                       # only ever used as the explicit fallback


def read_questions(page: Page) -> list[Question]:
    raise Quarantine("no verified adapter for this board yet — left for the human queue")


def fill(page: Page, answers, resume_path: str) -> None:
    raise Quarantine("no verified adapter for this board yet")


def submit(page: Page, *, dry_run: bool):
    raise Quarantine("no verified adapter for this board yet")
