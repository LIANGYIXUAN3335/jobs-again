"""Three-layer answer funnel: profile facts → policy table → LLM (sonnet) → quarantine.

Given the question list an adapter read off a form, produce an Answer for every one —
or raise Quarantine so the runner leaves the job unsubmitted. Layers:

1. PROFILE — identity fields from config.yaml `profile:` (regex classifier ported from
   extension content.js PATTERNS; ordering matters: pronunciation before preferred-name
   before first-name, legal name into legal/first/full-name fields).
2. POLICY — sensitive topics via autoapply/policy.py (config-backed, deterministic).
3. LLM — open questions batched through ai.answer_questions (grounded in résumé facts,
   trust-fenced JD, critique loop, answers.jsonl audit); dropdowns via ai.pick_option.
   The nightly module overrides ai.model per the owner's tiering rule (sonnet, not opus).

Required questions that no layer can answer → Quarantine. Optional ones → skipped.
"""
from __future__ import annotations

import copy
import re

from . import policy
from .models import Answer, Question, QType, Quarantine, Source

# Owner model-tiering rule: batch/content generation runs sonnet-tier, never opus/fable.
BATCH_MODEL = "claude-sonnet-5"

_P = [  # (regex, profile key or callable) — first match wins, ordering is load-bearing
    (r"pronounc", "name_pronunciation"),
    (r"preferred.{0,12}(first.?)?name|nickname|what should we call", "preferred_name"),
    (r"legal first.?name", "legal_first_name"),
    (r"first.?name|given name", "legal_first_name"),
    (r"last.?name|surname|family name", "last_name"),
    (r"^(full |legal )?name$|full legal name", "_full_name"),
    (r"e.?mail", "email"),
    (r"country code", "phone_country_code"),
    (r"phone|mobile|telephone", "phone"),
    (r"linked.?in", "linkedin"),
    (r"git.?hub", "github"),
    (r"website|portfolio|personal site", "_website"),
    (r"street address|address line|^address\b|^address$", "address"),
    (r"^city\b|current city", "_city"),
    (r"zip|postal", "_zip"),
    (r"^state\b|state of residence|province", "_state"),
    (r"country(?! code)", "country"),
    (r"location|where are you (located|based)|current(ly)? (residence|reside|based)", "location"),
    (r"current (company|employer)|company name", "_current_company"),
    (r"current (job )?title|^title$", "_current_title"),
    (r"school|university|college", "_school"),
    (r"degree", "_degree"),
    (r"(field|discipline|major) of study|^discipline$|^major$", "_discipline"),
]

_DERIVED = {
    "_full_name": lambda p: f"{p.get('legal_first_name') or p.get('first_name', '')} {p.get('last_name', '')}".strip(),
    "_website": lambda p: p.get("website") or p.get("github", ""),
    "_city": lambda p: "San Jose",
    "_state": lambda p: "California",
    "_zip": lambda p: "95112",
    "_current_company": lambda p: "TikTok",
    "_current_title": lambda p: "Software Engineer",
    "_school": lambda p: (p.get("education") or [{}])[0].get("school", ""),
    "_degree": lambda p: (p.get("education") or [{}])[0].get("degree", ""),
    "_discipline": lambda p: (p.get("education") or [{}])[0].get("discipline", ""),
}

_YOE = re.compile(r"(how many )?years of (professional |relevant |full.?time |software |industry )*"
                  r"(experience|engineering|development)", re.I)


def profile_value(q: Question, cfg: dict) -> str | None:
    """Layer 1: identity/profile lookup. None when the label isn't a profile field."""
    profile = cfg.get("profile") or {}
    label = policy.norm(q.label)
    if q.qtype == QType.FILE:
        return None                          # résumé handled by the adapter, not here
    for pat, key in _P:
        if re.search(pat, label):
            val = _DERIVED[key](profile) if key in _DERIVED else profile.get(key, "")
            return str(val) if val else None
    return None


def resolve_all(questions: list[Question], role: dict, cfg: dict) -> list[Answer]:
    """Answer every question via the funnel. Raises Quarantine when a required question
    survives all three layers unanswered, or when a policy topic is unanswerable."""
    out: list[Answer] = []
    llm_todo: list[Question] = []

    for q in questions:
        if q.qtype == QType.FILE:
            continue                         # adapters attach the résumé themselves
        v = profile_value(q, cfg)
        if v is not None:
            if q.options:                    # profile value must map onto a real option
                opt = policy.best_option(q.options, [v])
                if opt is None and q.required:
                    raise Quarantine(f"profile value {v!r} not among options", q.label)
                if opt is None:
                    out.append(Answer(q, "", Source.SKIP, "profile value not among options"))
                    continue
                v = opt
            out.append(Answer(q, v, Source.PROFILE, "profile"))
            continue

        pol = policy.resolve(q, role, cfg)   # may raise Quarantine — bubbles up by design
        if pol is not None:
            out.append(pol)
            continue

        if policy.SENSITIVE.search(q.label or ""):
            # Sensitive but neither profile nor policy could answer: never ask the LLM.
            if q.required:
                raise Quarantine("required sensitive question with no standing answer", q.label)
            out.append(Answer(q, "", Source.SKIP, "sensitive, optional → left blank"))
            continue

        if _YOE.search(q.label or "") and q.qtype in (QType.NUMBER, QType.TEXT):
            # Résumé-consistent total (roles span 2019→present per resume_base.yaml).
            out.append(Answer(q, "6", Source.POLICY, "total YOE consistent with résumé dates"))
            continue

        llm_todo.append(q)

    out.extend(_llm_layer(llm_todo, role, cfg))

    # Final gate: every required question must carry a non-empty answer.
    answered = {id(a.question): a for a in out}
    for q in questions:
        if q.qtype == QType.FILE or not q.required:
            continue
        a = answered.get(id(q))
        if a is None or a.source == Source.SKIP or a.value in ("", None):
            raise Quarantine("required question left unanswered after all layers", q.label)
    return out


def _llm_layer(todo: list[Question], role: dict, cfg: dict) -> list[Answer]:
    """Layer 3: batch the open questions through the existing grounded answerer."""
    if not todo:
        return []
    from app import ai, trust                      # late import: repo path set by caller

    if trust.suspicious(role.get("jd_text") or ""):
        raise Quarantine("JD flagged by injection detector — human review required")

    ncfg = copy.deepcopy(cfg)
    ncfg.setdefault("ai", {})["model"] = (cfg.get("autoapply") or {}).get("model", BATCH_MODEL)

    open_qs = [q for q in todo if not q.options]
    opt_qs = [q for q in todo if q.options]
    out: list[Answer] = []

    if open_qs:
        got = ai.answer_questions(ncfg, role, [q.label for q in open_qs])
        for q in open_qs:
            text = (got.get(q.label) or "").strip()
            if text == "[NEEDS_HUMAN]":
                raise Quarantine("question asks the candidate to perform a task", q.label)
            if not text:
                if q.required:
                    raise Quarantine("LLM declined a required open question", q.label)
                out.append(Answer(q, "", Source.SKIP, "LLM declined, optional"))
                continue
            out.append(Answer(q, text, Source.LLM, "ai.answer_questions"))

    for q in opt_qs:
        idx = ai.pick_option(ncfg, role, q.label, q.options)
        if idx is None or idx < 0 or idx >= len(q.options):
            if q.required:
                raise Quarantine("no grounded option choice for required dropdown", q.label)
            out.append(Answer(q, "", Source.SKIP, "no grounded option choice"))
            continue
        out.append(Answer(q, q.options[idx], Source.LLM, "ai.pick_option"))
    return out
