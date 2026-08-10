#!/usr/bin/env python3
"""Match inbox signals to application records."""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as cfgmod, interview_ingest, outcomes, store  # noqa: E402
from tools import mail_label  # noqa: E402   pure helpers only (_display/_domain/_registrable)

BACKUP_DIR = ".backups"
BUCKETS = "data/mail_label_buckets.json"


# ---------------------------------------------------------------------- reading the past

def connect_ro(cfg: dict) -> sqlite3.Connection:
    """Both DBs strictly read-only. A dry run must not be able to take a write lock or ALTER a
    schema — other agents are working in this tree, and the web server is live under launchd."""
    user = cfgmod.user_db_path(cfg)
    jobs = cfgmod.db_path(cfg)
    conn = sqlite3.connect(f"file:{user}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH DATABASE 'file:{jobs}?mode=ro' AS jobs")
    return conn


def load_buckets() -> dict[str, str]:
    try:
        return json.loads((ROOT / BUCKETS).read_text(encoding="utf-8"))
    except Exception:
        return {}


def company_index(by_co: dict) -> dict[str, list[dict]]:
    """{normalised company name: its applications}, keyed by BOTH `roles.company_norm` and the
    display name — the two are written by different code paths and have drifted before."""
    idx: dict[str, list[dict]] = {}
    for co, rows in by_co.items():
        for key in {co, cfgmod.norm_key(rows[0]["company"])}:
            if key:
                idx.setdefault(key, rows)
    return idx


def gazetteer(by_co: dict) -> list[tuple, ...]:
    """(word-boundary pattern, company key) for every company applied to, longest name first —
    mail_label's rule (`_gazetteer`/`_case_ok`), rebuilt from the roles already in hand so a dry
    run needs no writable connection.

    Word boundaries are the whole point. Plain substring matching, which is what the first cut of
    this tool did, resolved `Intelliprogroup` to Intel, `EvolutionaryScale` to Scale AI, `Alex AI`
    to xAI and the `Jack` autoapply newsletter to `Jack & Jill` — four fabricated responses out of
    ten, every one of them a company she never heard from (2026-08-10).
    """
    out = []
    for co, rows in sorted(by_co.items(), key=lambda kv: -len(kv[1][0]["company"] or "")):
        name = (rows[0]["company"] or "").strip()
        if len(name) < 4 or name.lower() in {"the", "inc", "llc"}:
            continue
        out.append((re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", re.I), co, name))
    return out


def _case_ok(matched: str, name: str) -> bool:
    """mail_label's rule: an all-lowercase common word is not a company name. "interview loop"
    must not invent a company called Loop; a name that IS lowercase ("fal") is exempt."""
    return matched != matched.lower() or name == name.lower()


def queue_threads(conn, by_co: dict) -> list[dict]:
    """`reply_queue` as threads, so it can go through the same classifier as the mail cache.

    The queue is the one record that knows she ANSWERED — `status='sent'` closes a conversation
    she wrote back into, and outcomes._credible treats a thread she replied to as real evidence
    however robotic the sender looks. The rows carry no company, so each is resolved the way
    mail_label resolves one: the sending domain, then display names and subjects against the
    gazetteer. Raw addresses are excluded on purpose — matching them filed a Lucid thread under
    LinkedIn (mail_label, 2026-08-03) and would file every InMail under LinkedIn here too.
    """
    idx = company_index(by_co)
    gaz = gazetteer(by_co)
    threads: dict[str, dict] = {}
    for r in conn.execute("SELECT from_addr, from_name, subject, category, status, found_at"
                          "  FROM reply_queue ORDER BY found_at"):
        subject = _flat(r["subject"])
        sender = r["from_addr"] or r["from_name"] or ""
        when = (r["found_at"] or "")[:10]
        if not when:
            continue
        co = resolve_company(sender, subject, idx, gaz)
        if not co:
            continue
        name = idx[co][0]["company"]
        t = threads.setdefault(co, {"company": name, "tier": "auto", "from": sender,
                                    "messages": []})
        t["messages"].append({"date": when, "subject": subject, "from": sender, "from_me": False})
        if r["status"] == "sent":
            # Her reply itself. Dated the same day on purpose: the queue does not store when it
            # was actually sent, and this message is only ever read as "this thread was real".
            t["messages"].append({"date": when, "subject": f"Re: {subject}", "from": "me",
                                  "from_me": True})
    return list(threads.values())


def resolve_company(sender: str, subject: str, idx: dict, gaz: list) -> str | None:
    """Which company applied to, if any, this message is about. None means "don't guess"."""
    dom = mail_label._registrable(mail_label._domain(sender))
    if dom and not mail_label._is_vendor(mail_label._domain(sender)):
        hit = idx.get(cfgmod.norm_key(mail_label._canon(dom))) or idx.get(cfgmod.norm_key(dom))
        if hit:
            return cfgmod.norm_key(hit[0]["company_norm"] or hit[0]["company"])
    text = f"{mail_label._display(sender)} || {subject}"
    for pat, co, name in gaz:
        m = pat.search(text)
        if m and _case_ok(m.group(0), name):
            return co
    return None


def roles_for(company: str, subject: str, idx: dict) -> list[dict]:
    """The applications one message could be about. Exact company key only: mail_label's
    gazetteer is built from `jobs.roles.company`, so a thread it named carries the DB's own
    spelling and containment can only ever be a mistake here. The subject still narrows the
    list to one requisition when it happens to name a title (outcomes._match)."""
    rows = idx.get(cfgmod.norm_key(company))
    if not rows:
        return []
    m = outcomes.applied_scan._TITLE_IN_SUBJECT.search(subject or "")
    return outcomes._match(company, (m.group(1).strip() if m else ""), subject,
                           {cfgmod.norm_key(company): rows})


def _flat(s: str | None) -> str:
    return " ".join((s or "").split())


def classified_messages(threads: list[dict], by_co: dict) -> list[dict]:
    """Every incoming message in the cache that reads as a stage signal, resolved to roles."""
    idx = company_index(by_co)
    out = []
    for t in threads:
        if t.get("tier", "auto") != "auto" or not t.get("company"):
            continue
        replied = any(x.get("from_me") for x in t.get("messages") or [])
        for m in t.get("messages") or []:
            if m.get("from_me") or not m.get("date"):
                continue
            subject = _flat(m.get("subject"))
            sender = m.get("from") or t.get("from") or ""
            sig = outcomes.signal_of(subject)
            if sig != "rejection" and outcomes._RANK.get(sig, -1) < outcomes._REAL:
                # A calendar invite is a booked human whatever the subject says — the same
                # promotion outcomes.company_signals makes, so the two agree on the evidence.
                if not (outcomes._BOOKED.search(subject)
                        and not outcomes._ackish(subject, sender)):
                    continue
                sig = "interview"
            if not outcomes._credible(subject, sender, replied):
                continue
            hits = roles_for(t["company"], subject, idx)
            if not hits:
                continue
            out.append({"date": m["date"], "subject": subject, "from": sender, "from_me": False,
                        "signal": sig, "company": t["company"], "hits": hits})
    out.sort(key=lambda m: m["date"])
    return out


# ---------------------------------------------------------------------------- the plan

def plan(cfg: dict, conn) -> dict:
    by_co = outcomes._open_roles(conn, outcomes.APPLIED_STATUSES)
    threads = outcomes.cached_threads(cfg)
    qthreads = queue_threads(conn, by_co)
    signals = outcomes.merge_signals(outcomes.company_signals(threads),
                                     outcomes.company_signals(qthreads))
    messages = classified_messages(threads, by_co) + classified_messages(qthreads, by_co)
    messages.sort(key=lambda m: m["date"])
    props = outcomes.attribute(messages, signals, by_co)

    # One write per company: the EARLIEST attributable signal is the one that establishes
    # first_response_at, and that is the fact being recovered. Later mail from the same company
    # only advances a stage the row may already have. Deliberately NOT one write per rejection
    # email: marking eight more Nvidia rows `rejected` on a guess would move them out of the
    # review queue and out of the application budget — a far bigger blast radius than the metric
    # this repairs, on evidence that names no requisition (owner call, 2026-08-10).
    best: dict[str, dict] = {}
    for p in props:
        if not outcomes.committable(p):
            continue
        cur = best.get(p["company_norm"])
        if cur is None or (p["date"], -outcomes._RANK.get(p["signal"], -1)) < \
                (cur["date"], -outcomes._RANK.get(cur["signal"], -1)):
            best[p["company_norm"]] = p

    state = {r["job_id"]: dict(r) for r in conn.execute(
        "SELECT job_id, status, stage, outcome, first_response_at, last_contact_at"
        "  FROM role_state")}
    writes = []
    for co, p in sorted(best.items(), key=lambda kv: kv[1]["date"]):
        row = state.get(p["job_id"], {})
        writes.append({**p, "was": {"stage": row.get("stage"), "outcome": row.get("outcome"),
                                   "first_response_at": row.get("first_response_at"),
                                   "status": row.get("status")},
                       "new": not row.get("first_response_at")})
    return {"by_co": by_co, "signals": signals, "proposals": props, "writes": writes,
            "legacy": len(legacy_rows(conn)),
            "interviews": plan_interviews(conn, signals, by_co)}


def plan_interviews(conn, signals: dict, by_co: dict) -> list[dict]:
    """Companies the mailbox puts at interview stage that the `interviews` table has never heard
    of. Reuses interview_ingest's marker id, so a later 复盘 upserts onto the same row rather
    than creating a second one."""
    start = min((r["applied_date"] for rows in by_co.values() for r in rows
                 if r.get("applied_date")), default="")
    have = {cfgmod.norm_key(r["company"]) for r in
            conn.execute("SELECT company FROM interviews")} - {""}
    idx = company_index(by_co)
    out = []
    for co, rec in signals.items():
        booked = sorted((when, ev) for when, ev in (rec.get("evidence") or {}).items()
                        if when >= start
                        and outcomes._RANK.get(ev["kind"], -1) >= outcomes._RANK["interview"])
        if not booked or _already_indexed(co, have):
            continue
        when, ev = booked[0]
        # interview_ingest's own rule — exactly one candidate role or no link at all — but only
        # asked at all once the company is an exact match, because `_match_role` matches on a
        # company_norm PREFIX and "Jack" would happily claim "Jack & Jill"'s requisition.
        job_id, n = interview_ingest._match_role(conn, rec["company"]) if co in idx else (None, 0)
        out.append({"company": rec["company"], "company_norm": co, "happened_on": when,
                    "round_type": _round_label(ev["subject"]), "evidence": ev,
                    "marker": interview_ingest._derived_id(rec["company"], when),
                    "job_id": job_id, "n_candidates": n,
                    "applied": co in by_co})
    out.sort(key=lambda r: r["happened_on"])
    return out


def _already_indexed(co: str, have: set[str]) -> bool:
    """Has she already written a 复盘 for this company, under any spelling?

    Prefix in either direction, the same rule interview_ingest._match_role uses to link a role
    (`company_norm LIKE ? || '%'`). The mailbox calls the company Lucid; her 复盘 calls it Lucid
    Motors; the 07-28 invite and the 07-30 review are the same interview, and inserting a second
    row for it would inflate the very count this repairs."""
    if co in have:
        return True
    return any(len(min(co, h, key=len)) >= 4 and (h.startswith(co) or co.startswith(h))
               for h in have)


def _round_label(subject: str) -> str:
    """The subject as a round name. Gmail's calendar wrapper and the trailing "@ Wed Jul 29,
    2026 11am" are noise; what is left ("Pinterest Chat | Alex & Crystal") is the round."""
    s = re.sub(r"^\s*(?:invitation from an unknown sender|invitation|updated invitation)\s*[:：]\s*",
               "", subject or "", flags=re.I)
    s = re.sub(r"\s*@\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b.*$", "", s)
    s = re.sub(r"\s*\([^)]*@[^)]*\)\s*$", "", s)
    return re.sub(r"\s+", " ", s).strip()[:70] or "Interview"


# ------------------------------------------------------------------------------- the write

def backup_user_db(cfg: dict) -> Path:
    """Snapshot the user DB before writing. sqlite's own backup API, not a file copy: the DB
    runs in WAL mode, so copying the .db alone can miss committed pages still in the -wal."""
    src = cfgmod.user_db_path(cfg)
    now = datetime.datetime.now()
    dest_dir = cfgmod.resolve_path(BACKUP_DIR) / now.strftime("%Y-%m-%d")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{src.name}.bak-outcomes-{now.strftime('%H%M%S')}"
    s = sqlite3.connect(str(src))
    try:
        d = sqlite3.connect(str(dest))
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()
    return dest


def legacy_rows(conn) -> list[str]:
    """Responses recorded before `response_confidence` existed. Every one of them came out of the
    old writer, which committed a signal ONLY when the company had exactly one open application —
    so they are role-level by construction, and leaving them NULL would make the new column read
    as "8 responses we know nothing about"."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(role_state)")}
    if "response_confidence" not in have:
        return []
    return [r["job_id"] for r in conn.execute(
        "SELECT job_id FROM role_state WHERE first_response_at IS NOT NULL"
        "   AND response_confidence IS NULL")]


def apply_plan(cfg: dict, conn, res: dict, *, responses: bool, interviews: bool) -> dict:
    n = {"responses": 0, "interviews": 0, "labelled": 0}
    if responses:
        for job_id in legacy_rows(conn):
            store.upsert_state(conn, job_id, response_confidence="role")
            n["labelled"] += 1
        for w in res["writes"]:
            outcomes.apply_proposal(cfg, conn, w, w["job_id"])
            n["responses"] += 1
    if interviews:
        for iv in res["interviews"]:
            store.interview_upsert(
                conn, marker_id=iv["marker"], company=iv["company"],
                happened_on=iv["happened_on"], round_type=iv["round_type"],
                # No {file: heading} map: nothing on disk backs this row, and web.py renders
                # every key of `sources` as a link into the 复盘 tree.
                sources="{}", job_id=iv["job_id"], source="mailbox")
            n["interviews"] += 1
    return n


# ---------------------------------------------------------------------------- the report

def measure(conn, by_co: dict) -> dict:
    """The numbers the owner has been making decisions on."""
    have_col = {r["name"] for r in conn.execute("PRAGMA table_info(role_state)")}
    conf = ("response_confidence" if "response_confidence" in have_col else "NULL")
    rows = [dict(r) for r in conn.execute(
        f"SELECT job_id, status, stage, outcome, first_response_at, {conf} AS response_confidence"
        "  FROM role_state WHERE status IN ('applied','interviewing','rejected')"
        "    AND applied_date IS NOT NULL AND applied_date != ''")]
    co_of = {r["job_id"]: co for co, rs in by_co.items() for r in rs}
    answered = {co_of.get(r["job_id"]) for r in rows if r["first_response_at"]} - {None}
    ivs = conn.execute("SELECT COUNT(*) n, COUNT(DISTINCT company) c FROM interviews").fetchone()
    return {
        "apps": len(rows),
        "companies": len(by_co),
        "responses": sum(1 for r in rows if r["first_response_at"]),
        "companies_answered": len(answered),
        "role_level": sum(1 for r in rows if r["response_confidence"] == "role"),
        "company_level": sum(1 for r in rows if r["response_confidence"] == "company"),
        "rejections": sum(1 for r in rows if r["outcome"] == "rejected"),
        "interviews": ivs["n"], "interview_companies": ivs["c"],
        "rate": (100.0 * len(answered) / len(by_co)) if by_co else 0.0,
    }


def _table(before: dict, after: dict) -> str:
    rows = [
        ("applications on file", "apps"),
        ("companies applied to", "companies"),
        ("rows with first_response_at", "responses"),
        ("  … attributed role-level", "role_level"),
        ("  … attributed company-level", "company_level"),
        ("companies with a recorded response", "companies_answered"),
        ("rejections recorded", "rejections"),
        ("interviews indexed", "interviews"),
        ("  … distinct companies", "interview_companies"),
    ]
    out = [f"  {'':38} {'before':>8}  {'after':>8}   {'Δ':>6}",
           f"  {'-' * 38} {'-' * 8}  {'-' * 8}   {'-' * 6}"]
    for label, k in rows:
        b, a = before[k], after[k]
        d = a - b
        out.append(f"  {label:38} {b:>8}  {a:>8}   {('+' + str(d)) if d else '·':>6}")
    out.append(f"  {'company-level response rate':38} {before['rate']:>7.1f}% "
               f"{after['rate']:>7.1f}%   {after['rate'] - before['rate']:>+5.1f}")
    return "\n".join(out)


def _projected(before: dict, res: dict, by_co: dict) -> dict:
    """What `measure` would say after the plan runs, without running it."""
    after = dict(before)
    new_rows = [w for w in res["writes"] if w["new"]]
    after["responses"] += len(new_rows)
    after["role_level"] += res["legacy"] + sum(1 for w in new_rows if w["scope"] == "role")
    after["company_level"] += sum(1 for w in new_rows if w["scope"] == "company")
    after["rejections"] += sum(1 for w in new_rows if w["signal"] == "rejection")
    answered = {w["company_norm"] for w in res["writes"]}
    already = {co for co, rs in by_co.items() if any(r.get("first_response_at") for r in rs)}
    after["companies_answered"] = len(already | answered)
    after["rate"] = (100.0 * after["companies_answered"] / len(by_co)) if by_co else 0.0
    after["interviews"] += len(res["interviews"])
    after["interview_companies"] += len({i["company_norm"] for i in res["interviews"]})
    return after


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        epilog="Dry run by default. --apply backs the user DB up into .backups/<date>/ first.")
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    ap.add_argument("--dry-run", action="store_true", help="explicit no-op default")
    ap.add_argument("--verbose", "-v", action="store_true", help="every affected row")
    ap.add_argument("--responses", action="store_true", help="skip the interview backfill")
    ap.add_argument("--interviews", action="store_true", help="skip the response backfill")
    args = ap.parse_args(argv)
    do_resp = args.responses or not args.interviews
    do_ivs = args.interviews or not args.responses

    cfg = cfgmod.load_config()
    conn = connect_ro(cfg)
    try:
        res = plan(cfg, conn)
        before = measure(conn, res["by_co"])
    finally:
        conn.close()
    after = _projected(before, res, res["by_co"])
    buckets = {cfgmod.norm_key(k): v for k, v in load_buckets().items()}

    print(f"backfill_outcomes · {store.la_today()}\n")
    print(f"{len(res['proposals'])} signals read off the mailbox cache → "
          f"{len(res['writes'])} companies to record, {len(res['interviews'])} interviews to index\n")

    if do_resp and res["writes"]:
        print("responses recovered (one per company — the reply that started the conversation)")
        print(f"  {'company':18} {'when':10} {'lag':>4}  {'signal':10} {'scope':8} {'bucket':6} role")
        for w in res["writes"]:
            lag = _lag(w["first_applied"], w["date"])
            n = f"1/{w['n_candidates']}" if w["scope"] == "company" else "1/1"
            print(f"  {w['company'][:18]:18} {w['date']:10} {lag:>4}  {w['signal']:10} "
                  f"{w['scope']:8} {buckets.get(w['company_norm'], '?'):6} {n:>6} {w['job_id'][:40]}")
            if args.verbose:
                print(f"      {w['subject'][:96]}")
                print(f"      from {w['from'][:60]}  was: {w['was']}")
        print()

    pre = [p for p in res["proposals"] if p["verdict"] == "pre-existing"]
    if pre:
        seen = {}
        for p in pre:
            seen.setdefault(p["company"], p)
        print(f"not counted — the conversation was already open before she applied ({len(seen)} companies)")
        for c, p in sorted(seen.items(), key=lambda kv: kv[1]["date"]):
            why = (f"referral {p['referral_at']}" if p.get("referral_at")
                   else f"contact {p['contact_at']}" if p.get("contact_at") else "reply predates")
            print(f"  {c[:18]:18} applied {p['first_applied']}  ·  {why}")
        print()

    if do_ivs and res["interviews"]:
        print("interviews the mailbox knows about and the 复盘 index does not")
        for iv in res["interviews"]:
            link = iv["job_id"] or (f"— ({iv['n_candidates']} candidates)" if iv["n_candidates"]
                                    else "— (never applied through this system)")
            print(f"  {iv['happened_on']:10} {iv['company'][:18]:18} {iv['round_type'][:44]:44} {link}")
        print()

    print(_table(before, after))

    if not args.apply:
        print("\nnothing written (dry run — the default). Re-run with --apply to commit.")
        return 0

    dest = backup_user_db(cfg)
    print(f"\nbackup: {dest}")
    rw = store.connect_view(cfg)
    try:
        n = apply_plan(cfg, rw, res, responses=do_resp, interviews=do_ivs)
        real_after = measure(rw, res["by_co"])
    finally:
        rw.close()
    print(f"wrote {n['responses']} responses · {n['interviews']} interviews"
          f" · labelled {n['labelled']} pre-existing rows role-level\n")
    print(_table(before, real_after))
    return 0


def _lag(first_applied: str | None, when: str) -> str:
    try:
        return str((datetime.date.fromisoformat(when)
                    - datetime.date.fromisoformat(first_applied)).days) + "d"
    except Exception:
        return "—"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
