"""Which slice of the application machine actually converts — measured against the MAILBOX,
not against the column the system forgot to fill in.

`app/funnel.py` already slices conversion by score/ATS/company. It reads `role_state.
first_response_at`, and that column is written by exactly one path: `python -m app.outcomes
--apply`, which only commits its *high-confidence* proposals — the ones where a company has
exactly one open application. Every company she applied to five times is structurally
unresolvable to that matcher, so it writes nothing, so the funnel reports a response rate of
~1% and every slice comes out flat and unreadable. The number is an artefact of the plumbing.

The mailbox knows better. `tools/mail_label` already groups every hiring thread by company and
`app/company_board` already reads a stage off those subjects. This report joins that to the
applications and asks the question the DB cannot answer:

    for each company she applied to, did a real human/stage-level reply arrive AFTER she applied?

That is a COMPANY-level fact, not an application-level one — a recruiter email names a company,
almost never one of nine reqs. So every rate here is `companies that answered / companies
applied to`, and the application counts are shown beside them as exposure, never as a
denominator for a rate they cannot support. Reading "12 Airbnb applications → 1 response" as a
1/12 conversion would be wrong; it is 1/1 at the company level with 12 applications spent.

Three verdicts, decided by rule and never by hand:
  attributable  a credible reply landed on/after the first application there, and nothing
                hiring-shaped had arrived from that company before it. This is the system working.
  pre-existing  the pipeline was already open before she applied (June's manual applications,
                a recruiter InMail, a referral invite). Counting these as conversions is the
                single easiest way to flatter this system, so they are quarantined.
  no-response   only auto-acknowledgements, job alerts, and silence.

"Credible" excludes the ATS auto-ack that happens to contain the words "next steps" — a real
signal must come from a human sender, an assessment vendor (CodeSignal/HackerRank), a calendar
invite, or a thread she replied into.

Everything is opened read-only. This tool never writes to a database.

    python tools/funnel_report.py                      # full report to the terminal
    python tools/funnel_report.py --out output/funnel_analysis.md
    python tools/funnel_report.py --show-companies     # name names in every bucket
    python tools/funnel_report.py --since 2026-07-18   # only the automated era
    python tools/funnel_report.py --json               # machine-readable dump

No network, no LLM, no API key. Re-run it after `python -m tools.mail_label` refreshes the scan.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import company_board  # noqa: E402
from app import config as cfgmod  # noqa: E402
from app import store  # noqa: E402

RANK = company_board._RANK
REAL = RANK["assessment"]          # assessment and above is a real stage; "contact" is not

# job_id prefix -> what she actually did. The `ats` column collapses `li:` and `manual:` into
# "manual", which hides the single biggest split in the data, so the prefix is the source of truth.
SOURCE = {
    "gh": "greenhouse", "ab": "ashby", "wd": "workday", "li": "linkedin",
    "sr": "smartrecruiters", "lv": "lever", "av": "avature", "rp": "rippling",
    "cap": "career-page", "manual": "manual",
}

# A reply is only evidence if a person (or a test vendor, or a calendar) sent it. Cisco's
# "Your Cisco Journey Begins Here – Next Steps for Your Application" is an auto-ack from
# sample-5cc464b5@example.com and reads as an interview invite to any subject-line matcher.
_MACHINE_SENDER = re.compile(r"no-?reply|donotreply|do-not-reply|jobalert|notification", re.I)
_ASSESSMENT_VENDOR = re.compile(
    r"codesignal|hackerrank|coderpad|karat|codility|hackerearth|woven|byteboard", re.I)
# The other half of the machine-sender rule. Greenhouse and Ashby send genuine interview invites
# from no-reply@ all day ("Anthropic invitation to interview for Fellow program"). A subject this
# specific is evidence on its own; "next steps for your application" never is.
_EXPLICIT_INVITE = re.compile(
    r"invit(?:ation|e|ing) (?:you )?to (?:an? )?interview|interview (?:invitation|invite|confirmation)|"
    r"schedule your interview|interview availability|phone screen|"
    r"(?:invitation|invite) to interview", re.I)

# Recruiter-initiated contact. Not a stage signal — its only job is to tell us the conversation
# started before the application did, which disqualifies the company from "attributable".
_OUTREACH = re.compile(
    r"opportunity (?:at|with|for)\b|hello from |reaching out|invitation from .+ to apply|"
    r"are you (?:still )?(?:job |open |looking)|following up on your|"
    r"would you be (?:interested|open)|exciting .+(?:role|opportunity|developer|engineer)|"
    r"new .+ opportunity|let'?s (?:connect|chat)|quick (?:chat|call)\b", re.I)
_REFERRAL = re.compile(r"invitation from .+ to apply|referr", re.I)

APPLIED_STATUSES = ("applied", "interviewing", "rejected")


# --------------------------------------------------------------------------- data loading

def connect_ro(cfg: dict) -> sqlite3.Connection:
    """user_state.db with jobscan.db attached, both strictly read-only. Other agents are writing
    to this tree; a report must never be able to take a write lock or migrate a schema."""
    user = cfgmod.resolve_path(cfg.get("user_db_path", "data/user_state.db"))
    jobs = cfgmod.resolve_path(cfg.get("db_path", "data/jobscan.db"))
    conn = sqlite3.connect(f"file:{user}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH DATABASE 'file:{jobs}?mode=ro' AS jobs")
    return conn


def load_applications(conn, *, since: str = "", until: str = "") -> list[dict]:
    """Every application with a date, joined to its role. `applied_date` is the only honest
    anchor: `status_updated_at` moves whenever anything else about the row does."""
    rows = conn.execute(
        "SELECT s.job_id, s.status, s.applied_date, s.applied_with_resume,"
        "       s.tailored_resume_path, s.tailor_status, s.first_response_at, s.outcome, s.stage,"
        "       r.ats, r.company, r.company_norm, r.title, r.seniority, r.is_remote,"
        "       r.weighted_score, r.verdict, r.first_seen, r.posted_at"
        "  FROM role_state s LEFT JOIN jobs.roles r ON r.job_id = s.job_id"
        f" WHERE s.status IN ({','.join('?' * len(APPLIED_STATUSES))})"
        "   AND s.applied_date IS NOT NULL AND s.applied_date != ''", APPLIED_STATUSES).fetchall()
    out = []
    for r in rows:
        a = dict(r)
        if since and a["applied_date"] < since:
            continue
        if until and a["applied_date"] > until:
            continue
        a["source"] = SOURCE.get(a["job_id"].split(":")[0], a["job_id"].split(":")[0])
        a["co"] = cfgmod.norm_key(a["company"] or a["job_id"].split(":")[1] if ":" in a["job_id"] else "")
        a["match"] = store.resume_score_from_path(a["tailored_resume_path"])
        a["age_days"] = _age_days(a)
        out.append(a)
    return out


def _age_days(a: dict) -> int | None:
    """How stale the posting was when she applied. `posted_at` when the ATS gave one, else
    `first_seen` — which is an upper bound on freshness, never worse than the truth."""
    stamp = (a.get("posted_at") or a.get("first_seen") or "")[:10]
    try:
        return (datetime.date.fromisoformat(a["applied_date"])
                - datetime.date.fromisoformat(stamp)).days
    except Exception:
        return None


def load_threads(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("threads", [])
    except Exception:
        return []


# ----------------------------------------------------------------- mailbox → company signals

def _credible(msg: dict, thread_has_reply: bool) -> bool:
    """Does this message prove a human decision, or is it the ATS talking to itself?"""
    sender = msg.get("from") or ""
    subject = msg.get("subject") or ""
    if _ASSESSMENT_VENDOR.search(sender + " " + subject):
        return True                                   # CodeSignal/HackerRank are real screens
    if _EXPLICIT_INVITE.search(subject):
        return True                                   # too specific for an auto-acknowledgement
    if company_board._BOOKED.search(subject):
        return True                                   # a calendar invite is a booked human
    if thread_has_reply:
        return True                                   # she answered it, so it was worth answering
    return not _MACHINE_SENDER.search(sender)


def company_signals(threads: list[dict]) -> dict[str, dict]:
    """{normalized company: earliest credible stage signal, earliest hiring-shaped contact}.

    Only the `auto` tier: `review` threads are ones mail_label refused to attribute to a
    company, and guessing here would put another company's interview on this row.
    """
    out: dict[str, dict] = {}
    for t in threads:
        if t.get("tier") != "auto" or not t.get("company"):
            continue
        co = cfgmod.norm_key(t["company"])
        rec = out.setdefault(co, {"company": t["company"], "real": None, "contact": None,
                                  "referral": None, "rejected": None, "messages": 0,
                                  "evidence": {}})
        msgs = t.get("messages") or []
        replied = any(m.get("from_me") for m in msgs)
        rec["messages"] += len(msgs)
        for m in msgs:
            subject, when = m.get("subject") or "", m.get("date") or ""
            if not when:
                continue
            if _REFERRAL.search(subject) and (rec["referral"] is None or when < rec["referral"]):
                rec["referral"] = when
            if m.get("from_me"):
                continue
            sig = company_board._classify(subject)
            if sig == "rejection":
                rec["rejected"] = max(rec["rejected"] or "", when)
                continue
            hiring = sig is not None or bool(_OUTREACH.search(subject))
            human = not _MACHINE_SENDER.search(m.get("from") or t.get("from") or "")
            if hiring and (human or _ASSESSMENT_VENDOR.search(subject)) \
                    and not company_board._ackish({**m, "from": m.get("from") or t.get("from")}):
                if rec["contact"] is None or when < rec["contact"]:
                    rec["contact"] = when
            # A calendar invite is a booked human and outranks anything the subject says —
            # "Invitation from an unknown sender: Pinterest Chat | Alex & Crystal" carries no
            # interview keyword at all, and it is the single most decisive message in the thread.
            if RANK.get(sig, -1) < REAL and company_board._BOOKED.search(subject) \
                    and not company_board._ackish({**m, "from": m.get("from") or t.get("from")}):
                sig = "interview"
            if RANK.get(sig, -1) < REAL:
                continue
            if not _credible({**m, "from": m.get("from") or t.get("from")}, replied):
                continue
            if rec["real"] is None or when < rec["real"]:
                rec["real"] = when
            rec["evidence"].setdefault(when, {"kind": sig, "subject": subject[:110],
                                              "from": (m.get("from") or t.get("from") or "")[:60]})
    return out


def verdicts(apps: list[dict], signals: dict[str, dict]) -> dict[str, dict]:
    """One row per company applied to: exposure, first application, and what came back."""
    by_co: dict[str, list[dict]] = collections.defaultdict(list)
    for a in apps:
        by_co[a["co"]].append(a)
    out = {}
    for co, rows in by_co.items():
        first = min(r["applied_date"] for r in rows)
        sig = signals.get(co) or {}
        real, contact = sig.get("real"), sig.get("contact")
        if not real:
            verdict, lag = "no-response", None
        elif real < first:
            verdict, lag = "pre-existing", None
        elif contact and contact < first:
            verdict, lag = "pre-existing", None            # recruiter opened it, not the application
        else:
            verdict = "attributable"
            lag = (datetime.date.fromisoformat(real) - datetime.date.fromisoformat(first)).days
        out[co] = {
            "company": rows[0]["company"] or co, "apps": len(rows), "first_applied": first,
            "verdict": verdict, "real_at": real, "contact_at": contact, "lag_days": lag,
            "referral_at": sig.get("referral"), "rejected_at": sig.get("rejected"),
            "sources": sorted({r["source"] for r in rows}),
            "evidence": (sig.get("evidence") or {}).get(real) if real else None,
            "db_knows": any(r["first_response_at"] for r in rows),
        }
    return out


# --------------------------------------------------------------------------------- slicing

def _band(v, edges: list[float], unit: str = "") -> str:
    if v is None:
        return "unknown"
    lo = None
    for e in edges:
        if v < e:
            return f"<{_n(e)}{unit}" if lo is None else f"{_n(lo)}–{_n(e)}{unit}"
        lo = e
    return f"{_n(edges[-1])}+{unit}"


def _n(x) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def slice_by(apps: list[dict], cos: dict[str, dict], key, *, order=None) -> list[dict]:
    """Group applications by `key(app)` and report company-level conversion inside each group.

    A company lands in every bucket it has an application in, so bucket company counts sum to
    more than the corpus. That is the honest shape of the data: she applied to Airbnb through
    both greenhouse and linkedin and the reply names neither.
    """
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for a in apps:
        groups[key(a)].append(a)
    rows = []
    for name, sel in groups.items():
        companies = {a["co"] for a in sel}
        hit = {c for c in companies if cos.get(c, {}).get("verdict") == "attributable"}
        pre = {c for c in companies if cos.get(c, {}).get("verdict") == "pre-existing"}
        rows.append({
            "bucket": name, "apps": len(sel), "companies": len(companies),
            "responded": len(hit), "pre_existing": len(pre),
            "rate": len(hit) / len(companies) * 100 if companies else 0.0,
            "who": sorted(cos[c]["company"] for c in hit),
        })
    if order:
        rows.sort(key=lambda r: order.index(r["bucket"]) if r["bucket"] in order else 99)
    else:
        rows.sort(key=lambda r: -r["apps"])
    return rows


def analyse(cfg: dict, conn, threads: list[dict], *, since="", until="") -> dict:
    apps = load_applications(conn, since=since, until=until)
    signals = company_signals(threads)
    cos = verdicts(apps, signals)

    score_edges = [60, 70, 80, 85]
    match_edges = [80, 85, 90, 95]
    age_edges = [2, 4, 8, 15, 31]
    vol_edges = [2, 4, 8, 16]

    def vol(a):
        return _band(cos[a["co"]]["apps"], vol_edges, " apps")

    # The control group nobody set up on purpose: companies that opened a real conversation in
    # the same weeks WITHOUT a single application from this system. Recruiter InMail, staffing
    # agencies, referrals, applications she made by hand. This is what 840 applications is
    # actually competing against, and it is the only comparison that can justify the machine.
    start = min((a["applied_date"] for a in apps), default="")
    off = [dict(v, co=k) for k, v in signals.items()
           if v.get("real") and v["real"] >= start and k not in cos]
    off.sort(key=lambda v: v["real"])

    # Everything the mailbox says reached an actual interview inside the window, whoever sourced
    # it — the yardstick the `interviews` table is measured against below.
    booked = {k: v for k, v in signals.items()
              if any(when >= start and RANK.get(ev["kind"], -1) >= RANK["interview"]
                     for when, ev in (v.get("evidence") or {}).items())}

    return {
        "generated": store.la_today(),
        "apps": apps, "companies": cos, "signals": signals, "off_system": off,
        "interview_stage": booked,
        "window": (start, max((a["applied_date"] for a in apps), default="")),
        "slices": {
            "source": slice_by(apps, cos, lambda a: a["source"]),
            "score": slice_by(apps, cos, lambda a: _band(a["weighted_score"], score_edges),
                              order=[_band(v, score_edges) for v in (0, 60, 70, 80, 85)] + ["unknown"]),
            "match": slice_by(apps, cos, lambda a: _band(a["match"], match_edges),
                              order=[_band(v, match_edges) for v in (0, 80, 85, 90, 95)] + ["unknown"]),
            "volume": slice_by(apps, cos, vol,
                               order=[_band(v, vol_edges, " apps") for v in (1, 2, 4, 8, 16)]),
            "resume": slice_by(apps, cos, lambda a: a["applied_with_resume"] or "unknown",
                               order=["tailored", "base", "unknown"]),
            "age": slice_by(apps, cos, lambda a: _band(a["age_days"], age_edges, "d"),
                            order=[_band(v, age_edges, "d") for v in (0, 2, 4, 8, 15, 31)] + ["unknown"]),
            "seniority": slice_by(apps, cos, lambda a: a["seniority"] or "unknown"),
        },
    }


# ------------------------------------------------------------------------------- interviews

def interview_attribution(conn, cos: dict[str, dict]) -> list[dict]:
    """Every logged interview, tagged with whether a system application could have caused it.

    `interviews.job_id` is NULL for almost every row — the table is fed from the interview-review
    markdown, which names a company and a date and nothing else. So the link has to be rebuilt
    here: same company, and the interview dated on/after the first application there.
    """
    out = []
    for r in conn.execute("SELECT marker_id, job_id, company, happened_on, round_type"
                          "  FROM interviews ORDER BY happened_on").fetchall():
        co = cfgmod.norm_key(r["company"])
        rec = cos.get(co)
        when = r["happened_on"]
        when_full = when if len(when) >= 10 else f"{when}-28"       # "2026-06" → end of June
        if not rec:
            how = "not-applied"                       # never went through this system at all
        elif when_full < rec["first_applied"]:
            how = "predates-apply"
        elif rec["verdict"] == "pre-existing":
            how = "pre-existing"
        else:
            how = "post-apply"
        out.append({"marker_id": r["marker_id"], "company": r["company"], "when": when,
                    "round": r["round_type"] or "", "linked_job_id": r["job_id"],
                    "how": how, "apps": rec["apps"] if rec else 0,
                    "first_applied": rec["first_applied"] if rec else ""})
    return out


# ----------------------------------------------------------------------------------- output

def _span(window: tuple[str, str]) -> int:
    try:
        return (datetime.date.fromisoformat(window[1])
                - datetime.date.fromisoformat(window[0])).days
    except Exception:
        return 0


def _table(rows: list[dict], label: str, min_n: int) -> list[str]:
    L = [f"{label:>16}  {'apps':>5} {'cos':>5} {'replied':>7} {'rate':>7}  {'pre':>3}  who"]
    L.append(" " * 16 + "  " + "-" * 60)
    for r in rows:
        thin = "" if r["companies"] >= min_n else "  ← n too small"
        who = ", ".join(r["who"][:4]) + ("…" if len(r["who"]) > 4 else "")
        L.append(f"{str(r['bucket']):>16}  {r['apps']:5} {r['companies']:5} {r['responded']:7} "
                 f"{r['rate']:6.1f}% {r['pre_existing']:4}  {who}{thin}")
    return L


def render(a: dict, ivs: list[dict], *, min_n: int, show_companies: bool) -> str:
    cos, apps = a["companies"], a["apps"]
    n_apps, n_cos = len(apps), len(cos)
    hit = [c for c in cos.values() if c["verdict"] == "attributable"]
    pre = [c for c in cos.values() if c["verdict"] == "pre-existing"]
    db_known = sum(1 for x in apps if x["first_response_at"])
    db_pos = sum(1 for x in apps if x["first_response_at"] and x["outcome"] != "rejected")

    L = ["", "=" * 92,
         f"FUNNEL REPORT   {a['window'][0]} → {a['window'][1]}   generated {a['generated']}",
         "=" * 92, "",
         f"{n_apps} applications across {n_cos} companies.", ""]

    L += ["PLUMBING GAP  (what the DB knows vs what the mailbox knows)", "-" * 92,
          f"  role_state.first_response_at set on           {db_known:4} applications "
          f"({db_pos} of them non-rejections)",
          f"  companies with a credible reply in the mail   {len(hit) + len(pre):4}",
          f"    · attributable to an application here       {len(hit):4}",
          f"    · pipeline already open before she applied  {len(pre):4}",
          f"  attributable companies the DB knows about     "
          f"{sum(1 for c in hit if c['db_knows']):4}", ""]
    booked = a["interview_stage"]
    logged = {cfgmod.norm_key(i["company"]) for i in ivs if i["when"] >= a["window"][0]}
    L += [f"  companies at interview stage in the mailbox    {len(booked):4}  "
          f"({', '.join(sorted(v['company'] for v in booked.values()))})",
          f"  of those, present in the `interviews` table    "
          f"{sum(1 for k in booked if k in logged):4}", ""]

    L += ["ATTRIBUTABLE REPLIES  (reply landed after the first application, nothing before it)",
          "-" * 92,
          f"  {'company':20} {'apps':>4} {'applied':>10} {'replied':>10} {'lag':>4}  evidence"]
    for c in sorted(hit, key=lambda x: x["real_at"]):
        ev = c["evidence"] or {}
        flag = " [referral]" if c["referral_at"] else ""
        L.append(f"  {c['company'][:20]:20} {c['apps']:4} {c['first_applied']:>10} "
                 f"{c['real_at']:>10} {c['lag_days']:3}d  {ev.get('kind', ''):10} "
                 f"{ev.get('subject', '')[:44]}{flag}")
    lags = sorted(c["lag_days"] for c in hit if c["lag_days"] is not None)
    if lags:
        L += ["", f"  reply lag: median {lags[len(lags) // 2]}d, max {lags[-1]}d — applications older "
                  f"than that have had their chance."]
    L += [""]

    L += ["PRE-EXISTING PIPELINES  (real, but not caused by these applications)", "-" * 92]
    for c in sorted(pre, key=lambda x: x["real_at"] or ""):
        L.append(f"  {c['company'][:20]:20} {c['apps']:4} applied {c['first_applied']} · "
                 f"first real signal {c['real_at']} · first contact {c['contact_at'] or '—'}")
    L += [""]

    off = a["off_system"]
    L += [f"OFF-SYSTEM PIPELINES  ({len(off)} companies opened a real conversation in the same "
          f"window with ZERO applications from this system)", "-" * 92]
    for c in off:
        L.append(f"  {c['company'][:20]:20} {'':4} first real signal {c['real']} · "
                 f"{c['messages']} messages")
    L += ["", f"  {n_apps} applications bought {len(hit)} conversations. "
              f"Inbound/manual bought {len(off)} over the same {_span(a['window'])} days.", ""]

    titles = {"source": "BY SOURCE  (job_id prefix = how she found and filed it)",
              "score": "BY WEIGHTED SCORE  (does the four-dimension score predict a reply?)",
              "match": "BY RÉSUMÉ MATCH SCORE  (from the tailored PDF filename)",
              "volume": "BY VOLUME AT ONE COMPANY",
              "resume": "BY RÉSUMÉ  (tailored vs base)",
              "age": "BY POSTING AGE WHEN SHE APPLIED",
              "seniority": "BY SENIORITY"}
    for key, title in titles.items():
        L += [title, "-" * 92, *_table(a["slices"][key], key, min_n), ""]

    counts = collections.Counter(i["how"] for i in ivs)
    L += ["INTERVIEWS  (where they actually came from)", "-" * 92,
          f"  {len(ivs)} logged interviews:",
          f"    not-applied     {counts['not-applied']:3}  company never went through this system",
          f"    predates-apply  {counts['predates-apply']:3}  interview happened before she applied there",
          f"    pre-existing    {counts['pre-existing']:3}  company's pipeline predates the application",
          f"    post-apply      {counts['post-apply']:3}  could plausibly be caused by an application here",
          f"  linked to a job_id in the DB: {sum(1 for i in ivs if i['linked_job_id'])}/{len(ivs)}", ""]
    for i in ivs:
        if i["how"] != "not-applied" or show_companies:
            L.append(f"  {i['when']:10} {i['company'][:24]:26} {i['how']:14} "
                     f"apps={i['apps']:2} {i['round'][:38]}")
    L += [""]

    if show_companies:
        L += ["EVERY COMPANY APPLIED TO", "-" * 92]
        for c in sorted(cos.values(), key=lambda x: (-x["apps"], x["company"])):
            L.append(f"  {c['company'][:26]:28} {c['apps']:4} apps  {c['verdict']:13} "
                     f"{','.join(c['sources'])}")
        L += [""]
    return "\n".join(L)


def render_markdown(a: dict, ivs: list[dict], *, min_n: int) -> str:
    """The same numbers as a markdown table dump, for pasting into a written report."""
    cos = a["companies"]
    hit = [c for c in cos.values() if c["verdict"] == "attributable"]
    pre = [c for c in cos.values() if c["verdict"] == "pre-existing"]
    L = [f"# Funnel report — {a['window'][0]} → {a['window'][1]}", "",
         f"{len(a['apps'])} applications · {len(cos)} companies · "
         f"{len(hit)} attributable replies · {len(pre)} pre-existing pipelines · "
         f"{len(a['off_system'])} off-system pipelines", "",
         "## attributable replies", "",
         "| company | apps | first applied | replied | lag | evidence |", "|---|---:|---|---|---:|---|"]
    for c in sorted(hit, key=lambda x: x["real_at"]):
        ev = c["evidence"] or {}
        L.append(f"| {c['company']} | {c['apps']} | {c['first_applied']} | {c['real_at']} |"
                 f" {c['lag_days']}d | {ev.get('kind', '')}: {ev.get('subject', '')} |")
    L += ["", "## pre-existing pipelines (not caused by these applications)", "",
          "| company | apps | first applied | first real signal | first contact |", "|---|---:|---|---|---|"]
    for c in sorted(pre, key=lambda x: x["real_at"] or ""):
        L.append(f"| {c['company']} | {c['apps']} | {c['first_applied']} | {c['real_at']} |"
                 f" {c['contact_at'] or '—'} |")
    L += ["", "## off-system pipelines (zero applications from this system)", "",
          "| company | first real signal | messages |", "|---|---|---:|"]
    for c in a["off_system"]:
        L.append(f"| {c['company']} | {c['real']} | {c['messages']} |")
    L += [""]
    for key, rows in a["slices"].items():
        L += [f"## {key}", "", "| bucket | apps | companies | replied | rate | pre-existing | who |",
              "|---|---:|---:|---:|---:|---:|---|"]
        for r in rows:
            note = "" if r["companies"] >= min_n else " *(n too small)*"
            L.append(f"| {r['bucket']}{note} | {r['apps']} | {r['companies']} | {r['responded']} |"
                     f" {r['rate']:.1f}% | {r['pre_existing']} | {', '.join(r['who'])} |")
        L += [""]
    L += ["## interviews", "", "| when | company | attribution | apps | round |", "|---|---|---|---:|---|"]
    for i in ivs:
        L.append(f"| {i['when']} | {i['company']} | {i['how']} | {i['apps']} | {i['round']} |")
    return "\n".join(L) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mail", default="data/mail_label_scan.json",
                    help="mail_label scan cache (refresh with `python -m tools.mail_label`)")
    ap.add_argument("--since", default="", help="only applications on/after this date")
    ap.add_argument("--until", default="", help="only applications on/before this date")
    ap.add_argument("--min-n", type=int, default=20,
                    help="below this many companies a bucket's rate is flagged unreadable")
    ap.add_argument("--show-companies", action="store_true", help="name every company")
    ap.add_argument("--json", action="store_true", help="dump the analysis as JSON instead")
    ap.add_argument("--out", default="", help="also write a markdown table dump here")
    args = ap.parse_args(argv)

    cfg = cfgmod.load_config()
    threads = load_threads(cfgmod.resolve_path(args.mail))
    if not threads:
        print(f"no mail scan at {args.mail} — every company will read as no-response.\n"
              f"run `python -m tools.mail_label` first.", file=sys.stderr)

    conn = connect_ro(cfg)
    try:
        a = analyse(cfg, conn, threads, since=args.since, until=args.until)
        ivs = interview_attribution(conn, a["companies"])
    finally:
        conn.close()

    if not a["apps"]:
        print("no applications matched those filters")
        return 1

    if args.json:
        print(json.dumps({"window": a["window"], "slices": a["slices"],
                          "companies": a["companies"], "interviews": ivs},
                         indent=1, ensure_ascii=False))
        return 0

    print(render(a, ivs, min_n=args.min_n, show_companies=args.show_companies))
    if args.out:
        out = cfgmod.resolve_path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_markdown(a, ivs, min_n=args.min_n), encoding="utf-8")
        print(f"→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
