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
# 2026-08-03 user 拍板从 45 收紧到 15：求职节奏下，15 天没动静的线就当凉了（Nvidia/OCBridge/
# Gamma/Temu 全是这一刀收进已冷的）。手动 booked/closed 标记不受影响。
_COLD = 15


# outcomes' patterns are tuned for a decision that WRITES to the DB, so they demand a full
# phrase ("next steps in your interview process"). This board only draws a row, so it can also
# take the bare word once outcomes has passed — "Next steps for your Google interviews" is
# plainly an interview thread, and under-reading it would show the row as mere contact.
_BARE = [("onsite", re.compile(r"\bon-?site\b|final round", re.I)),
         ("interview", re.compile(r"\binterviews?\b", re.I)),
         ("assessment", re.compile(r"\bassessment\b|\bcoding (?:test|challenge)\b", re.I)),
         # "Next Steps with Microsoft" 这类约面线程的标题。放在 assessment 之后——
         # OA 邮件也爱说 next steps，先让更具体的词赢。排除 "Next Steps for Your
         # (Job) Application"——那是 ATS 投递后的群发（Cisco/Jobgether 误报过面试中）。
         ("interview", re.compile(r"\bnext steps?\b(?!\s+for\s+your\s+(?:job\s+)?application)", re.I))]


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
    r"upcoming interview\b|interview reminder\b|"
    r"you'?re all set|see you (?:on|at)\b", re.I)


# 机器人邮件：投递回执、系统通知。它们既不算"真人回过音"，也没资格决定"该谁回"——
# 2026-08-03 之前 Microsoft 行就是被最新一封 "Thank you for your application!" 回执
# 拖成了错误的球权/时间。
_NOREPLY = re.compile(r"no-?reply|donotreply|do-not-reply|notification|mailer-daemon", re.I)

# 广播/回执类标题：job alert、课程营销、表单确认……哪怕发件人像真人，也不构成欠账。
# （Nvidia 的课程推广、Mercor 的表单回执、Snowflake 的 Job Alert 都曾顶着"你欠一封回信"。）
_BULKISH = re.compile(
    r"job alert|new jobs?\b|newsletter|digest\b|webinar|course\b|certificat|"
    r"we'?re hiring|now hiring|journey begins|profile has been submitted|"
    r"thank you for filling out|roundup|top picks", re.I)


def _ackish(m: dict) -> bool:
    subj = m.get("subject") or ""
    if _BULKISH.search(subj):
        return True
    if applied_scan._CONFIRM.search(subj) and not applied_scan._NOT_CONFIRM.search(subj):
        return True
    if outcomes._NOT_A_RESPONSE.search(subj):
        return True
    # no-reply 发件人 + 读不出任何信号的标题 = 系统信。拒信/OA 邀请常从 no-reply 发，
    # 但它们的标题能被 _classify 认出来，不会走到这一步。
    return bool(_NOREPLY.search(m.get("from") or "")) and _classify(subj) in (None, "applied")


def _days_since(iso: str) -> int:
    try:
        return (date.today() - datetime.fromisoformat(iso).date()).days
    except Exception:
        return 0


def build(threads: list[dict], overrides: dict[str, dict] | None = None) -> list[dict]:
    """One row per company, most-urgent first. `overrides` = store.company_overrides 的人工拍板。"""
    by_co: dict[str, list[dict]] = collections.defaultdict(list)
    for t in threads:
        if t.get("company") and t.get("tier") == "auto":
            by_co[t["company"]].append(t)

    rows = []
    for company, ts in by_co.items():
        ov = (overrides or {}).get(company)
        if ov and ov["action"] == "hidden":
            continue
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

        # 真人回音门槛（2026-08-03 拍板）：一家公司如果收到的全是机器人回执（"我们收到了
        # 你的申请"），它就还不是一个"进程"，不上榜。到过 OA/拒信等真信号、或被人工标记过
        # 的除外。这就是 Fellow Program 这类"投了没人理"的行消失的地方。
        human_in = any(not m.get("from_me") and not _ackish(m) for m in msgs)
        if not ov and not human_in and best_rank < _RANK["assessment"] and not rejected_on:
            continue

        # "该谁回"只看有分量的邮件：机器人回执不改变球权、不重置沉默计时。
        meaningful = [m for m in msgs if m.get("from_me") or not _ackish(m)]
        # ✓已处理：user 在邮件之外回过了（平台里回的/电话里说的/日历上定的）。等价于
        # "当天发过一封信"；之后再来新邮件排在它后面，欠账自然复活。
        if ov and ov["action"] == "acked":
            meaningful = sorted(
                meaningful + [{"from_me": True, "date": (ov.get("set_at") or "")[:10],
                               "subject": "（已处理 — 手动标记）"}],
                key=lambda m: m.get("date") or "")
        last = (meaningful or msgs)[-1]
        last_date = last.get("date") or ""
        forced_dead = bool(ov and ov["action"] == "closed")
        forced_booked = bool(ov and ov["action"] == "booked")
        dead = (bool(rejected_on) and rejected_on >= (last_date or "")) or forced_dead
        stage = "rejected" if (dead and not forced_dead) else best
        from_me = bool(last.get("from_me"))
        quiet = _days_since(last_date)

        booked = forced_booked or (not dead and not from_me
                                   and bool(_BOOKED.search(last.get("subject") or "")))
        if forced_booked and not dead and best_rank < _RANK["interview"]:
            stage = "interview"                     # 人说约上面试了，阶段至少是面试中
        # "进行中"必须是一场真对话：你回过 / 到过 OA·拒信这种真阶段 / 有日历邀请 / 人工
        # 标记过。单向来信（猎头冷启动、Ccsend/Gmass 这类工具信）不占进行中，直接归已冷
        # （2026-08-03 user 审完 Gmail 标签拍板）。
        conversation = (best_rank >= _RANK["assessment"] or bool(rejected_on) or booked
                        or any(m.get("from_me") for m in msgs) or bool(ov))
        cold = not dead and (quiet >= _COLD or not conversation)
        if dead:
            turn, urgency = "closed", 0
            action = (f"手动结案（{(ov.get('set_at') or '')[:10]}）" if forced_dead else "已结束")
        elif booked and (forced_booked or not cold):
            turn, urgency = "booked", 0
            cold = False
            action = ("已排期（手动标记）" if forced_booked
                      else f"已排期 · {last_date} 那封是日历邀请，不用回")
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
            # "欠回信"只对你参与过的进程成立：你回过邮件、或到过 OA/拒信这种真阶段。
            # 猎头冷启动/营销信你从没接过茬的，不算账——不然 40 家"该你回"里 30 家是噪音。
            engaged = (best_rank >= _RANK["assessment"] or bool(rejected_on)
                       or any(m.get("from_me") for m in msgs))
            if _classify(last.get("subject", "")) == "assessment":
                # OA 邀请要的是"去做题"，不是回信。Snowflake/Airbnb 的 HackerRank 邀请
                # 曾把看板刷成一排"你欠一封回信"。
                urgency = 0
                action = f"OA 邀请（{quiet} 天前）— 做或放弃都不用回邮件"
            elif engaged:
                urgency = 2 if quiet >= _STALE_YOU else 1
                action = f"⚠️ 你欠一封回信（{quiet} 天）" if quiet >= _STALE_YOU else "该你回复"
            else:
                urgency = 0
                action = f"对方主动接触，你没接过茬（{quiet} 天）— 想接就回，不接就 🙈"

        rows.append({
            "company": company,
            "stage": stage, "stage_label": _LABEL.get(stage, stage), "rank": _RANK.get(stage, -1),
            "turn": turn, "urgency": urgency, "action": action, "cold": cold, "dead": dead,
            "override": ov["action"] if ov else None,
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
        "your_turn": sum(1 for r in live if r["turn"] == "you" and r["urgency"] >= 1),
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
    conn = store.connect_user(cfg)
    try:
        ov = store.company_overrides(conn)
    finally:
        conn.close()
    rows = build(res.get("threads", []), ov)
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
