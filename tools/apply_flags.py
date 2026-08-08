#!/usr/bin/env python3
"""apply_flags.py — read every tailored résumé's `gaps` and surface the roles that need a
human decision BEFORE the application goes out.

Why this exists: the 0-100 résumé score measures keyword overlap, not fitness to apply. A
security-specialist JD full of Python/AWS/monitoring words scores 90+ against a backend
résumé while being a role she cannot do. The tailoring agents read the whole JD and record
those mismatches honestly in `gaps` — this tool turns that prose back into a checklist.

    jobscan/.venv/bin/python jobscan/tools/apply_flags.py [--md output/apply_flags.md]

Grouped by flag class, hardest blockers first. Nothing is auto-skipped: the owner decides.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfgmod, store  # noqa: E402

# Ordered hardest-first: a role matching several is reported under the first it hits.
# Judged against the job TITLE. Deliberately narrow: only titles that name a discipline she
# does not practise. "Software Engineer, X" is never matched on X alone — plenty of backend
# roles mention security or platform in a suffix.
TITLE_FAMILY = re.compile(
    r"\b(product security|application security|appsec|security engineer|"
    r"detection engineer|threat intelligence|risk (and|&) compliance|"
    r"developer (advocate|relations)|devrel|community engineer|"
    r"facilities|harness|battery|power supply|test development|"
    r"hardware|firmware|asic|silicon|"
    r"(product|program|project|account) (manager|coordinator)|design engineer|"
    r"forward deployed|solutions? (engineer|architect)|sales|"
    r"workday|analytics engineer|zuora|"
    r"network engineer|systems? analyst|operations (engineer|associate|analyst))\b", re.I)

FLAGS: list[tuple[str, str, re.Pattern | None]] = [
    ("BLOCKER · 身份/公民/出口管制", "投了也进不去 —— JD 明写身份门槛", re.compile(
        r"u\.?s\.?\s*person|security clearance|us citizen|u\.s\. citizen|citizenship|"
        r"export[- ]control|itar|green card|permanent resident|"
        r"(not|may not|cannot|unable to)[^.]{0,40}sponsor|no[^.]{0,20}sponsorship|h-?1b", re.I)),
    ("BLOCKER · 单次申请限制", "投错一个就废掉整家公司", re.compile(
        r"one application|single application|only one role|apply(ing)? to (any )?other[^.]{0,40}"
        r"(auto|reject)|per person", re.I)),
    ("BLOCKER · 地点不符", "非美国 / 明确排除加州或湾区", re.compile(
        r"excludes? (california|the bay area|ca\b)|not hir(e|ing) in california|"
        r"based in (basel|switzerland|canada|ontario|london|serbia|belgrade|india|singapore)|"
        r"relocation to|international relocation|uk residents|canada .{0,15}posting", re.I)),
    # Role family is judged from the TITLE, never from `gaps`. A gaps sentence merely
    # *mentioning* "security" or "forward deployed" ("JD prefers security exposure") is not
    # evidence the role IS one — matching prose here produced ~50% false positives
    # (Nvidia Platform Telemetry, Microsoft Senior SWE, Postman Monetization all tripped it).
    ("WARN · 角色族不符", "标题即非软件工程岗（安全专精/运维/DevRel/硬件/PM/销售）", None),
    ("WARN · 级别/合同不符", "entry-level、实习、合同工、时薪、明显降级", re.compile(
        r"entry[- ]level|new grad|graduation date|no more than 2 years|"
        r"\bcontract(or)?\b|per[- ]task|\$\d{2}/hr|hourly|part[- ]time|"
        r"associate (software )?engineer|level down|below her", re.I)),
    ("WARN · 年限硬缺口", "JD 要求年限明显高于她 (~5 年)", re.compile(
        r"\b([7-9]|1\d)\+? (or more )?years|requires? \d{2}\+? years", re.I)),
    ("INFO · 重复贴/中介/雇主存疑", "同一岗位多个 job_id，或聚合器/中介代招", re.compile(
        r"duplicate|identical (jd|posting)|same posting|aggregator|repost|"
        r"intermediar|anonymi[sz]ed client|staffing|recruit(ing|er) (agency|intermediary)|"
        r"employer (identity )?(unverified|inconsistent)|does not name the actual employer", re.I)),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", default="output/apply_flags.md")
    args = ap.parse_args()

    cfg = cfgmod.load_config()
    conn = store.connect_view(cfg)
    meta = {r["job_id"]: r for r in
            conn.execute("SELECT job_id, company, title, location, weighted_score, apply_url "
                         "FROM jobs.roles")}
    state = {r["job_id"]: r for r in
             conn.execute("SELECT job_id, status, tailored_resume_path FROM role_state")}
    conn.close()

    ydir = cfgmod.resolve_path(cfg["output_dirs"]["resumes"])
    buckets: dict[str, list] = defaultdict(list)
    scanned = 0
    for f in sorted(ydir.glob("*.yaml")):
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        jid = d.get("job_id")
        role = meta.get(jid)
        if not role:
            continue
        st = state.get(jid)
        if st and st["status"] in ("applied", "interviewing", "skip", "rejected"):
            continue                       # only things still pending a decision
        scanned += 1
        # `gaps` is agent-authored prose; a few agents emitted dicts (e.g. {keyword: why})
        # instead of plain strings, so flatten whatever shape came back before matching.
        raw = d.get("gaps") or []
        items: list[str] = []
        for g in raw if isinstance(raw, list) else [raw]:
            if isinstance(g, str):
                items.append(g)
            elif isinstance(g, dict):
                items += [f"{k}: {v}" for k, v in g.items()]
            else:
                items.append(str(g))
        gaps = " ".join(items)
        if not gaps:
            continue
        for name, blurb, pat in FLAGS:
            if pat is None:                                   # title-judged, not gaps-judged
                m = TITLE_FAMILY.search(role["title"] or "")
                if m:
                    hit = next((s for s in items if re.search(re.escape(m.group(0)), s, re.I)),
                               f"标题命中「{m.group(0)}」—— 非软件工程岗族")
                    buckets[name].append((role, hit.strip()))
                    break
                continue
            if pat.search(gaps):
                hit = next((s for s in items if pat.search(s)), "")
                buckets[name].append((role, hit.strip()))
                break

    lines = ["# 投递前需人工确认的岗位", "",
             f"来源：{scanned} 份待投简历的 `gaps` 字段（由 tailoring agent 读完整 JD 后写入）。",
             "分数衡量的是关键词重叠，不是能不能投 —— 这份清单补的正是分数看不见的那一半。", ""]
    total = 0
    for name, blurb, _ in FLAGS:
        rows = buckets.get(name) or []
        if not rows:
            continue
        total += len(rows)
        lines += [f"## {name} ({len(rows)})", f"_{blurb}_", ""]
        for role, hit in sorted(rows, key=lambda t: -(t[0]["weighted_score"] or 0)):
            lines.append(f"- **{role['company']}** — {role['title']}  "
                         f"`{role['weighted_score'] or 0:.0f}分`  "
                         f"{role['location'] or ''}")
            lines.append(f"  - {hit[:260]}")
        lines.append("")
    lines.insert(3, f"**共 {total} 个岗位命中，{scanned - total} 个无红旗。**\n")

    out = cfgmod.resolve_path(args.md)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"scanned {scanned} pending résumés → {total} flagged → {out}")
    for name, _, _ in FLAGS:
        if buckets.get(name):
            print(f"  {len(buckets[name]):4d}  {name}")


if __name__ == "__main__":
    main()
