"""ATS-specific adapters.

Each adapter exposes: matches(url), read_questions(page), fill(page, answers, resume_path),
submit(page, dry_run) -> confirmation_text|None.

Tonight's build ships Greenhouse (the verified primary target, ~1000 ready roles). Ashby
and Lever are designed (their live DOM shapes are captured in AUTOAPPLY_DESIGN.md and the
probe notes) and are the next increment — until their adapters land and pass dry-runs, a
role on those boards is handled by the conservative `generic` fallback, which fills only
the unambiguous standard fields and quarantines anything with custom questions rather than
guessing. So no un-verified board ever silently submits.
"""
from __future__ import annotations

from . import greenhouse, generic

ALL = [greenhouse]


def for_url(url: str):
    for mod in ALL:
        if mod.matches(url):
            return mod
    return generic
