"""Where does each company actually stand — read off the mail threads, one row per company.

`/outcomes` answers "what happened to application X". This answers the question you actually ask
at 11pm with six processes running in parallel: *whose turn is it, and what has gone quiet?*

Built on the same thread grouping as tools/mail_label, so the board and the Gmail labels always
agree: one company = one label = one row. Stage comes from outcomes._SIGNALS (the same regexes
that drive /outcomes), applied to subject lines newest-first.

Two facts do the real work:
  turn        who sent the last message. You last → waiting on them. Them last → you owe a reply.
  quiet_days  days since that last message. Waiting on them for 10 days is a follow-up; owing
              them a reply for 3 days is a problem.

Read-only: it reads the scan cache (or takes a fresh one) and the jobs DB. Writes nothing.
"""
from __future__ import annotations

import collections
import pathlib
import re
import sys
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import applied_scan, outcomes, store  # noqa: E402

# Rank orders the funnel so "furthest reached" survives a later scheduling email dragging the
# row backwards. `rejected` is not on this ladder — it is terminal and handled separately.
_RANK = {"contact": 0, "applied": 1, "assessment": 2, "interview": 3, "onsite": 4, "offer": 5}
_LABEL = {
    "contact": "接触中", "applied": "已投递", "assessment": "笔试/OA",
    "interview": "面试中", "onsite": "Onsite/终面", "offer": "Offer", "rejected": "已挂",
}
# How long a silence has to run before it means something. Waiting on a company is normal for a
# week; leaving a recruiter's question unanswered for two days is not.
_STALE_THEM, _STALE_YOU = 7, 2
# Past this, silence stops being urgent and starts being an answer. Without it the board opens
# on a recruiter screen you ghosted in September 2025 shouting "you owe a reply (304 days)",
# and the four processes actually in flight this week sit below the fold.
_COLD = 45


# outcomes' patterns are tuned for a decision that WRITES to the DB, so they demand a full
# phrase ("next steps in your interview process"). This board only draws a row, so it can also
# take the bare word once outcomes has passed — "Next steps for your Google interviews" is
# plainly an interview thread, and under-reading it would show the row as mere contact.
_BARE = [("onsite", re.compile(r"\bon-?site\b|final round", re.I)),
         ("interview", re.compile(r"\binterviews?\b", re.I)),
         ("assessment", re.compile(r"\bassessment\b|\bcoding (?:test|challenge)\b", re.I))]


def _classify(subject: str) -> str | None:
    """Subject -> signal name. Bodies are not fetched: a thread quotes its own history, so a
    rejection quoted inside a later reply would keep re-classifying the whole thread as dead."""
    if applied_scan._CONFIRM.search(subject or "") and not applied_scan._NOT_CONFIRM.search(subject or ""):
        return "applied"
    hit = outcomes._classify(subject or "", "")
    if hit:
        return hit[0]
    for name, pat in _BARE:
        if pat.search(subject or ""):
            return name
    return None


# A calendar invite is the last word in a thread far more often than any human reply, and it is
# the one incoming message that does NOT mean you owe an answer — the time is already booked.
# Without this, every scheduled interview reads as "⚠️ 你欠一封回信".
_BOOKED = re.compile(
    r"^\s*(?:invitation|updated invitation|invitación|邀请|日程邀请|confirmed|accepted|"
    r"reminder|canceled|cancelled)\s*[:：]|"
    r"^\s*invitation from an unknown sender\s*[:：]|"       # Gmail's wording for the same thing
    r"(?:has been|is|was) scheduled|appointment booked|calendar invite|"
    r"you'?re all set|see you (?:on|at)\b", re.I)


def _days_since(iso: str) -> int:
    try:
        return (date.today() - datetime.fromisoformat(iso).date()).days
    except Exception:
        return 0


def build(threads: list[dict]) -> list[dict]:
    """One row per company, most-urgent first."""
    by_co: dict[str, list[dict]] = collections.defaultdict(list)
    for t in threads:
        if t.get("company") and t.get("tier") == "auto":
            by_co[t["company"]].append(t)

    rows = []
    for company, ts in by_co.items():
        msgs = [dict(m, _thr=t["thrid"]) for t in ts for m in t.get("messages", [])]
        msgs.sort(key=lambda m: m.get("date") or "")
        if not msgs:
            continue

        best, best_rank, rejected_on, evidence = "contact", -1, None, {}
        for m in msgs:
            sig = _classify(m.get("subject", ""))
            if not sig:
                continue
            if sig == "rejection":
                # Only the LAST word counts: plenty of companies reject one req and interview you
                # for another. A later non-rejection message revives the row.
                rejected_on = m.get("date")
                continue
            stage = {"assessment": "assessment"}.get(sig, sig)
            if _RANK.get(stage, -1) > best_rank:
                best, best_rank = stage, _RANK.get(stage, -1)
            evidence.setdefault(stage, {"date": m.get("date"), "subject": m.get("subject", "")[:110]})

        last = msgs[-1]
        last_date = last.get("date") or ""
        dead = bool(rejected_on) and rejected_on >= (last_date or "")
        stage = "rejected" if dead else best
        from_me = bool(last.get("from_me"))
        quiet = _days_since(last_date)

        booked = not from_me and bool(_BOOKED.search(last.get("subject") or ""))
        cold = not dead and quiet >= _COLD
        if dead:
            turn, urgency, action = "closed", 0, "已结束"
        elif booked and not cold:
            turn, urgency = "booked", 0
            action = f"已排期 · {last_date} 那封是日历邀请，不用回"
        elif cold:
            turn = "them" if from_me else "you"
            urgency = 0
            action = f"已冷掉 · {quiet} 天没动静" + ("（当时球在你那边）" if not from_me else "")
        elif from_me:
            turn = "them"
            urgency = 1 if quiet >= _STALE_THEM else 0
            action = f"等回复 {quiet} 天，可以 follow up 了" if quiet >= _STALE_THEM else "等对方回复"
        else:
            turn = "you"
            urgency = 2 if quiet >= _STALE_YOU else 1
            action = f"⚠️ 你欠一封回信（{quiet} 天）" if quiet >= _STALE_YOU else "该你回复"

        rows.append({
            "company": company,
            "stage": stage, "stage_label": _LABEL.get(stage, stage), "rank": _RANK.get(stage, -1),
            "turn": turn, "urgency": urgency, "action": action, "cold": cold, "dead": dead,
            "quiet_days": quiet, "last_date": last_date,
            "last_subject": (last.get("subject") or "")[:110],
            "last_from": (last.get("from") or "")[:70],
            "threads": len(ts), "messages": len(msgs),
            "rejected_on": rejected_on if dead else None,
            "evidence": evidence,
            # Mirrors tools.mail_label.BUCKETS — the row and the Gmail folder are the same fact.
            "label": f"Interviews/{'3-已挂' if dead else ('2-已冷' if cold else '1-进行中')}/{company}",
        })

    # In flight first, then cold, then closed. Within each: who needs you, how far it got, how
    # long it has sat. Recency breaks ties among the cold, where the stage no longer means much.
    rows.sort(key=lambda r: (r["dead"], r["cold"], -r["urgency"], -r["rank"],
                             -r["quiet_days"] if not r["cold"] else r["quiet_days"]))
    return rows


def summary(rows: list[dict]) -> dict:
    live = [r for r in rows if not r["dead"] and not r["cold"]]
    return {
        "companies": len(rows),
        "live": len(live),
        "your_turn": sum(1 for r in live if r["turn"] == "you"),
        "waiting": sum(1 for r in live if r["turn"] == "them"),
        "booked": sum(1 for r in live if r["turn"] == "booked"),
        "stale": sum(1 for r in live if r["urgency"] >= 1),
        "cold": sum(1 for r in rows if r["cold"]),
        "closed": sum(1 for r in rows if r["dead"]),
        "by_stage": collections.Counter(r["stage"] for r in rows),
    }


def load(cfg: dict, refresh: bool = False, query: str = "is:starred") -> dict:
    """Board rows from the mail_label scan cache; `refresh` takes a fresh IMAP pass."""
    from tools import mail_label
    if refresh or not mail_label.CACHE.exists():
        res = mail_label.scan(cfg, query=query)
        mail_label.CACHE.parent.mkdir(parents=True, exist_ok=True)
        mail_label.CACHE.write_text(__import__("json").dumps(res, indent=1, ensure_ascii=False))
    else:
        res = __import__("json").loads(mail_label.CACHE.read_text())
    rows = build(res.get("threads", []))
    return {"account": res.get("account", ""), "rows": rows, "summary": summary(rows),
            "review": [t for t in res.get("threads", []) if t.get("tier") == "review"]}


def main(argv: list[str]) -> int:
    import argparse

    from app import config as cfgmod
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refresh", action="store_true", help="re-read the mailbox over IMAP")
    args = ap.parse_args(argv)

    d = load(cfgmod.load_config(), refresh=args.refresh)
    s = d["summary"]
    print(f"\n{d['account']} — {s['live']} 个在跑 "
          f"({s['your_turn']} 该你回, {s['waiting']} 等对方, {s['booked']} 已排期) · "
          f"{s['cold']} 个已冷 · {s['closed']} 个已挂\n")
    mark = {2: "🔴", 1: "🟡", 0: "　"}
    for r in d["rows"]:
        if r["dead"] or r["cold"]:
            continue
        print(f"{mark[r['urgency']]} {r['company']:20} {r['stage_label']:12} "
              f"{r['last_date']}  {r['quiet_days']:3}d  {r['action']}")
        print(f"     {r['last_subject'][:88]}")
    cold = [r for r in d["rows"] if r["cold"]]
    if cold:
        print(f"\n已冷 ({len(cold)}，>{_COLD} 天没动静): "
              + ", ".join(f"{r['company']}({r['quiet_days']}d)" for r in cold[:24]))
    dead = [r for r in d["rows"] if r["dead"]]
    if dead:
        print(f"\n已挂 ({len(dead)}): " + ", ".join(r["company"] for r in dead))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
