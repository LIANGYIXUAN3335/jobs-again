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

from . import answer_log
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
- If a question asks the applicant to PERFORM an external action right now — call an API \
endpoint and paste the output, run code and report the result, complete a puzzle/challenge/\
assessment, record a video, attach or create a file — you cannot do that for her. Do NOT \
fabricate a result and NEVER write that you are unable to do it (that text would be submitted \
on the form). Return exactly the string "[NEEDS_HUMAN]" for that question so the field is \
left blank for the candidate to complete herself.
Return ONLY a JSON object mapping each question's number (as a string) to the answer string.
""" + trust.RULE


# Deterministic post-generation rejects (2026-08-20). Peer finding from sdh's 26-report defect
# ledger: a rule the model must obey but keeps ignoring ("never say you're an AI") is useless as
# a prompt soft-constraint — if it can be written as a no-model assertion, it should BE one.
# Any match → the answer is dropped and the field left blank for the candidate; a blank field is
# always recoverable, "As an AI language model…" on a submitted form is not. Quality concerns
# (voice, filler words, relevance) stay with the critic + lessons loop — this list is only for
# text that must never reach a form, regardless of how well it's written.
_REJECT = [
    ("ai_disclosure", re.compile(
        r"\bas an AI\b|\bas a language model\b|\bAI (?:language )?model\b|\bI am an AI\b", re.I)),
    # refusal of an ACTION the form asked for — verbs kept specific so a legitimate
    # "I'm unable to relocate before June" style answer never trips it
    ("refusal", re.compile(
        r"\bI(?:'m| am) (?:not able|unable) to (?:call|access|run|execute|make|perform|browse|"
        r"visit|open|complete|fetch|click|submit)\b|"
        r"\bI (?:can(?:'t|not)|don'?t have the ability to) (?:call|access|run|execute|make|"
        r"perform|browse|visit|open|complete|fetch|click|submit)\b|"
        r"\bI don'?t have access to\b", re.I)),
    ("placeholder", re.compile(r"\[[^\]]{0,40}\]|\{\{[^}]*\}\}|\bTODO\b|lorem ipsum", re.I)),
]


def _hard_reject(text: str) -> str | None:
    """Reason string if `text` must never be typed onto a form, else None."""
    for reason, rx in _REJECT:
        if rx.search(text or ""):
            return reason
    return None


def _lessons() -> str:
    """Rules distilled from past answer evals (tools/answer_eval.py writes them to
    data/answer_lessons.md, the owner can edit/delete lines freely). Appended to the system
    prompt so a recurring mistake stops recurring. Best-effort and capped, so a malformed or
    runaway file can never crowd out the base rules or break filling."""
    try:
        p = cfgmod.resolve_path("data/answer_lessons.md")
        t = p.read_text(encoding="utf-8").strip() if p.exists() else ""
        if not t:
            return ""
        return "\n\nLessons from human/automated review of past answers — follow these too:\n" + t[:2000]
    except Exception:
        return ""


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
    for q in questions:
        if q and q.strip() and _NEVER.search(q):
            answer_log.log("unanswered", role, question=q.strip(), reason="never_filter")
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
    needs_human: set[str] = set()   # questions the model flagged as "only a human can do this"
    rejected: dict[str, str] = {}   # answers killed by the deterministic validator, by reason

    def _gen(extra: str) -> dict[str, str]:
        resp = client.messages.create(
            model=ai.get("model", "claude-opus-4-8"),
            max_tokens=2500,
            thinking={"type": "adaptive"},
            output_config={"effort": ai.get("effort", "high")},
            system=_SYSTEM + _lessons(),
            messages=[{"role": "user", "content": base_user + extra}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        data = json.loads(_extract_json(text))
        out: dict[str, str] = {}
        for i, q in todo:
            a = data.get(str(i)) or data.get(i) or ""
            if not isinstance(a, str) or not a.strip():
                continue
            # "call this API and paste the output"-style questions: leave the field for the
            # candidate rather than submit "I can't do that" (which once went onto a real form).
            if "NEEDS_HUMAN" in a:
                needs_human.add(q)
                continue
            bad = _hard_reject(a)
            if bad:
                rejected[q] = bad
                continue
            out[q] = a.strip()
        return out

    def _finish(out: dict[str, str], tries_used: int, verdict: dict | None) -> dict[str, str]:
        """Single exit: persist every outcome (answered / needs-human / declined) then return."""
        for q, a in out.items():
            answer_log.log("answer", role, question=q.strip(), answer=a,
                           model=ai.get("model"), effort=ai.get("effort"), tries=tries_used,
                           critic_pass=None if verdict is None else bool(verdict.get("pass")),
                           critic_issues=None if verdict is None else verdict.get("issues"))
        for q in needs_human:
            answer_log.log("unanswered", role, question=q.strip(), reason="needs_human")
        for q, why in rejected.items():
            if q not in out and q not in needs_human:
                answer_log.log("unanswered", role, question=q.strip(),
                               reason=f"validator_{why}")
        for _, q in todo:
            if q not in out and q not in needs_human and q not in rejected:
                answer_log.log("unanswered", role, question=q.strip(), reason="model_declined")
        return out

    # generate → critique the NARRATIVE answers as a block → rewrite if they read like AI.
    # (Short factual one-liners are left out of the critique; the critic passes those anyway.)
    ctx = (f"Role: {role['company']} — {role['title']}. These are answers to open-ended "
           "application questions; judge the multi-sentence ones for human voice.")
    tries = max(1, int(ai.get("quality_max_tries", 3)))
    best, best_issues, best_verdict, extra = {}, 1 << 30, None, ""
    used = 0
    for _ in range(tries):
        used += 1
        out = _gen(extra)
        if not out:
            break
        block = "\n\n".join(f"Q: {q}\nA: {a}" for q, a in out.items()
                            if len(a.split()) >= 12)          # only judge real prose
        if not block:
            return _finish(out, used, None)                   # all short/factual — accept as-is
        verdict = _critique(cfg, block, facts, ctx)
        if len(verdict["issues"]) < best_issues:
            best, best_issues, best_verdict = out, len(verdict["issues"]), verdict
        if verdict["pass"]:
            return _finish(out, used, verdict)
        extra = _revise_note(block, verdict)
    return _finish(best, used, best_verdict)


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
    ok = 40_000 <= n <= 900_000
    answer_log.log("salary", role, answer=(n if ok else None), raw=n, model=ai.get("model"))
    return n if ok else None


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
            system=_LETTER_SYSTEM + _lessons(),
            messages=[{"role": "user", "content": base_user + extra}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def _finish(text: str | None, tries_used: int, verdict: dict | None) -> str | None:
        if text:
            answer_log.log("cover_letter", role, answer=text,
                           model=ai.get("model"), effort=ai.get("effort"), tries=tries_used,
                           critic_pass=None if verdict is None else bool(verdict.get("pass")),
                           critic_issues=None if verdict is None else verdict.get("issues"))
        return text

    ctx = f"Company: {role.get('company')} — Role: {role.get('title')} (a short cover-letter note)."
    tries = max(1, int(ai.get("quality_max_tries", 3)))
    best, best_issues, best_verdict, extra = None, 1 << 30, None, ""
    used = 0
    for _ in range(tries):
        used += 1
        text = _gen(extra)
        if not text:
            break
        bad = _hard_reject(text)
        if bad:
            # never let this text become `best` — a blank box beats an AI-disclosure on a form
            answer_log.log("unanswered", role, reason=f"validator_{bad}",
                           surface="cover_letter", tries=used)
            extra = (f"\n\nAn automated check rejected the previous draft ({bad}). Never "
                     "reveal AI authorship, never refuse a task, never leave bracketed "
                     "placeholders. Write a clean new version.")
            continue
        verdict = _critique(cfg, text, facts, ctx)
        if len(verdict["issues"]) < best_issues:
            best, best_issues, best_verdict = text, len(verdict["issues"]), verdict
        if verdict["pass"]:
            return _finish(text, used, verdict)
        extra = _revise_note(text, verdict)
    return _finish(best, used, best_verdict)


def pick_option(cfg: dict, role: dict, question: str, options: list[str]) -> int:
    """Choose the best option INDEX for a non-sensitive dropdown whose choices are full
    sentences, grounded in the candidate facts. Returns -1 when none fit or the question is one
    she should answer herself."""
    if _NEVER.search(question or "") or any(_NEVER.search(o or "") for o in options or []):
        answer_log.log("pick_option", role, question=(question or "").strip(),
                       index=-1, reason="never_filter")
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
    idx = int(m.group(0)) if m else -1
    if not (0 <= idx < len(options)):
        idx = -1
    answer_log.log("pick_option", role, question=(question or "").strip(), options=options,
                   index=idx, answer=(options[idx] if idx >= 0 else None),
                   model=ai.get("model"))
    return idx


# ─────────────────────────────────────────────────────────────────────────────
# 念稿练习 (recite) 的 AI 判官。2026-08-29 新增，与上面的申请自动填写完全独立：
# 输入是**她自己**的录音转写，输出是一张评分卡，永远不会被打到任何表单上。
#
# 三条与上面共享的方法论：
#   ① 能确定性算的别问模型 —— coverage 由代码算（判官只吐 missed_cues）、forbid 由正则判、
#      verbatim_ratio 由 4-gram 算、时长由 ffprobe 算。判官只判它唯一有优势的那四维。
#   ② 反空洞夸奖三层 —— prompt 硬规则 → _reject_review() 正则后置 → 5 分必须带 TRANSCRIPT
#      逐字引证否则代码降到 3。同 _hard_reject 的思路：模型反复不听的规则要写成无模型断言。
#   ③ 失败不阻断 —— 判官挂了返回一张空卡，take 照样入库、录音照样在盘上。
# ─────────────────────────────────────────────────────────────────────────────

_RECITE_SYSTEM = """You are a senior FAANG interviewer debriefing ONE spoken answer from a \
mock-interview drill. The candidate recorded herself answering from memory. You get the \
TRANSCRIPT of what she actually said, the cue points she was aiming at, and — for reference \
only — the TARGET SCRIPT she once wrote.

Return ONLY a JSON object:
{"scores": {"structure": 1-5, "concision": 1-5, "fluency": 1-5, "why_depth": 1-5},
 "missed_cues": ["<verbatim text of each cue point whose IDEA never reached the listener>"],
 "evidence": {"structure": "...", "concision": "...", "fluency": "...", "why_depth": "..."},
 "issues": ["<concrete problem>", ...],
 "fix_next": "<ONE executable instruction for the very next take>",
 "one_line": "这一段会让面试官写下 ___，因为 ___"}

SCORE ANCHORS — use these exact behaviours, do not invent your own scale.
 structure  1: after the whole answer a listener still doesn't know what story this was, or the
               conclusion never arrives. 3: the conclusion only shows up in the last 20 seconds,
               everything before it is background. 5: the landing point is in the first two
               sentences and everything after it is evidence for that point.
 concision  1: most sentences run past 25 words, stacked clauses, restarts mid-sentence.
               3: one or two clearly over-long sentences, the rest are listenable.
               5: one idea per sentence, longest sentence under 16 words, not a single restart.
 fluency    1: speaking rate is 25+ wpm outside the 130-160 band, or 4+ pauses over 2 seconds.
               3: rate at the edge of the band, ~3 long pauses, audibly rushing or dragging.
               5: 130-160 wpm, at most one pause over 2 seconds, even rhythm.
            This is the ONLY place in the whole system that watches her speaking rate. Do not
            widen this band and do not excuse a rushed take because "the other gates passed".
            Rushing is her documented failure mode.
 why_depth  1: reports only what she did, zero reasoning. 3: gives a reason but no trade-off
               (says "better" without saying what it cost). 5: reason + an explicit trade-off +
               she can name the option she ruled out.

COVERAGE — you do NOT score this dimension. You only output `missed_cues`.
  A cue point counts as COVERED if the IDEA reached the listener, in ANY wording.
  Different words are NOT a deduction — they are evidence she owns the idea. If her phrasing
  for a cue is BETTER than the target script's, say so in `evidence`.
  Reproducing the TARGET SCRIPT's wording earns NOTHING extra.
  A cue is MISSED only if a listener would not have received that idea at all.
  Copy the cue's text VERBATIM into `missed_cues` — the code matches on it.

DELIVERY NOTES — the cue list may be followed by delivery instructions (how to speak, not what
  to say). Judge those under `structure` and mention them in `evidence.structure`. They are NOT
  cue points and must never appear in `missed_cues`.

Never justify a score by comparing to the TARGET SCRIPT. The script is one way to say it, not
the standard. Judge the transcript as an interviewer who has never seen the script would.

`read_aloud` is NOT a flag you emit. Whether she was looking at the script is already known
deterministically. Do not infer it from the text.

BANNED — the review is worthless if it flatters:
- Never open with praise. No "great job", "nice work", "strong answer", "you did well",
  "overall this is solid". No emoji.
- `issues` must never be empty. If the answer is genuinely good, name the thing that would
  still stop it from being a Strong Hire.
- `fix_next` must be ONE thing she can do in the next 60 seconds, with a location in the answer
  ("at the 40-second mark, go straight into…"). Not "be more concise", not "practice more",
  not two instructions joined by "and also".
- Any dimension you score 5 MUST quote the exact words from the TRANSCRIPT that earned it in
  `evidence`. A 5 without a verbatim quote will be reduced to 3 by the caller.
- `one_line` must follow the shape "这一段会让面试官写下 ___，因为 ___" and the second half must
  contain a concrete noun or number from THIS take.

Write `issues`, `fix_next` and `one_line` in Chinese (her working language); keep quoted English
from the transcript in English.
"""

# 正则后置：模型反复不听的软约束写成无模型断言（同 _REJECT 的思路）。命中 → 这一维/这条
# 输出被判无效，代码兜底。
_PRAISE = re.compile(
    r"great job|nice work|well done|excellent|strong answer|solid answer|good job|"
    r"overall.{0,20}(good|solid|strong)|做得很好|很棒|非常好|表现不错|整体不错", re.I)
_VAGUE_FIX = re.compile(
    r"^(be |try |practice|keep |continue|more |少一点|多练|注意一下|再简洁|更简洁|更清晰)",
    re.I)


def _reject_review(d: dict) -> list[str]:
    """判官输出的确定性质检。返回被改写的项，调用方记进 judge_raw 以便事后看判官漂移。"""
    fixed = []
    if _PRAISE.search(json.dumps(d, ensure_ascii=False)):
        fixed.append("praise")
    if not (d.get("issues") or []):
        d["issues"] = ["判官没给出问题 —— 这条本身就是问题，人看一眼"]
        fixed.append("empty_issues")
    fx = str(d.get("fix_next") or "").strip()
    if not fx or _VAGUE_FIX.match(fx) or " and also " in fx.lower() or "并且" in fx:
        fixed.append("vague_fix")
    # 5 分必须带 TRANSCRIPT 逐字引证，否则降到 3。
    ev = d.get("evidence") or {}
    for dim, v in list((d.get("scores") or {}).items()):
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v >= 5 and not re.search(r"[\"“'‘][^\"”'’]{8,}[\"”'’]", str(ev.get(dim) or "")):
            d["scores"][dim] = 3
            fixed.append(f"unquoted_5:{dim}")
    return fixed


def review_take(cfg: dict, ctx: dict) -> dict:
    """给一次 take 打分。`ctx` 由 recite 层组装，见下面的取值。永不抛。

    返回 {"scores", "missed_cues", "evidence", "issues", "fix_next", "one_line",
          "raw", "fixed", "error"}。判官不可用时 scores 为空 dict —— 调用方仍会落库，
          卡片显示「评分没跑出来，录音已存 [重试评分]」。
    """
    blank = {"scores": {}, "missed_cues": [], "evidence": {}, "issues": [],
             "fix_next": "", "one_line": "", "raw": "", "fixed": [], "error": ""}
    if not available(cfg):
        blank["error"] = "ai disabled or no api key"
        return blank
    key = load_key(cfg)
    ai = cfg.get("ai") or {}

    cues = ctx.get("scored_cues") or []
    cue_block = "\n".join(f"  - {c}" for c in cues) or "  (no cue points)"
    notes = ctx.get("delivery_notes") or []
    note_block = "\n".join(f"  - {n}" for n in notes)

    parts = [
        f"Question as asked this time:\n  {ctx.get('question', '')}",
        f"\nCUE POINTS she was aiming at (the ONLY things `missed_cues` may contain):\n{cue_block}",
    ]
    if note_block:
        parts.append(f"\nDELIVERY NOTES (judge under structure, never in missed_cues):\n{note_block}")
    parts.append(f"\nTARGET SCRIPT (reference only — never the standard):\n\"\"\"\n"
                 f"{(ctx.get('script') or '')[:4000]}\n\"\"\"")
    parts.append(f"\nTRANSCRIPT of what she actually said:\n\"\"\"\n"
                 f"{(ctx.get('transcript') or '')[:6000]}\n\"\"\"")
    m = ctx.get("metrics") or {}
    parts.append(
        f"\nMeasured (deterministic, already computed — do not recompute):\n"
        f"  duration {m.get('duration', 0):.0f}s against a {ctx.get('target_sec', 0)}s target\n"
        f"  speaking rate {ctx.get('wpm', 0)} wpm · pauses over 2s: {m.get('pauses_over_2s', 0)}"
        f" · longest pause {m.get('longest_pause_sec', 0)}s")

    vt = ctx.get("variant_type") or "original"
    if vt != "original":
        parts.append(
            f"\nVARIANT — this take was recorded under a variant constraint:\n"
            f"  {ctx.get('variant_hint') or ctx.get('question') or ''}\n"
            f"  Time allowed: {ctx.get('variant_sec')}s (the original target was "
            f"{ctx.get('target_sec')}s).\n"
            "  Judge `structure` and coverage AGAINST THE VARIANT, not against the original "
            "question or the original target length. Answering the original question when a "
            "different one was asked is a structure failure, not a coverage success. Dropping "
            "whole beats to fit a shorter budget is the CORRECT strategy under a time "
            "constraint — say which beats she chose to drop, and whether that was the right "
            "call, but do not score it as a failure of concision.")

    why = (ctx.get("why_line") or "").strip()
    if why:
        parts.append(
            f"\nWHY LINE — before unlocking anything, she typed one line answering "
            f"\"{ctx.get('why_q') or 'why was this the right call'}\":\n  \"\"\"{why}\"\"\"\n"
            "  Score `why_depth` PRIMARILY on this line, plus any reasoning she volunteered in "
            "the TRANSCRIPT. Do not hunt for it inside the spoken answer.")

    # duration < 0.5 * target 的零代码补丁（MEM2-P0-2）：undertime 移出硬门之后唯一的真实
    # 漏洞是「点名而不支撑」—— 30 秒里把四个要点各说一个词，coverage 机械地满分。
    tgt = float(ctx.get("target_sec") or 0)
    dur = float(m.get("duration") or 0)
    if tgt and dur and dur < 0.5 * tgt:
        parts.append(
            f"\nThis take ran {dur:.0f}s against a {tgt:.0f}s target. Under half the target, a "
            "cue that was merely NAMED does not count as covered; the idea must have been "
            "SUPPORTED. Re-check missed_cues under that stricter reading.")

    # `pace: "deliberate"` 的条目，慢是设计意图不是缺陷：这几条是靠停顿制造重量的短答
    # （53 词 / 35 秒），是全样本里最好的澄清式回答。不告诉判官的话 fluency 的 130-160 带会
    # 把它们系统性地判成 2–3 分 —— 一个把最佳短答判死的锚点，和退出硬门的那个 wpm 是同一个
    # 错误。band 本身不放宽（它是全系统唯一的语速监控），只在这一类条目上说明设计意图。
    if (ctx.get("pace") or "") == "deliberate":
        parts.append(
            "\nPACE — this script is deliberately slow: it is a short answer that carries its "
            "weight through pauses, and the author fixed its target length by hand rather than "
            "deriving it from the word count. For `fluency`, judge whether the pauses land "
            "where the meaning breaks and whether the rhythm is even — do NOT deduct for a "
            "speaking rate below the 130-160 band on this item alone.")

    name = (ctx.get("speaker_name") or "").strip()
    if name:
        parts.append(f"\nIn this interview the interviewer called her {name}.")

    import anthropic
    client = anthropic.Anthropic(api_key=key)
    user = "\n".join(parts)

    def _ask(max_tokens: int, think: bool) -> str:
        kw = {"thinking": {"type": "adaptive"}} if think else {}
        resp = client.messages.create(
            model=ai.get("model", "claude-opus-4-8"),
            max_tokens=max_tokens,
            output_config={"effort": ai.get("effort", "high")},
            system=_RECITE_SYSTEM + _lessons(),
            messages=[{"role": "user", "content": user}],
            **kw,
        )
        return "".join(b.text for b in resp.content if b.type == "text")

    # max_tokens 要装下 thinking + JSON。2000 在长条目上被 thinking 吃光过，返回了零个 text
    # block —— 而那正好是判官"没给分但也没报错"的样子，最难查的一种失败。宽一点，并且
    # 拿不到文本时退一次「不 thinking」再试，别让一条稿因为长而永远评不出分。
    text = ""
    try:
        text = _ask(8000, True)
        if not text.strip():
            text = _ask(3000, False)
        d = json.loads(_extract_json(text))
    except Exception as e:
        blank["error"] = f"{type(e).__name__}: {e}"[:300]
        blank["raw"] = text[:2000]
        return blank
    if not (d.get("scores") or {}):
        blank["error"] = "判官没返回 scores"
        blank["raw"] = text[:2000]
        return blank
    d.setdefault("scores", {})
    d.setdefault("missed_cues", [])
    d.setdefault("evidence", {})
    d.setdefault("issues", [])
    fixed = _reject_review(d)
    return {"scores": {k: v for k, v in (d.get("scores") or {}).items()},
            "missed_cues": [str(x) for x in (d.get("missed_cues") or [])],
            "evidence": d.get("evidence") or {},
            "issues": [str(x) for x in (d.get("issues") or [])],
            "fix_next": str(d.get("fix_next") or "").strip(),
            "one_line": str(d.get("one_line") or "").strip(),
            "raw": text[:8000], "fixed": fixed, "error": ""}
