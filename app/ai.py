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
from . import trust

# Questions the model must never answer — mirrors extension/content.js EXCLUDE, plus
# export-control. These may be factual, but they are sensitive/legal/user-choice fields and the
# owner must confirm them on the live form.
_NEVER = re.compile(
    r"gender|ethnic|hispanic|latin|\brace\b|racial|veteran|disab|pronoun|"
    r"sexual orientation|lgbt|demographic|self.?identif|equal employment|\beeo\b|"
    r"salary|compensation|desired pay|referr|referral|source of|how did you hear|"
    r"sponsor|visa|authoriz|work permit|right to work|eligible to work|"
    r"felony|conviction|criminal|background check|signature|sign your name|"
    r"certif|attest|acknowledg|consent|privacy|terms|gdpr",
    re.I,
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
- For a "why this role/company" style answer, tell it like a person: first name, in plain words, \
what THEY build or the problem THEY have (what you saw in the post), THEN connect one concrete \
thing you actually did. Never compress it into an abstract equation like "X is a Y and that maps \
what I do" — no human talks that way. Keep sentences short enough to read aloud without stumbling.
- Reference real companies/projects (GMV Max, the AI-agent onboarding platform, the \
MySQL/Cassandra product backend) where relevant, with the real numbers.
- Be concise and confident; no apologies, no "I hope to", no hedging.
- SHORT SENTENCES — this is the point, NOT a short answer. One idea per sentence, the way you'd \
say it out loud. Break up every long compound/complex sentence: a line that needs a comma-plus- \
"and" or three stacked clauses reads like a machine wrote it. Say as much as the question needs, \
but never in long winding sentences. Use contractions and plain words.
- For a SHORT factual field (current job title, current/previous employer, most recent school, \
degree, years of experience, current location/company), answer with JUST the value — e.g. \
"Software Engineer", "TikTok", "Illinois Institute of Technology" — not a full sentence.
- Every REQUIRED question needs a real answer — do NOT return empty just because the résumé \
doesn't spell it out. If it asks about your familiarity or experience with a common tool, \
process, or system that this role or company uses (e.g. "describe your interaction with the \
Workday system", "how have you used Jira?"), first work out what that thing is and how a software \
engineer at a big company actually touches it, then answer from your real day-to-day at TikTok and \
Meta — honest and a little general is fine. Don't invent specific projects, numbers, dates, or \
credentials you don't have; stay truthful and general rather than leaving it blank.
- ONLY return an empty string for a legal / eligibility / demographic question (work \
authorization, sponsorship, veteran / disability / race / gender, salary, criminal history, \
U.S.-person status) — those are the candidate's to answer herself.
Return ONLY a JSON object mapping each question's number (as a string) to the answer string.
""" + trust.RULE


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
    # Personal facts + the owner's OWN stated answers. Without these the model would answer a
    # demographic question from profile.ethnicity ("Asian") even though the owner's eeo config
    # says decline — so the preference has to travel with the fact.
    prof = cfg.get("profile") or {}
    if prof.get("legal_first_name"):
        parts.append(f"Legal name: {prof['legal_first_name']} {prof.get('last_name', '')}".strip())
    if prof.get("preferred_name"):
        parts.append(f"Preferred name: {prof['preferred_name']} {prof.get('preferred_last_name', '')}".strip())
    for k, label in (("location", "Location"), ("country", "Country"),
                     ("work_authorization", "Work authorization")):
        if prof.get(k):
            parts.append(f"{label}: {prof[k]}")
    eeo = cfg.get("eeo") or {}
    if eeo:
        parts.append("Voluntary self-identification — answer EXACTLY these, they are the "
                     "candidate's own chosen responses (a decline is a deliberate choice, "
                     "never substitute a real value for it):")
        for k, v in eeo.items():
            parts.append(f"  {k.replace('_', ' ')}: {v}")
    parts.append("No criminal convictions have been recorded for this candidate; if a question "
                 "asks about criminal history or a background check and no fact covers it, "
                 "leave the answer empty rather than guessing.")
    for e in base.get("education", []):
        parts.append(f"Education: {e['degree']}, {e['school']} ({e['dates']})")
    parts.append("Skills: " + ", ".join(
        s for items in (base.get("skills_master") or {}).values() for s in items))
    for job in base.get("experience", []):
        parts.append(f"\n{job['title']} @ {job['company']} ({job['dates']}, {job['location']}):")
        parts.extend(f"  - {b}" for b in job.get("bullets_master", []))
    # If a tailored résumé exists for this exact role, ground the answers in it — not just the
    # summary but the re-aimed bullets: they are the EXACT claims the submitted PDF makes for
    # this role, and an answer that re-derives a different angle from the master list above
    # will contradict the résumé the reader has open in the other tab (2026-08-01, ✨ button).
    try:
        yp = tailoring.resume_yaml_path(cfg, role["company"], role["job_id"])
        if yp.exists():
            t = yaml.safe_load(yp.read_text(encoding="utf-8")) or {}
            if t.get("summary"):
                parts.append("\nTailored summary for this role: " + t["summary"].strip())
            tb = [(e.get("company", ""), b)
                  for e in (t.get("experience") or []) for b in (e.get("bullets") or [])]
            if tb:
                parts.append("Claims the submitted résumé makes for THIS role — answers must "
                             "stay consistent with these (same projects, same numbers, no new "
                             "specifics):")
                parts.extend(f"  - [{co}] {b}" for co, b in tb[:14])
            if t.get("angle"):
                parts.append(f"Positioning angle of that résumé: {str(t['angle']).strip()}")
    except Exception:
        pass    # grounding is best-effort — a malformed YAML must not break form filling
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
    facts = _facts(cfg, role)
    numbered = "\n".join(f"{i}. {q.strip()}" for i, q in todo)
    base_user = (
        f"Candidate facts:\n{facts}\n\n"
        f"Role: {role['company']} — {role['title']}\n"
        f"{trust.fence('job description', role.get('jd_text'), 6000)}\n\n"
        f"Answer each question below in the first person, under {max_words} words each. "
        f"Return a JSON object mapping the question number (string) to the answer.\n\n"
        f"Questions:\n{numbered}"
    )
    import anthropic
    client = anthropic.Anthropic(api_key=key)

    def _gen(extra: str) -> dict[str, str]:
        resp = client.messages.create(
            model=ai.get("model", "claude-opus-4-8"),
            max_tokens=2500,
            thinking={"type": "adaptive"},
            output_config={"effort": ai.get("effort", "high")},
            system=_SYSTEM,
            messages=[{"role": "user", "content": base_user + extra}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        data = json.loads(_extract_json(text))
        out: dict[str, str] = {}
        for i, q in todo:
            a = data.get(str(i)) or data.get(i) or ""
            if isinstance(a, str) and a.strip():
                out[q] = a.strip()
        return out

    # generate → critique the NARRATIVE answers as a block → rewrite if they read like AI.
    # (Short factual one-liners are left out of the critique; the critic passes those anyway.)
    ctx = (f"Role: {role['company']} — {role['title']}. These are answers to open-ended "
           "application questions; judge the multi-sentence ones for human voice.")
    tries = max(1, int(ai.get("quality_max_tries", 3)))
    best, best_issues, extra = {}, 1 << 30, ""
    for _ in range(tries):
        out = _gen(extra)
        if not out:
            break
        block = "\n\n".join(f"Q: {q}\nA: {a}" for q, a in out.items()
                            if len(a.split()) >= 12)          # only judge real prose
        if not block:
            return out                                        # all short/factual — accept as-is
        verdict = _critique(cfg, block, facts, ctx)
        if len(verdict["issues"]) < best_issues:
            best, best_issues = out, len(verdict["issues"])
        if verdict["pass"]:
            return out
        extra = _revise_note(block, verdict)
    return best


def estimate_salary(cfg: dict, role: dict) -> int | None:
    """Market-rate BASE salary (USD/yr) to state as a desired salary for this role.

    Deliberately does NOT go through answer_questions(): that path refuses anything matching
    `_NEVER` (salary included) because the model must never answer a compensation question
    *to an employer* on the candidate's behalf. This is the opposite direction — researching a
    reference figure FOR the candidate, who still reviews it before submitting. Returns None
    when the model won't give a usable number.
    """
    key = load_key(cfg)
    if not key:
        return None
    ai = cfg.get("ai") or {}
    user = (
        f"Role: {role.get('title')} at {role.get('company')}\n"
        f"Location: {role.get('location') or 'United States'}\n\n"
        "What annual BASE salary in USD should a candidate state as their desired salary for "
        "this role? Use typical market rates (levels.fyi / Glassdoor style) for this title, "
        "company and location — and lean to the LOWER end of the band so the figure never "
        "screens the candidate out.\n"
        "Reply with ONLY an integer. No $, no commas, no words, no range."
    )
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=ai.get("model", "claude-opus-4-8"),
        max_tokens=600,
        system="You are a compensation research assistant. Answer with a single integer only.",
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\b(\d{5,6})\b", text.replace(",", ""))
    if not m:
        return None
    n = int(m.group(1))
    return n if 40_000 <= n <= 900_000 else None


# Voice rules lifted from the cover-letter skill (~/.claude/skills/cover-letter/SKILL.md) so
# the extension writes in the same register as the hand-written ones — persona-first, specific,
# and above all NOT AI-sounding. The banned list is the skill's, verbatim.
_LETTER_SYSTEM = """You write a short application paragraph (or a "why this role" note) as the
candidate, in FIRST PERSON. It must read like a real person talking — someone who read the job
post and is telling a friend why it fits — never like AI or a cover-letter template.

HOW A HUMAN ACTUALLY TELLS THIS (follow this shape):
1. Open by saying, in plain words, what THEY build or the problem THEY have — the way you'd
   describe the job to a friend. Start from what you SAW in the post. e.g. "You're building the
   engine that keeps long agent sessions from falling over." Not a definition, not "X is a Y".
2. THEN connect to ONE concrete thing YOU did, told as a small story: what you built, where, and
   one real number. "That's close to what I do at TikTok — I own the state logic behind our
   multi-step advertiser flows, and it cut invalid states by 30%."
3. One or two more sentences at most: a second real overlap, or a genuine specific reason this
   company pulls you. Then stop.

THE #1 THING TO AVOID — abstract "mapping" sentences that compress two ideas into an equation.
   BAD:  "The agent orchestration engine you are building is a state machine for long-running
          async sessions, and that maps what I do now."
   WHY:  no human talks like that. It stacks abstract nouns and states an equivalence instead of
          telling what happened.
   GOOD: "I read that you're building an orchestration engine for long agent sessions. I've
          basically been doing that at TikTok — I built the state-machine logic behind our
          multi-step async advertiser flows."
   Say what you SAW, then what you DID. Never "X is Y, and that maps Z".

Language rules (these are the heart):
- Hemingway grade <= 9. Short sentences, one idea each. Read it aloud — if you stumble or run out
  of breath, the sentence is too long or too abstract. Keep sentences under ~20 words.
- Use contractions (I'm, I've, you're, that's). Active voice. Own the work: "the service I built".
- No self-adjectives. Show it through the work, never "I am a passionate, results-driven engineer".
- ONE number per claim, not a wall of metrics. Name real projects (GMV Max, the AI-agent
  onboarding platform, the MySQL/Cassandra product backend) with the real numbers from the facts.
- BANNED, do not use: "I am excited/thrilled to apply", "I am confident that", "I am passionate
  about", "proven track record", "results-driven", "leverage", "spearheaded", "furthermore",
  "moreover", "in today's fast-paced world", "not only... but also", "I believe my skills align",
  "maps to", "aligns with", tricolons, em-dash pile-ups. If a sentence could open ANY applicant's
  letter, rewrite it.
- Also banned, the current crop of AI tells: "delve", "underscore", "pivotal", "robust",
  "seamless", "testament to", "landscape", "navigate the complexities". Exception: if one of
  these is a proper noun lifted from the job description (a team or research area actually
  called "Adversarial Robustness", say), quoting it is fine — the ban is on filler, not names.
- No "Bold term: explanation" list formatting — that shape is the single most recognisable AI
  pattern. Write flowing prose. No signposting ("Let's explore", "It's worth noting").
- NEVER invent a fact. Every claim must trace to the candidate facts given. If something would
  help but isn't in the facts, leave it out — do not fabricate.

Output the paragraph text ONLY. No greeting, no sign-off, no subject line, no markdown.
""" + trust.RULE


# A second-pass editor. The generator above is good but not reliable — some drafts still come out
# in abstract "X maps to Y" machine-voice. This critic judges a draft and, when it fails, hands
# back concrete rewrite notes so the next generation is targeted, not a random re-roll. Owner
# asked for exactly this: keep rewriting until it passes a human-voice check.
_CRITIC_SYSTEM = """You are a blunt writing editor. You are given a short first-person job-
application paragraph (or answer) plus the candidate's real facts. Decide two things: does it
sound like a REAL PERSON talking (not AI / not a corporate template), and is every claim grounded
in the facts?

FAIL the text if ANY of these are true:
- A sentence states an abstract equivalence or "mapping" instead of telling what happened — e.g.
  "X is a Y, and that maps what I do", "my experience aligns with your needs". This is the single
  biggest machine-voice tell. Fail it.
- It uses long, complex, or multi-clause sentences — the #1 machine tell. Real people speak in
  short sentences, one idea each. Fail any sentence that stacks clauses, chains "and/which/so", or
  would make you take a breath in the middle. (Total answer length is NOT the issue — the winding
  SENTENCES are; a long answer made of short sentences is fine.)
- Any sentence runs over ~22 words, or stacks abstract nouns so you'd stumble reading it aloud.
- It uses banned filler / AI tells: leverage, spearheaded, passionate, proven track record,
  excited/thrilled to apply, confident that, delve, underscore, pivotal, robust, seamless,
  testament to, maps to, aligns with, in today's fast-paced.
- It opens with a template line that could start any applicant's letter.
- It makes a claim (employer, project, number, skill) NOT present in the candidate facts.
- It has no concrete named project + real number from the facts, where the format allows one.
- It reads like a press release, not like a person telling a friend about the job.

PASS only if it opens from a concrete observation about the role/company, then connects to a real
project the candidate did (named, with a number where natural), in plain spoken sentences.

For a SHORT factual answer (a job title, a school name, a one-word yes/no) the human-voice tests
don't apply — pass it as long as it's grounded and not padded with filler.

Return ONLY a JSON object:
{"pass": true or false, "issues": ["short specific problem", ...],
 "fix": "one or two sentences of concrete direction for the rewrite — what to cut, how to reopen"}"""


def _critique(cfg: dict, text: str, facts: str, context: str) -> dict:
    """Judge whether `text` reads like a real person and stays grounded. Returns
    {"pass": bool, "issues": [str], "fix": str}. On any error, passes (never blocks output)."""
    key = load_key(cfg)
    if not key or not (text or "").strip():
        return {"pass": True, "issues": [], "fix": ""}
    ai = cfg.get("ai") or {}
    user = (f"Candidate facts:\n{facts}\n\n{context}\n\n"
            f'Text to judge:\n"""\n{text}\n"""\n\nJudge it and return the JSON verdict.')
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    try:
        resp = client.messages.create(
            model=ai.get("model", "claude-opus-4-8"),
            max_tokens=900,
            output_config={"effort": ai.get("effort", "high")},
            system=_CRITIC_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        t = "".join(b.text for b in resp.content if b.type == "text")
        d = json.loads(_extract_json(t))
    except Exception:
        return {"pass": True, "issues": [], "fix": ""}
    return {"pass": bool(d.get("pass", True)),
            "issues": [str(x) for x in (d.get("issues") or [])],
            "fix": str(d.get("fix") or "").strip()}


def _revise_note(prev: str, verdict: dict) -> str:
    """Turn a failed critique into a rewrite instruction appended to the next generation."""
    note = ("\n\nA reviewer rejected the previous draft for sounding like AI / not like a real "
            'person. Previous draft:\n"""\n' + prev + '\n"""\nProblems found: '
            + "; ".join(verdict["issues"] or ["reads like machine phrasing"]))
    if verdict.get("fix"):
        note += "\nRewrite direction: " + verdict["fix"]
    note += ("\nWrite a NEW version that fixes these. Keep the same facts and length limit. "
             "Open from what you SAW in the post, then tell one concrete thing you DID.")
    return note


def cover_letter(cfg: dict, role: dict, max_words: int = 180) -> str | None:
    """A short 'anything else you'd like us to know' / cover-letter paragraph for ONE role.

    Generate → critique → rewrite loop: a second LLM pass checks the draft reads like a real
    person (owner's ask — the old single-shot drafts came out machine-voiced). On a fail it
    rewrites with the critic's notes, up to `ai.quality_max_tries`. Returns the passing draft, or
    the least-flawed attempt if none passes. None if the model gives nothing at all."""
    key = load_key(cfg)
    if not key:
        return None
    ai = cfg.get("ai") or {}
    facts = _facts(cfg, role)
    base_user = (
        f"Candidate facts (the ONLY claims you may make):\n{facts}\n\n"
        f"Company: {role.get('company')}\nRole: {role.get('title')}\n"
        f"{trust.fence('job description', role.get('jd_text'), 6000)}\n\n"
        f"Write the paragraph in under {max_words} words."
    )
    import anthropic
    client = anthropic.Anthropic(api_key=key)

    def _gen(extra: str) -> str:
        resp = client.messages.create(
            model=ai.get("model", "claude-opus-4-8"),
            max_tokens=1500,
            thinking={"type": "adaptive"},
            output_config={"effort": ai.get("effort", "high")},
            system=_LETTER_SYSTEM,
            messages=[{"role": "user", "content": base_user + extra}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    ctx = f"Company: {role.get('company')} — Role: {role.get('title')} (a short cover-letter note)."
    tries = max(1, int(ai.get("quality_max_tries", 3)))
    best, best_issues, extra = None, 1 << 30, ""
    for _ in range(tries):
        text = _gen(extra)
        if not text:
            break
        verdict = _critique(cfg, text, facts, ctx)
        if len(verdict["issues"]) < best_issues:
            best, best_issues = text, len(verdict["issues"])
        if verdict["pass"]:
            return text
        extra = _revise_note(text, verdict)
    return best


def pick_option(cfg: dict, role: dict, question: str, options: list[str]) -> int:
    """Choose the best option INDEX for a non-sensitive dropdown whose choices are full
    sentences, grounded in the candidate facts. Returns -1 when none fit or the question is one
    she should answer herself."""
    if _NEVER.search(question or "") or any(_NEVER.search(o or "") for o in options or []):
        return -1
    key = load_key(cfg)
    if not key or not options:
        return -1
    ai = cfg.get("ai") or {}
    numbered = "\n".join(f"{i}. {o}" for i, o in enumerate(options))
    user = (
        f"Candidate facts:\n{_facts(cfg, role)}\n\n"
        f"Application question: {question}\n\nOptions:\n{numbered}\n\n"
        "Pick the ONE option that is correct for this candidate given her facts. Do not answer "
        "work authorization, sponsorship, demographic/self-ID, salary, consent, attestation, "
        "criminal-history, referral-source, or signature questions; answer -1 for those. If "
        "it's a relocation/on-site question and she is in San Jose CA, prefer 'willing to "
        "relocate'. If no option fits, or it's something only she can decide, answer -1.\n"
        "Reply with ONLY the option number (an integer), nothing else."
    )
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=ai.get("model", "claude-opus-4-8"),
        max_tokens=400,
        system="You map a candidate's facts to the single correct dropdown option. Answer with one integer.",
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"-?\d+", text)
    if not m:
        return -1
    idx = int(m.group(0))
    return idx if 0 <= idx < len(options) else -1
