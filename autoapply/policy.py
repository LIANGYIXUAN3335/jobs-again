"""Routing layer: sensitive form questions → the owner's standing answers in config.yaml.

All answer DATA lives in config.yaml (`autoapply.answers` + the existing `eeo:` block) so
the owner reviews policy in one place; this module only classifies a question and looks up
the configured answer. Topics with no configured answer, or where the visible options
can't truthfully express the configured answer, raise Quarantine: the job is filled as far
as safe and left unsubmitted for the human queue. Authorization trail: SAFE_APPLY_PIPELINE.md
Owner Operating Memory (2026-08-18) + owner chat 2026-08-20; decisions in AUTOAPPLY_DESIGN.md §六.
"""
from __future__ import annotations

import datetime
import re

from .models import Answer, Question, QType, Quarantine, Source

# Mirrors ai._NEVER + the extension EXCLUDE list: anything those layers refuse is ours.
SENSITIVE = re.compile(
    r"gender|ethnic|hispanic|latin|\brace\b|racial|veteran|disab|pronoun|"
    r"sexual orientation|lgbt|transgender|demographic|self.?identif|equal employment|\beeo\b|"
    r"salary|compensation|desired pay|pay (range|expectation)|referr|source of|how did you hear|"
    r"sponsor|visa|authoriz|work permit|right to work|eligible to work|citizen|"
    r"felony|conviction|criminal|background check|signature|sign your name|"
    r"certif|attest|acknowledg|consent|privacy policy|\bterms\b|gdpr|export.?control|"
    r"u\.?s\.? person|security clearance|date of birth|\bssn\b|social security",
    re.I,
)

# Narrative prompts never get a lookup answer (extension essay-guard lesson: a yes/no
# rule once matched "describe your experience with…" phrasing).
ESSAY = re.compile(r"^\s*(describe|explain|tell us|why|what|how (would|do|did|have)|share|walk us)\b", re.I)

DECLINE_CHAIN = [
    "i don't wish to answer", "i do not wish to answer", "decline to self identify",
    "decline to self-identify", "decline to answer", "prefer not to say",
    "i prefer not to say", "prefer not to answer", "i prefer not to answer",
    "prefer not to disclose", "decline to state", "decline",
]

_MONEY = re.compile(r"\$\s?(\d{2,3})[,.]?(\d{3})(?:\s*(?:[-–—]|to)\s*\$?\s?(\d{2,3})[,.]?(\d{3}))?")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def best_option(options: list[str], candidates: list[str]) -> str | None:
    """First candidate found (normalized substring, either direction) among the options;
    returns the option's original text so adapters click the exact visible label."""
    normed = [(o, norm(o)) for o in options]
    for cand in candidates:
        c = norm(str(cand))
        if not c:
            continue
        for orig, n in normed:
            if c == n or c in n or n in c:
                return orig
    return None


def yes_option(options: list[str]) -> str | None:
    return best_option(options, ["yes", "i am", "i do", "true"])


def no_option(options: list[str]) -> str | None:
    return best_option(options, ["no", "i am not", "i do not", "false"])


def jd_salary_low(role: dict) -> int | None:
    """Low end of the JD's own published annual pay range, if any."""
    jd = role.get("jd_text") or ""
    best: int | None = None
    for m in _MONEY.finditer(jd):
        lo = int(m.group(1) + m.group(2))
        if 60_000 <= lo <= 500_000:
            best = lo if best is None else min(best, lo)
    return best


def _pick(q: Question, want: str, note: str, *, strict: bool) -> Answer:
    """Configured yes/no-style answer, mapped onto the control's actual options.
    strict=True → no matching option is a Quarantine (never guess on these topics)."""
    if q.options:
        opt = (yes_option(q.options) if norm(want) == "yes" else
               no_option(q.options) if norm(want) == "no" else
               best_option(q.options, [want]))
        if opt is None and strict:
            raise Quarantine(f"options can't express configured answer {want!r}", q.label)
        if opt is None:
            return Answer(q, want, Source.POLICY, note)
        return Answer(q, opt, Source.POLICY, note)
    if q.qtype == QType.CHECKBOX:
        return Answer(q, norm(want) in ("yes", "true"), Source.POLICY, note)
    return Answer(q, want, Source.POLICY, note)


def resolve(q: Question, role: dict, cfg: dict) -> Answer | None:
    """Look up the standing answer for one question; None = not a policy topic.
    Raises Quarantine when the topic is policy-governed but unanswerable as configured."""
    aa = ((cfg.get("autoapply") or {}).get("answers") or {})
    eeo = (cfg.get("eeo") or {})
    profile = (cfg.get("profile") or {})
    label = norm(q.label)
    full = f"{label} {norm(str(q.meta.get('context', '')))}"

    if ESSAY.search(q.label or ""):
        return None

    # Topics with no standing answer by design → straight to the human queue.
    if re.search(r"felony|conviction|criminal|misdemeanor", full):
        raise Quarantine("criminal-history question", q.label)
    if re.search(r"export.?control|u\.?s\.? person|itar|security clearance", full):
        raise Quarantine("export-control / US-person / clearance question", q.label)
    if re.search(r"non.?compete|arbitrat|restrictive covenant|assignment of invention", full):
        raise Quarantine("unusual legal commitment", q.label)
    if re.search(r"date of birth|\bssn\b|social security|passport|driver.?s licen", full):
        raise Quarantine("identity-document data", q.label)
    if re.search(r"referr", full):
        raise Quarantine("referral field (no known referrer — never invent)", q.label)

    if re.search(r"sponsor", full):
        return _pick(q, aa.get("needs_sponsorship", "No"), "config needs_sponsorship", strict=True)
    if re.search(r"authoriz|work permit|right to work|eligible to work|legally able to work", full):
        return _pick(q, aa.get("work_authorized", "Yes"), "config work_authorized", strict=True)
    if re.search(r"visa (status|type)|immigration status|current visa", full) and q.options:
        opt = best_option(q.options, list(aa.get("visa_status_pick", ["EAD", "Other"])))
        if opt:
            return Answer(q, opt, Source.POLICY, "config visa_status_pick")
        raise Quarantine("visa-status options don't fit the truthful configured picks", q.label)
    if re.search(r"citizen|green card|permanent resident", full):
        if q.options:
            opt = no_option(q.options)
            if opt:
                return Answer(q, opt, Source.POLICY, "truthful No (EAD holder)")
        raise Quarantine("citizenship question without a clean truthful No", q.label)

    if re.search(r"hispanic|latin", full):
        return _q_eeo(q, [eeo.get("hispanic_latino", "No")], "eeo.hispanic_latino")
    if re.search(r"veteran", full):
        return _q_eeo(q, [eeo.get("veteran", ""), "i am not a protected veteran"], "eeo.veteran")
    if re.search(r"disab", full):
        return _q_eeo(q, [eeo.get("disability", ""), "no, i don't have a disability",
                          "no, i do not have a disability"], "eeo.disability")
    if re.search(r"gender|\brace\b|racial|ethnic|pronoun|sexual orientation|lgbt|transgender|"
                 r"demographic|self.?identif", full):
        return _q_eeo(q, [], "decline set")

    if re.search(r"salary|compensation|desired pay|pay (range|expectation|rate)", full):
        lo = jd_salary_low(role)
        if lo is None:
            if not q.required:
                return None
            raise Quarantine("required salary field but JD publishes no range", q.label)
        val = f"${lo:,}" if q.qtype in (QType.TEXT, QType.TEXTAREA) else lo
        return Answer(q, val, Source.POLICY, f"JD published range low end ({lo})")

    if re.search(r"background check", full):
        if re.search(r"willing|consent|agree|undergo|authoriz", full):
            return _pick(q, aa.get("background_check_consent", "Yes"),
                         "config background_check_consent", strict=True)
        raise Quarantine("background-check question that isn't a willingness ask", q.label)
    if re.search(r"privacy|gdpr|data (processing|protection)|consent to (the )?process", full):
        if not aa.get("privacy_consent", False):
            raise Quarantine("privacy consent present but not enabled in config", q.label)
        if q.qtype == QType.CHECKBOX:
            return Answer(q, True, Source.POLICY, "config privacy_consent")
        return _pick(q, "Yes", "config privacy_consent", strict=False)
    if re.search(r"signature|sign your name|type your (full |legal )?name|certif|attest|acknowledg", full):
        if not aa.get("standard_certification", False):
            raise Quarantine("certification field present but not enabled in config", q.label)
        if q.qtype == QType.CHECKBOX:
            return Answer(q, True, Source.POLICY, "config standard_certification")
        if re.search(r"date", label) and "name" not in label:
            return Answer(q, datetime.date.today().isoformat(), Source.POLICY, "attestation date")
        legal = f"{profile.get('legal_first_name', '')} {profile.get('last_name', '')}".strip()
        return Answer(q, legal, Source.POLICY, "config standard_certification (legal name)")

    if re.search(r"how did you hear|source of|where did you (hear|find)", full):
        chain = list(aa.get("how_did_you_hear", ["LinkedIn", "Other"]))
        if q.options:
            opt = best_option(q.options, chain)
            return Answer(q, opt, Source.POLICY, "config how_did_you_hear") if opt else None
        return Answer(q, chain[0], Source.POLICY, "config how_did_you_hear")
    if re.search(r"relocat", full):
        return _pick(q, aa.get("willing_to_relocate", "Yes"), "config willing_to_relocate", strict=False)
    if re.search(r"(work|be) (onsite|on-site|in.?office|in office|hybrid)|commut|days? (a|per) week in", full):
        return _pick(q, aa.get("onsite_hybrid_ok", "Yes"), "config onsite_hybrid_ok", strict=False)
    if re.search(r"travel", full):
        return _pick(q, aa.get("travel_ok", "Yes"), "config travel_ok", strict=False)
    if re.search(r"18 (years|or older)|over (the age of )?18|legal working age", full):
        return _pick(q, aa.get("over_18", "Yes"), "config over_18", strict=False)
    if re.search(r"(previously|ever) (worked|been employed)|worked (at|for|here)", full):
        return _pick(q, aa.get("worked_here_before", "No"), "config worked_here_before", strict=False)
    if re.search(r"(relative|family member|anyone you know).{0,40}(work|employ)|related to.{0,30}employee", full):
        return _pick(q, aa.get("relatives_at_company", "No"), "config relatives_at_company", strict=False)
    if re.search(r"contact.{0,30}(current|present) employer", full):
        return _pick(q, aa.get("contact_current_employer", "No"), "config contact_current_employer", strict=False)
    if re.search(r"(marketing|promotional|newsletter|talent (community|network)).{0,40}"
                 r"(opt|subscribe|contact|updates)|future (openings|opportunities)", full):
        return _pick(q, aa.get("marketing_opt_in", "No"), "config marketing_opt_in", strict=False)
    if re.search(r"(start date|available to start|earliest.{0,20}start|notice period)", full):
        if q.qtype == QType.DATE:
            start = datetime.date.today() + datetime.timedelta(days=14)
            return Answer(q, start.isoformat(), Source.POLICY, "2 weeks notice")
        return Answer(q, aa.get("notice_period", "Available with 2 weeks' notice"),
                      Source.POLICY, "config notice_period")

    return None


def _q_eeo(q: Question, preferred: list[str], note: str) -> Answer | None:
    """Demographic questions: configured wording first, then any decline-flavored option.
    Optional + no fit → leave blank (None); required + no fit → Quarantine."""
    cands = [p for p in preferred if p] + DECLINE_CHAIN
    if q.options:
        opt = best_option(q.options, cands)
        if opt:
            return Answer(q, opt, Source.POLICY, note)
        if not q.required:
            return None
        raise Quarantine("required demographic question with no decline-style option", q.label)
    if not q.required:
        return None
    return Answer(q, cands[0], Source.POLICY, note)
