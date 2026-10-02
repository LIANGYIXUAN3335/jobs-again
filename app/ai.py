"""AI application-autofill — the ONE place jobscan calls an LLM, and only when opted in
(config `ai.enabled` + a key in gitignored secrets.yaml / $ANTHROPIC_API_KEY).

It drafts truthful, first-person answers to OPEN-ENDED application questions ("Why this
company?", "Describe a project…"), grounded ONLY in the candidate's résumé + profile. Legal /
eligibility / EEO questions ARE answered too — from the recorded facts only (owner rescope
2026-09-09; the owner reviews every form before submitting). The only machine-blank categories are
compensation and referral source (_NEVER); everything else is fact-grounded or left empty.
"""
from __future__ import annotations

import json
import os
import sys
import pathlib
import re

import yaml

from . import answer_log
from . import config as cfgmod
from . import tailoring
from . import trust

# Questions the model must never answer — mirrors extension/content.js EXCLUDE.
# 2026-09-09 owner rescoped this («我就是一个有 GC EAD 的普通人,这个是可以填的»): legal /
# eligibility / EEO / criminal / signature questions are now AUTO-FILLED from the recorded
# facts in _facts() — her status is simple and definite, and she reviews every form before
# submitting. The guard against harm is FACT-GROUNDING (never guess an unrecorded fact — see
# _SYSTEM), not category blanking. Only two categories stay machine-blank:
# compensation (the dedicated /api/salary flow owns it) and referral/"how did you hear"
# (situational facts nothing on file can answer).
_NEVER = re.compile(
    # `referr` used to be a bare substring — it also matched "preferred", so Robinhood's
    # "Which of the preferred qualifications…" was silently skipped (2026-09-23). Word-bounded now.
    r"salary|compensation|desired pay|\breferral\b|\breferred by\b|\bwho referred\b|source of|how did you hear",
    re.I,
)

# The one sentence for every work-authorization / sponsorship box (owner 2026-08-07 定稿:
# "不主动展开移民细节"). Overridden by config profile.work_authorization_answer; the extension
# carries the same default (content.js LEGAL_CANON) for its deterministic path.
_LEGAL_CANON = "I'm authorized to work in the US and I don't require sponsorship."

_SYSTEM = """You are drafting job-application answers in the FIRST PERSON as the candidate, \
__NAME__. Absolute rules:
- Ground every answer ONLY in the candidate facts provided. Never invent employers, titles, \
dates, metrics, skills, or claims not present in the facts.

Sound like a real person, not an AI or a press release. This matters:
- Write the way a sharp engineer actually talks about their work — plain, direct, a little \
personal. Use contractions (I'm, I've, it's, that's). Vary sentence length; don't make every \
sentence the same polished shape.
- BAN this corporate/AI filler: "leverage", "spearheaded", "passionate about", "align with", \
"excited to contribute", "I am confident that", "proven track record", "cutting-edge", \
"seamlessly", "robust", "in today's fast-paced", "furthermore", "moreover", "delve". If a \
sentence sounds like a cover-letter template, rewrite it plainer.
- INTENT FIRST (owner 2026-09-11): before writing each answer, silently work out WHY the \
employer asks this — what risk they're screening for, or what signal they hope to see. Then \
write the answer so it (a) directly answers the literal question in the first sentence, and \
(b) resolves the underlying concern with concrete evidence, not reassurance. Examples of the \
translation: "describe your remote experience" is really "will coordination and delivery \
suffer when you're not in the office?" — so state the arrangement, then show the mechanisms \
that keep you coordinated and shipping. "Any cool project for fun?" is really "do you have \
genuine drive beyond assigned work?". "Why this company?" is really "did you do homework, \
and will you stay?". Never answer the surface question while leaving the real worry untouched.
- Lead with the real thing you did or the real reason, not a throat-clearing preamble. It's \
fine to be specific and slightly informal ("At __EMPLOYER_1__ I…", "What pulls me toward this is…").
- For a "why this role/company" style answer, tell it like a person: first name, in plain words, \
what THEY build or the problem THEY have (what you saw in the post), THEN connect one concrete \
thing you actually did. Never compress it into an abstract equation like "X is a Y and that maps \
what I do" — no human talks that way. Keep sentences short enough to read aloud without stumbling.
- Reference the real companies/projects named in the candidate facts where relevant, with \
the real numbers.
- For "side project / personal project / built for fun / outside of work" questions: lead with \
ONE of the Personal side projects from the candidate facts — pick whichever fits this company \
best and tell it concretely (what it does, one or two real details), don't list them all. \
Only fall back to a work anecdote if no side project fits at all. Never invent side projects \
beyond that list.
- For "remote work experience" questions: answer the question actually asked — HER OWN \
experience of working away from the office — from the Remote work facts. Lead with the honest \
arrangement (hybrid, days per week from home), then what makes them productive on home days. \
Do NOT substitute distributed-team collaboration for personal remote experience, and never \
imply they have worked fully remote.
- Be concise and confident; no apologies, no "I hope to", no hedging.
- SHORT SENTENCES — this is the point, NOT a short answer. One idea per sentence, the way you'd \
say it out loud. Break up every long compound/complex sentence: a line that needs a comma-plus- \
"and" or three stacked clauses reads like a machine wrote it. Say as much as the question needs, \
but never in long winding sentences. Use contractions and plain words.
- For a SHORT factual field (current job title, current/previous employer, most recent school, \
degree, years of experience, current location/company), answer with JUST the value — e.g. \
"Software Engineer", "__EMPLOYER_1__", "__SCHOOL__" — not a full sentence.
- Every REQUIRED question needs a real answer — do NOT return empty just because the résumé \
doesn't spell it out. If it asks about your familiarity or experience with a common tool, \
process, or system that this role or company uses (e.g. "describe your interaction with the \
Workday system", "how have you used Jira?"), first work out what that thing is and how a software \
engineer at a big company actually touches it, then answer from your real day-to-day at __EMPLOYER_1__ and \
__EMPLOYER_2__ — honest and a little general is fine. Don't invent specific projects, numbers, dates, or \
credentials you don't have; stay truthful and general rather than leaving it blank.
- Legal / eligibility / demographic questions (work authorization, sponsorship, citizenship, \
export control, security clearance, veteran / disability / race / gender, criminal history, \
U.S.-person status) ARE answerable: the candidate facts record their definite status. \
Answer them exactly from those facts — the owner reviews every form before submitting. \
For a work-authorization or sponsorship question use the CANONICAL sentence given in the facts, \
word for word (prefixed with "Yes." / "No." when the question is yes/no). Never volunteer the \
visa type, EAD, I-485, green card or citizenship unless the question explicitly asks for the \
status or basis — then name only the Employment Authorization Document (EAD). \
Return an empty string ONLY for salary/compensation (a separate flow owns it) and for personal \
facts NOT on file — non-compete details, whether they previously applied or interviewed at this \
company, or anything else the facts above don't cover. NEVER guess those; an empty field the \
candidate fills in 5 seconds beats a wrong commitment under their name.
- NEVER volunteer a weakness, gap, or missing qualification — not in optional "anything else" \
boxes either. An answer sells real overlap. If an OPTIONAL field has nothing strong to say, \
return an empty string; no "happy to grow into the role", no "I should be straight about \
something".
- If a question asks the applicant to PERFORM an external action right now — call an API \
endpoint and paste the output, run code and report the result, complete a puzzle/challenge/\
assessment, record a video, attach or create a file — you cannot do that for them. Do NOT \
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


def _hard_reject(text: str, cfg: dict | None = None) -> str | None:
    """Reason string if `text` must never be typed onto a form, else None. Workspace-specific
    phrases come from config `ai.reject_phrases` (e.g. the owner's "never claim to hold an
    H-1B") — they are facts about ONE candidate, so they cannot live in code."""
    for reason, rx in _REJECT:
        if rx.search(text or ""):
            return reason
    for pat in ((cfg or {}).get("ai") or {}).get("reject_phrases") or []:
        try:
            if re.search(pat, text or "", re.I):
                return "config_phrase"
        except re.error:
            continue
    return None


def _personalize(text: str, cfg: dict) -> str:
    """Fill the __NAME__ / __EMPLOYER_n__ / __SCHOOL__ tokens in a prompt from the workspace's
    résumé base, so the same code serves any candidate (owner 2026-09-23)."""
    try:
        base = tailoring.load_base(cfg)
    except FileNotFoundError:
        base = {}
    jobs = [j.get("company", "") for j in base.get("experience", []) if j.get("company")]
    edu = base.get("education") or []
    return (text.replace("__NAME__", base.get("name") or "the candidate")
                .replace("__EMPLOYER_1__", jobs[0] if jobs else "my current employer")
                .replace("__EMPLOYER_2__", jobs[1] if len(jobs) > 1 else (jobs[0] if jobs else "my previous employer"))
                .replace("__SCHOOL__", (edu[0].get("school") if edu else "") or "my university"))


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


def _ready(cfg: dict) -> bool:
    """Some backend can answer: the CLI (when selected and installed) or the API key."""
    return (backend(cfg) == "cli" and cli_available()) or bool(load_key(cfg))


def backend(cfg: dict) -> str:
    """'cli' = the machine's logged-in Claude Code (Max subscription, no API bill), 'api' = the
    Anthropic key in secrets.yaml. Owner 2026-09-25: default cli, api is the fallback."""
    return str((cfg.get("ai") or {}).get("backend") or "api").lower()


def cli_available() -> bool:
    return pathlib.Path(_CLAUDE_BIN).exists()


def available(cfg: dict) -> bool:
    if not (cfg.get("ai") or {}).get("enabled"):
        return False
    if backend(cfg) == "cli" and cli_available():
        return True
    return bool(load_key(cfg))


def model_label(cfg: dict) -> str:
    """What to write in answers.jsonl: which backend AND model produced the text."""
    ai = cfg.get("ai") or {}
    if backend(cfg) == "cli" and cli_available():
        return "cli:" + str(ai.get("cli_model") or _CLI_MODEL)
    return str(ai.get("model") or "claude-opus-4-8")


_CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
_CLI_MODEL = "claude-opus-5-5"
_CLI_CWD = cfgmod.resolve_path("data/cli_cwd")      # an EMPTY dir: no CLAUDE.md, no repo context leaks into the answer


def _complete_cli(cfg: dict, system: str, user: str, *, effort: str | None, timeout: int) -> str:
    """One headless `claude -p` turn on the subscription. Tools off, one turn, own system prompt,
    JSON envelope so we get the bare answer text back. Raises on any failure (caller falls back)."""
    import subprocess
    ai = cfg.get("ai") or {}
    model = str(ai.get("cli_model") or _CLI_MODEL)
    _CLI_CWD.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}   # never bill the API by accident
    argv = [_CLAUDE_BIN, "-p", user, "--model", model, "--output-format", "json",
            "--system-prompt", system, "--tools", "", "--max-turns", "1", "--no-session-persistence"]
    if effort:
        argv += ["--effort", str(effort)]
    r = subprocess.run(argv, cwd=str(_CLI_CWD), env=env, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 and not r.stdout.strip():
        raise RuntimeError(f"claude -p exit {r.returncode}: {r.stderr.strip()[:300]}")
    try:
        d = json.loads(r.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise RuntimeError(f"claude -p returned no JSON envelope: {r.stdout[:200]!r}")
    if d.get("is_error"):
        raise RuntimeError(f"claude -p error: {str(d.get('result'))[:300]}")
    return str(d.get("result") or "")


def _complete_api(cfg: dict, system: str, user: str, *, max_tokens: int, model: str | None,
                  thinking: bool, effort: str | None) -> str:
    key = load_key(cfg)
    if not key:
        raise RuntimeError("no Anthropic API key (set secrets.yaml or ANTHROPIC_API_KEY)")
    ai = cfg.get("ai") or {}
    model = model or ai.get("model", "claude-opus-4-8")
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    kwargs = {"model": model, "max_tokens": max_tokens, "system": system,
              "messages": [{"role": "user", "content": user}]}
    if "haiku" not in model:                      # Haiku 4.5 has no adaptive thinking / effort
        if thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": effort or ai.get("effort", "high")}
    resp = client.messages.create(**kwargs)
    return "".join(b.text for b in resp.content if b.type == "text")


def complete(cfg: dict, *, system: str, user: str, max_tokens: int = 1500, model: str | None = None,
             thinking: bool = True, effort: str | None = None) -> str:
    """Every model call in JobScan goes through here. Backend per config ai.backend: `cli` runs
    the local Claude Code on the subscription and falls back to the API key when the CLI fails
    (not logged in, quota window, timeout) — loudly, one stderr line — so a form fill never
    silently dies; `api` is the original path, byte-for-byte."""
    ai = cfg.get("ai") or {}
    if backend(cfg) == "cli" and cli_available():
        try:
            return _complete_cli(cfg, system, user, effort=effort or ai.get("effort"),
                                 timeout=int(ai.get("cli_timeout_sec", 120)))
        except Exception as e:  # noqa: BLE001 — fall back, but say so
            print(f"ai: claude CLI failed ({type(e).__name__}: {str(e)[:160]}); "
                  f"{'falling back to the API key' if load_key(cfg) else 'no API key to fall back to'}", file=sys.stderr)
            if not load_key(cfg):
                raise
    return _complete_api(cfg, system, user, max_tokens=max_tokens, model=model, thinking=thinking, effort=effort)


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
                     ("work_authorization", "Work authorization"),
                     ("years_of_experience",
                      "Years of experience (canonical — use for any years question or bucket)"),
                     ("relocation", "Relocation / onsite willingness"),
                     ("notice_period",
                      "Notice period (an earliest-start-date question = about this long after an offer)"),
                     ("favorite_recent_ai",
                      "Favorite recent AI development (canonical talking points — phrase to fit the question)")):
        if prof.get(k):
            parts.append(f"{label}: {prof[k]}")
    eeo = cfg.get("eeo") or {}
    if eeo:
        parts.append("Voluntary self-identification — answer EXACTLY these, they are the "
                     "candidate's own chosen responses (a decline is a deliberate choice, "
                     "never substitute a real value for it):")
        for k, v in eeo.items():
            parts.append(f"  {k.replace('_', ' ')}: {v}")
    # Legal / eligibility persona (owner 2026-09-09): simple and fully recorded — answer these
    # confidently. Anything NOT listed here or in the profile is not a fact on file.
    canon = prof.get("work_authorization_answer") or _LEGAL_CANON
    legal = list(prof.get("legal_facts") or [])
    legal.append("Work authorization / sponsorship — CANONICAL ANSWER, use these exact words: "
                 f"\"{canon}\" (prefix Yes./No. for a yes/no question). Do NOT volunteer visa type or "
                 "immigration details unless the question explicitly asks for the status/basis — then "
                 f"answer \"{prof.get('visa_status_answer') or 'see the facts above'}\"")
    legal.append("Employment history: exactly the employers listed below — no other employment, "
                 "internship, vendor, contractor or temp relationship with any company")
    parts.append("Legal / eligibility facts (definite — answer such questions from these; anything "
                 "NOT listed here is not a fact on file):\n" + "\n".join(f"  - {x}" for x in legal))
    for k, label in (("ethnicity", "Race/ethnicity self-identification"),
                     ("veteran_status", "Veteran status"),
                     ("disability_status", "Disability status")):
        if prof.get(k):
            parts.append(f"{label}: {prof[k]}")
    for e in base.get("education", []):
        parts.append(f"Education: {e['degree']}, {e['school']} ({e['dates']})")
    # Remote-work facts (2026-09-11): without these, "describe your remote experience"
    # got answered with distributed-team collaboration — which reads as "never worked
    # remote". Owner-confirmed: hybrid at __EMPLOYER_1__ and Meta, never fully remote.
    rw = base.get("remote_work") or {}
    if rw:
        parts.append("\nRemote work experience (owner-confirmed, honest): " + rw.get("arrangement", ""))
        parts.extend(f"  - {f}" for f in rw.get("facts", []))
    # Personal side projects (2026-09-11): without these, questions explicitly about
    # side/personal projects could only reach for early-career work anecdotes. But they are
    # STRICTLY for that question shape (owner 2026-09-15, after "Share something cool" came
    # back as a side-project pitch): any general cool/proud/impressive-work question must be
    # answered from the employment experience below — the résumé is the baseline, and the
    # reader has it open in the other tab.
    if base.get("side_projects"):
        parts.append(
            "\nPersonal side projects — use ONLY when the question explicitly asks about a "
            "personal / side / hobby / outside-of-work project. For any general question "
            "(coolest thing you've built, proudest work, exceptional work, impressive "
            "project), answer from the employment experience below instead:")
    for p in base.get("side_projects", []):
        parts.append(f"\nPersonal side project: {p['name']} ({p.get('year','')}, {p.get('stack','')}):")
        parts.extend(f"  - {f}" for f in p.get("facts", []))
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
    if not _ready(cfg):
        raise RuntimeError("no model backend: log in the claude CLI (ai.backend: cli) or set an Anthropic API key")
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
    needs_human: set[str] = set()   # questions the model flagged as "only a human can do this"
    rejected: dict[str, str] = {}   # answers killed by the deterministic validator, by reason

    def _gen(extra: str) -> dict[str, str]:
        text = complete(cfg, system=_personalize(_SYSTEM, cfg) + _lessons(), user=base_user + extra, max_tokens=2500)
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
            bad = _hard_reject(a, cfg)
            if bad:
                rejected[q] = bad
                continue
            out[q] = a.strip()
        return out

    def _finish(out: dict[str, str], tries_used: int, verdict: dict | None) -> dict[str, str]:
        """Single exit: persist every outcome (answered / needs-human / declined) then return."""
        for q, a in out.items():
            answer_log.log("answer", role, question=q.strip(), answer=a,
                           model=model_label(cfg), effort=ai.get("effort"), tries=tries_used,
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
    if not _ready(cfg):
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
    text = complete(cfg, system="You are a compensation research assistant. Answer with a single integer only.",
                    user=user, max_tokens=600, thinking=False)
    m = re.search(r"\b(\d{5,6})\b", text.replace(",", ""))
    if not m:
        return None
    n = int(m.group(1))
    ok = 40_000 <= n <= 900_000
    answer_log.log("salary", role, answer=(n if ok else None), raw=n, model=model_label(cfg))
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
   one real number. "That's close to what I do at __EMPLOYER_1__ — I own the state logic behind our
   multi-step advertiser flows, and it cut invalid states by 30%."
3. One or two more sentences at most: a second real overlap, or a genuine specific reason this
   company pulls you. Then stop.

THE #1 THING TO AVOID — abstract "mapping" sentences that compress two ideas into an equation.
   BAD:  "The agent orchestration engine you are building is a state machine for long-running
          async sessions, and that maps what I do now."
   WHY:  no human talks like that. It stacks abstract nouns and states an equivalence instead of
          telling what happened.
   GOOD: "I read that you're building an orchestration engine for long agent sessions. I've
          basically been doing that at __EMPLOYER_1__ — I built the state-machine logic behind our
          multi-step async advertiser flows."
   Say what you SAW, then what you DID. Never "X is Y, and that maps Z".

Language rules (these are the heart):
- Hemingway grade <= 9. Short sentences, one idea each. Read it aloud — if you stumble or run out
  of breath, the sentence is too long or too abstract. Keep sentences under ~20 words.
- Use contractions (I'm, I've, you're, that's). Active voice. Own the work: "the service I built".
- No self-adjectives. Show it through the work, never "I am a passionate, results-driven engineer".
- ONE number per claim, not a wall of metrics. Name the real projects from the facts with the real numbers from the facts.
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

THE MISMATCH FUSE (decide this BEFORE writing anything):
A letter may only SELL real overlap. NEVER mention, list, or apologize for qualifications the
candidate lacks — no "I don't have X", no "while I haven't", no "happy to talk through where
I'd need to grow". A letter that has to negotiate a gap is a letter that should not exist.
So first decide: does the candidate's actual professional identity (backend / distributed-systems
software engineer) credibly serve this role's PRIMARY domain? If the role is a specialist
practitioner job in another field — offensive security / red team / pentest, hardware or silicon
engineering, mechanical/electrical, construction, legal, research science — and the facts contain
no real work IN that field, DO NOT write a letter. Output exactly one line instead:
MISMATCH: <one plain sentence naming the field gap>
Nothing else. Adjacent is fine (a backend SWE applying for a backend role at a security COMPANY
is a normal match); dressing adjacency up as field expertise is not — that fails in the interview.
What is NOT a mismatch (write the letter, sell only the real overlap, never mention the gap):
a preferred/required degree or PhD, publication wishes, years-of-experience bars, missing
individual tools or languages, or a JD that wants research/domain flavor ALONGSIDE real backend
software engineering she does have. The fuse is for a DIFFERENT PRACTICE — not for a harder or
fancier version of her own practice.

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
    if not _ready(cfg) or not (text or "").strip():
        return {"pass": True, "issues": [], "fix": ""}
    ai = cfg.get("ai") or {}
    user = (f"Candidate facts:\n{facts}\n\n{context}\n\n"
            f'Text to judge:\n"""\n{text}\n"""\n\nJudge it and return the JSON verdict.')
    try:
        t = complete(cfg, system=_CRITIC_SYSTEM, user=user, max_tokens=900, thinking=False)
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
    if not _ready(cfg):
        return None
    ai = cfg.get("ai") or {}
    facts = _facts(cfg, role)
    base_user = (
        f"Candidate facts (the ONLY claims you may make):\n{facts}\n\n"
        f"Company: {role.get('company')}\nRole: {role.get('title')}\n"
        f"{trust.fence('job description', role.get('jd_text'), 6000)}\n\n"
        f"Write the paragraph in under {max_words} words."
    )
    def _gen(extra: str) -> str:
        return complete(cfg, system=_personalize(_LETTER_SYSTEM, cfg) + _lessons(), user=base_user + extra, max_tokens=1500).strip()

    def _finish(text: str | None, tries_used: int, verdict: dict | None) -> str | None:
        if text:
            answer_log.log("cover_letter", role, answer=text,
                           model=model_label(cfg), effort=ai.get("effort"), tries=tries_used,
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
        if text.startswith("MISMATCH:"):
            # the fuse fired: a domain-mismatch role must get NO letter, not a spun one.
            # Return the marker so the endpoint can refuse with the reason (409), and log it
            # as unanswered — a blank box beats a letter that opens with the candidate's gaps.
            answer_log.log("unanswered", role, reason="mismatch_fuse",
                           surface="cover_letter", tries=used, answer=text)
            return text
        bad = _hard_reject(text, cfg)
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


def _legal_guidance(cfg: dict) -> str:
    """One line telling the picker how THIS candidate answers the two legal yes/no questions and
    a visa-status list — from profile.work_authorized / needs_sponsorship / visa_status_pick."""
    prof = cfg.get("profile") or {}
    wa = prof.get("work_authorized", True)
    ns = bool(prof.get("needs_sponsorship", False))
    pick = prof.get("visa_status_pick") or ["EAD", "Employment Authorization Document", "Other"]
    wrong = prof.get("status_terms_wrong") or ["citizen", "permanent resident", "H-1B"]
    return (f"'authorized to work in the US?' → {'Yes' if wa else 'No'}"
            + (" (and, if the options say both, the one that also says NO sponsorship is needed)" if wa and not ns
               else " (and, if the options say both, the one that says sponsorship WILL be needed)" if wa and ns else "")
            + f"; 'require visa sponsorship now or in the future?' → {'Yes' if ns else 'No'}"
            + f"; a work-authorization or visa STATUS list → the option naming {' / '.join(pick[:3])}"
            + f" (never {', '.join(wrong[:4])})")


def pick_option(cfg: dict, role: dict, question: str, options: list[str]) -> int:
    """Choose the best option INDEX for a non-sensitive dropdown whose choices are full
    sentences, grounded in the candidate facts. Returns -1 when none fit or the question is one
    she should answer herself."""
    if _NEVER.search(question or "") or any(_NEVER.search(o or "") for o in options or []):
        answer_log.log("pick_option", role, question=(question or "").strip(),
                       index=-1, reason="never_filter")
        return -1
    if not _ready(cfg) or not options:
        return -1
    ai = cfg.get("ai") or {}
    numbered = "\n".join(f"{i}. {o}" for i, o in enumerate(options))
    user = (
        f"Candidate facts:\n{_facts(cfg, role)}\n\n"
        f"Application question: {question}\n\nOptions:\n{numbered}\n\n"
        "Pick the ONE option that is correct for this candidate given the facts. Legal / "
        f"eligibility questions ARE answerable from the facts: {_legal_guidance(cfg)}; demographic "
        "self-ID → exactly the recorded preference. If it's a relocation/on-site question, follow "
        f"the recorded relocation fact ({(cfg.get('profile') or {}).get('relocation') or 'not on file'}). "
        "Answer -1 ONLY for salary/compensation, referral source, signature fields, or when no "
        "option is correct for the facts on file.\n"
        "Reply with ONLY the option number (an integer), nothing else."
    )
    text = complete(cfg, system="You map a candidate's facts to the single correct dropdown option. Answer with one integer.",
                    user=user, max_tokens=400, thinking=False)
    m = re.search(r"-?\d+", text)
    idx = int(m.group(0)) if m else -1
    if not (0 <= idx < len(options)):
        idx = -1
    answer_log.log("pick_option", role, question=(question or "").strip(), options=options,
                   index=idx, answer=(options[idx] if idx >= 0 else None),
                   model=model_label(cfg))
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

    user = "\n".join(parts)

    def _ask(max_tokens: int, think: bool) -> str:
        return complete(cfg, system=_RECITE_SYSTEM + _lessons(), user=user, max_tokens=max_tokens, thinking=think)

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
