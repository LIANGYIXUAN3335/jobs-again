"""AI application-autofill — the ONE place jobscan calls an LLM, and only when opted in
(config `ai.enabled` + a key in gitignored secrets.yaml / $ANTHROPIC_API_KEY).

It drafts truthful, first-person answers to OPEN-ENDED application questions ("Why this
company?", "Describe a project…"), grounded ONLY in Casey's résumé + profile. It NEVER
answers EEO / legal / work-authorization / demographic questions — those are the user's to
answer, and the model is instructed and pre-filtered to skip them (defense in depth vs. the
extension's own EXCLUDE list).
"""
from __future__ import annotations

import json
import os
import re

import yaml

from . import config as cfgmod
from . import tailoring

# Questions the model must never answer — mirrors extension/content.js EXCLUDE, plus export-control.
_NEVER = re.compile(
    r"gender|ethnic|hispanic|latin|\brace\b|racial|veteran|disab|pronoun|sexual orientation|"
    r"lgbt|demographic|self.?identif|equal employment|\beeo\b|salary|compensation|desired pay|"
    r"\bsponsor|visa|authoriz|felony|conviction|background check|u\.?s\.? person|\bcitizen",
    re.IGNORECASE,
)

_SYSTEM = """You are drafting job-application answers in the FIRST PERSON as the candidate, \
Casey Example. Absolute rules:
- Ground every answer ONLY in the candidate facts provided. Never invent employers, titles, \
dates, metrics, skills, or claims not present in the facts.

Sound like a real person, not an AI or a press release. This matters:
- Write the way a sharp engineer actually talks about her work — plain, direct, a little \
personal. Use contractions (I'm, I've, it's, that's). Vary sentence length; don't make every \
sentence the same polished shape.
- BAN this corporate/AI filler: "leverage", "spearheaded", "passionate about", "align with", \
"excited to contribute", "I am confident that", "proven track record", "cutting-edge", \
"seamlessly", "robust", "in today's fast-paced", "furthermore", "moreover", "delve". If a \
sentence sounds like a cover-letter template, rewrite it plainer.
- Lead with the real thing you did or the real reason, not a throat-clearing preamble. It's \
fine to be specific and slightly informal ("At TikTok I…", "What pulls me toward this is…").
- Reference real companies/projects (GMV Max, the AI-agent onboarding platform, the \
MySQL/Cassandra product backend) where relevant, with the real numbers.
- Be concise and confident; no apologies, no "I hope to", no hedging.
- For a SHORT factual field (current job title, current/previous employer, most recent school, \
degree, years of experience, current location/company), answer with JUST the value — e.g. \
"Software Engineer", "TikTok", "Illinois Institute of Technology" — not a full sentence.
- If a question asks for anything you cannot truthfully answer from the facts, or is a \
legal/eligibility/demographic question (work authorization, sponsorship, veteran/disability/\
race/gender, salary, criminal history, U.S.-person status), return an empty string for it — \
do NOT guess. Those are the candidate's to answer herself.
Return ONLY a JSON object mapping each question's number (as a string) to the answer string."""


def load_key(cfg: dict) -> str | None:
    p = cfgmod.resolve_path("secrets.yaml")
    if p.exists():
        k = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("anthropic_api_key")
        if k:
            return k
    return os.environ.get("ANTHROPIC_API_KEY")


def available(cfg: dict) -> bool:
    return bool((cfg.get("ai") or {}).get("enabled")) and bool(load_key(cfg))


def _facts(cfg: dict, role: dict) -> str:
    base = tailoring.load_base(cfg)
    parts = [f"Name: {base['name']}"]
    for e in base.get("education", []):
        parts.append(f"Education: {e['degree']}, {e['school']} ({e['dates']})")
    parts.append("Skills: " + ", ".join(
        s for items in (base.get("skills_master") or {}).values() for s in items))
    for job in base.get("experience", []):
        parts.append(f"\n{job['title']} @ {job['company']} ({job['dates']}, {job['location']}):")
        parts.extend(f"  - {b}" for b in job.get("bullets_master", []))
    # If a tailored résumé exists for this exact role, its summary is good extra grounding.
    yp = tailoring.resume_yaml_path(cfg, role["company"], role["job_id"])
    if yp.exists():
        t = yaml.safe_load(yp.read_text(encoding="utf-8")) or {}
        if t.get("summary"):
            parts.append("\nTailored summary for this role: " + t["summary"].strip())
    return "\n".join(parts)


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
    a, b = text.find("{"), text.rfind("}")
    return text[a:b + 1] if a != -1 and b != -1 else "{}"


def answer_questions(cfg: dict, role: dict, questions: list[str]) -> dict[str, str]:
    """Draft answers for the open-ended `questions`. Returns {question: answer}; skips any
    question that matches the never-answer filter or that the model declines (empty answer)."""
    key = load_key(cfg)
    if not key:
        raise RuntimeError("no Anthropic API key (set secrets.yaml or ANTHROPIC_API_KEY)")
    todo = [(i, q) for i, q in enumerate(questions) if q and q.strip() and not _NEVER.search(q)]
    if not todo:
        return {}
    ai = cfg.get("ai") or {}
    max_words = int(ai.get("max_answer_words", 120))
    jd = (role.get("jd_text") or "")[:6000]
    numbered = "\n".join(f"{i}. {q.strip()}" for i, q in todo)
    user = (
        f"Candidate facts:\n{_facts(cfg, role)}\n\n"
        f"Role: {role['company']} — {role['title']}\nJob description:\n{jd}\n\n"
        f"Answer each question below in the first person, under {max_words} words each. "
        f"Return a JSON object mapping the question number (string) to the answer.\n\n"
        f"Questions:\n{numbered}"
    )
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=ai.get("model", "claude-opus-4-8"),
        max_tokens=2500,
        thinking={"type": "adaptive"},
        output_config={"effort": ai.get("effort", "high")},
        system=_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    data = json.loads(_extract_json(text))
    out: dict[str, str] = {}
    for i, q in todo:
        a = data.get(str(i)) or data.get(i) or ""
        if isinstance(a, str) and a.strip():
            out[q] = a.strip()
    return out
