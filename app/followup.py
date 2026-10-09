"""The applications that went quiet, and a short note to send about them. Drafts only.

The inbox side of this system is entirely reactive: an email arrives, it gets triaged. The other
half of the loop — an application that goes out and is never heard from again — had nothing
watching it. This is that half.

What it does NOT do, deliberately:
  - It never sends. `mail.send_reply` is reachable from here only through an explicit user action
    in the UI; nothing in this module is wired to the ARMED auto-send path in inbox.py, and a
    follow-up must never be. A nudge that goes out on a timer is a different, worse product.
  - It never makes a new claim. The note is grounded ONLY in the tailored résumé that was
    actually submitted for that role plus the posting — the two things the reader already has.
    Introducing a skill in a follow-up is fabrication with extra steps.
  - It never chases forever. Two per application, then it stops and says so; the honest move
    after two silent nudges is recording `no_response`, not a third email.

Reaching a human is the real constraint. Most of these applications went through an ATS form and
have no reply address at all, so `resolve_contact` looks for one in the mailbox (a repliable
human who has written about that company) and, failing that, marks the note as portal/LinkedIn
text for her to paste. A "no-reply@" confirmation is not a contact.

    python -m app.followup                     # who has gone quiet, ranked
    python -m app.followup --draft <job_id>    # draft one (needs ai.enabled + a key)
"""
from __future__ import annotations

import argparse
import datetime
import re
import sys

import yaml

from . import ai as aimod
from . import config as cfgmod
from . import mail, store, tailoring, trust

DEFAULTS = {
    "after_days": 10,       # standard practice: one to two weeks after applying
    "max_per_app": 2,       # then stop chasing and record the outcome
    "gap_days": 10,         # minimum spacing between the first nudge and the second
    "top": 25,              # she has hundreds of open applications; nudge the ones worth it
    "min_score": 0.0,       # optionally only chase roles above a weighted score
    # 2026-08-10: the first real look at this list was 10 Nvidia + 9 Mastercard out of 25.
    # `resolve_contact` resolves per COMPANY, not per role — so that list is ten separate
    # emails to the same recruiter, which is the spam this module exists to avoid. Cap the
    # roles surfaced per company; 0 disables the cap.
    "per_company": 3,
}

# Addresses that are not a person: ATS relays, and the marketing mailboxes that share a company
# domain. Sending a nudge to sample-50533ce0@example.com is not "finding a contact", it's noise with a
# recipient — so these are rejected outright.
#
# 2026-08-10: the recruiting-side role mailboxes were missing from this list, and the first live
# run proved it — the only "contact" it found for the ten quiet Nvidia roles was
# sample-d502a631@example.com, an unattended referral bot. That is worse than finding nothing,
# because None correctly routes her to the portal while a role mailbox looks like a win and
# reaches no one. Every entry here is anchored to the WHOLE local part, so a real human on a
# team alias (sample-09b4ccdf@example.com) still resolves.
_NOT_A_PERSON = re.compile(
    r"no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|notifications?@|automated@|mailer-daemon|"
    r"^(?:news|newsletter|info|marketing|updates?|press|events?|support|help|billing|sales|"
    r"webinar|community|social|hello|contact|"
    r"careers?|jobs?|recruit(?:ing|ment)|talent(?:[-_.]?acquisition)?|hr|people(?:[-_.]?ops)?|"
    r"staffing|employee[-_.]?referrals?|referrals?|apply|applications?|resumes?)@|"
    r"@(?:greenhouse|lever|ashbyhq|myworkday|smartrecruiters|icims)\.", re.I)
# A human writing about her application says so. Used only for the mailbox fallback, where there
# is no other evidence the sender has anything to do with the job.
_ABOUT_THE_APPLICATION = re.compile(
    r"applicat|\brole\b|position|interview|recruit|candidat|your (?:resume|cv|profile)|"
    r"opportunit|hiring|next steps?|schedul", re.I)


def tuning(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("followup") or {})}


def quiet(cfg: dict, conn) -> list[dict]:
    """Applications with no reply yet, past the threshold, under the follow-up ceiling.

    Ranked by role score: at a few hundred open applications, chasing all of them is neither
    possible nor advisable, so this surfaces the ones actually worth a human's morning.

    "No reply yet" is three tests, not one — `first_response_at`, `stage`, and `outcome` are
    each capable of holding the fact on their own. A linked li:/ATS pair counts once, and no
    company may take more than `per_company` slots (see DEFAULTS).

    tools/followup_nudge.py is what actually puts this in front of her, twice a week.
    """
    tune = tuning(cfg)
    today = datetime.date.fromisoformat(store.la_today())
    rows = [dict(r) for r in conn.execute(
        "SELECT r.job_id, r.company, r.title, r.apply_url, r.weighted_score, r.seniority,"
        "       s.applied_date, s.status, s.stage, s.outcome, s.first_response_at,"
        "       s.tailored_resume_path"
        " FROM jobs.roles r JOIN role_state s ON s.job_id = r.job_id"
        " WHERE s.status = 'applied' AND s.applied_date IS NOT NULL"
        "   AND s.first_response_at IS NULL"
        "   AND (s.outcome IS NULL OR s.outcome = 'in_progress')"
        # A linked pair (li: shadow + ATS canonical, see role_state.duplicate_of) is ONE
        # physical application, exactly as funnel.load counts it. Without this the list
        # showed Otter/Lightning AI twice each and she'd nudge the same job twice
        # (2026-08-10). The shadow still counts if its canonical partner never went out.
        "   AND (s.duplicate_of IS NULL OR s.duplicate_of NOT IN"
        "        (SELECT job_id FROM role_state WHERE applied_date IS NOT NULL))")]

    out = []
    for r in rows:
        if (r["weighted_score"] or 0) < tune["min_score"]:
            continue
        # `first_response_at` is the primary answer-detector, but `stage` is the other half of
        # the same fact and funnel.py already treats it as one. A row whose stage was advanced
        # without going through record_signal (hand edit, migration) must not be chased.
        if store.STAGE_RANK.get(r["stage"] or "applied", 0) >= store.STAGE_RANK["responded"]:
            continue
        try:
            applied = datetime.date.fromisoformat(r["applied_date"])
        except ValueError:
            continue                                    # unparseable date: skip, never guess
        sent = store.followups_for(conn, r["job_id"])
        if len(sent) >= tune["max_per_app"]:
            continue
        # Days quiet counts from the last nudge if there was one, else from the application.
        last = applied
        for f in sent:
            try:
                last = max(last, datetime.date.fromisoformat(f["drafted_at"]))
            except (TypeError, ValueError):
                pass
        need = tune["after_days"] if not sent else tune["gap_days"]
        days_quiet = (today - last).days
        if days_quiet < need:
            continue
        out.append({**r, "days_quiet": days_quiet, "days_since_applied": (today - applied).days,
                    "followups_sent": len(sent)})
    out.sort(key=lambda d: (-(d["weighted_score"] or 0), -d["days_quiet"]))
    if tune["per_company"]:
        seen: dict[str, int] = {}
        kept = []
        for r in out:
            k = cfgmod.norm_key(r["company"])
            if seen.get(k, 0) >= tune["per_company"]:
                continue
            seen[k] = seen.get(k, 0) + 1
            kept.append(r)
        out = kept
    return out[:tune["top"]]


def resolve_contact(cfg: dict, conn, company: str, days: int = 120) -> dict | None:
    """A repliable HUMAN address that has written about this company, or None.

    Two sources, best first:
      1. the inbox triage queue — those senders were already judged to be a real person needing
         a reply (recruiter / scheduling / assessment), which is exactly the person to nudge;
      2. a mailbox scan, but only for mail that both names the company AND reads like it is
         about her application. Without that second test the search happily returns the
         company's marketing list, which looks like success and reaches nobody.

    Read-only on the mailbox. Returning None is a perfectly good answer — it means "send this
    through the portal", not "give up".
    """
    cn = cfgmod.norm_key(company)

    for r in conn.execute(
        "SELECT from_addr, reply_to, subject, in_reply_to, references_hdr, category"
        " FROM reply_queue WHERE repliable=1 AND category IN ('recruiter','scheduling','assessment')"
        " ORDER BY found_at DESC"
    ):
        addr = (r["reply_to"] or "").strip()
        if not addr or _NOT_A_PERSON.search(addr):
            continue
        if cn not in cfgmod.norm_key(f"{r['from_addr']} {r['subject']} {addr}"):
            continue
        return {"addr": addr, "name": re.sub(r"<.*?>", "", r["from_addr"] or "").strip().strip('"'),
                "subject": r["subject"] or "", "source": f"triage queue ({r['category']})",
                "message_id": r["in_reply_to"], "references": r["references_hdr"]}

    try:
        heads = mail.search_recent(cfg, days=days, limit=200)
    except Exception:
        return None
    for h in heads:
        frm, subj = h.get("from") or "", h.get("subject") or ""
        if cn not in cfgmod.norm_key(f"{frm} {subj}") or _NOT_A_PERSON.search(frm):
            continue
        if not _ABOUT_THE_APPLICATION.search(subj):
            continue
        try:
            full = mail.get_message(cfg, h["uid"])
        except Exception:
            continue
        addr = (full.get("reply_to") or "").strip()
        if addr and not _NOT_A_PERSON.search(addr):
            return {"addr": addr, "name": re.sub(r"<.*?>", "", frm).strip().strip('"'),
                    "subject": full.get("subject") or "", "source": "mailbox scan",
                    "message_id": full.get("message_id"), "references": full.get("references")}
    return None


def _system_for(cfg: dict) -> str:
    """The follow-up voice for THIS workspace's candidate (owner 2026-09-23: name and sign-off
    used to be the owner's, hardcoded)."""
    from . import ai as aimod, tailoring
    prof = cfg.get("profile") or {}
    first = prof.get("preferred_name") or prof.get("first_name") or tailoring.candidate_name(cfg).split()[0]
    return aimod._personalize(_SYSTEM, cfg).replace("__FIRST__", first)


_SYSTEM = """You write ONE short follow-up note for a job application that has had no reply. \
First person, as the candidate, __NAME__.

Hard rules:
- 60-120 words. Shorter is better. No subject line, no signature beyond "Best,\\n__FIRST__".
- Every substantive claim must come from the SUBMITTED RÉSUMÉ below. This is a reminder of an \
application they already have, not a new pitch: introducing a skill, project, or number that \
wasn't in the submitted materials is fabrication. If you want to say something you can't source \
there, leave it out.
- Exactly three beats: (1) one sentence naming the role and roughly when you applied; (2) ONE \
concrete thing from the submitted résumé that maps to what the posting asked for — the single \
strongest overlap, named specifically, not a list; (3) one polite question about their timeline.
- No pressure, no guilt, no "just checking in", no "I wanted to follow up", no "I am writing to". \
Open with something real. Use contractions. Short sentences.
- Never imply you have been promised anything, never mention other offers, never ask them to \
hurry. Assume they are busy and that silence means nothing personal.
""" + trust.RULE + """
Output the note body ONLY."""


def draft(cfg: dict, conn, job_id: str, *, channel: str = "email") -> dict:
    """Draft one follow-up. Returns {body, channel, contact, seq_would_be}. Writes nothing."""
    role = store.get_role(conn, job_id)
    if not role:
        raise ValueError(f"unknown job_id {job_id!r}")
    if not aimod.available(cfg):
        raise RuntimeError("AI drafting is off (needs ai.enabled + a key in secrets.yaml)")

    # The submitted résumé — the ONLY source of claims. Fall back to the base only if this role
    # was applied to without a tailored one.
    ypath = tailoring.resume_yaml_path(cfg, role["company"], job_id)
    if ypath.exists():
        t = yaml.safe_load(ypath.read_text(encoding="utf-8")) or {}
        submitted = tailoring.merge(tailoring.load_base(cfg), t)
        source = f"the tailored résumé submitted for this role ({ypath.name})"
    else:
        submitted = tailoring.merge(tailoring.load_base(cfg), {"experience": []})
        source = "her base résumé (no tailored version was submitted for this role)"
    resume_txt = yaml.safe_dump(
        {"summary": submitted.get("summary", ""),
         "skills": submitted.get("skills", []),
         "experience": [{"company": j["company"], "title": j["title"], "bullets": j["bullets"]}
                        for j in submitted.get("experience", [])]},
        sort_keys=False, allow_unicode=True)

    sent = store.followups_for(conn, job_id)
    prior = ("\nThis is the SECOND and final nudge; a first one already went out on "
             f"{sent[-1]['drafted_at']}. Keep it even shorter and do not repeat its angle.\n"
             f"First note was:\n{sent[-1]['body']}\n" if sent else "")
    applied = role.get("applied_date") or "recently"
    user = (
        f"Company: {role['company']}\nRole: {role['title']}\nApplied on: {applied}\n"
        f"Channel: {channel} (an email, a LinkedIn message, or text to paste into a portal — "
        f"match the register; a LinkedIn message is shorter and has no greeting line)\n\n"
        f"SUBMITTED RÉSUMÉ — the only claims you may make ({source}):\n{resume_txt}\n\n"
        f"{trust.fence('job description', role.get('jd_text'), 3500)}\n{prior}\n"
        f"Write the follow-up note."
    )
    from . import ai as aimod
    body = aimod.complete(cfg, system=_system_for(cfg), user=user, max_tokens=1200).strip()
    contact = resolve_contact(cfg, conn, role["company"]) if channel == "email" else None
    return {
        "job_id": job_id, "company": role["company"], "title": role["title"],
        "channel": channel if contact else ("portal" if channel == "email" else channel),
        "contact": contact,
        "subject": f"Following up — {role['title']}",
        "body": body, "seq_would_be": len(sent) + 1,
        "note": None if contact else
        "no repliable human address found in the mailbox for this company — send it through the "
        "portal or LinkedIn instead",
    }


def log(conn, d: dict) -> int:
    """Record a drafted follow-up so the quiet maths and the two-nudge ceiling stay correct."""
    return store.followup_add(
        conn, d["job_id"], channel=d["channel"],
        to_addr=(d["contact"] or {}).get("addr"), subject=d["subject"], body=d["body"])


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--draft", metavar="JOB_ID", help="draft the note for one application")
    ap.add_argument("--channel", default="email", choices=["email", "linkedin", "portal"])
    ap.add_argument("--log", action="store_true", help="record the draft (counts toward the cap)")
    args = ap.parse_args(argv)

    cfg = cfgmod.load_config()
    conn = store.connect_view(cfg)
    try:
        if args.draft:
            d = draft(cfg, conn, args.draft, channel=args.channel)
            who = (f"{d['contact']['addr']}  [{d['contact']['source']}]"
                   if d["contact"] else f"({d['note']})")
            print(f"\n{d['company']} — {d['title']}  [nudge #{d['seq_would_be']}]")
            print(f"to: {who}\nsubject: {d['subject']}\n\n{d['body']}\n")
            if args.log:
                seq = log(conn, d)
                print(f"logged as follow-up #{seq}. Nothing was sent — send it yourself.")
            else:
                print("not logged. Re-run with --log once you actually send it.")
            return 0

        rows = quiet(cfg, conn)
        tune = tuning(cfg)
        if not rows:
            print(f"nothing has gone quiet yet (threshold {tune['after_days']}d since applying)")
            return 0
        print(f"\n{len(rows)} applications quiet for {tune['after_days']}d+ "
              f"(top {tune['top']} by score; max {tune['max_per_app']} nudges each)\n")
        print(f"  {'score':>5} {'quiet':>5} {'sent':>4}  {'company':16} title")
        for r in rows:
            print(f"  {r['weighted_score'] or 0:5.0f} {r['days_quiet']:4d}d {r['followups_sent']:4d}  "
                  f"{r['company'][:16]:16} {r['title'][:46]}")
            print(f"        {r['job_id']}")
        print("\n  draft one:  python -m app.followup --draft <job_id>")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
