#!/usr/bin/env python3
"""probe_ats.py — find the companies LinkedIn told us about that we could be scanning directly.

The LinkedIn nightly is good at DISCOVERING companies and bad at delivering their jobs: it
hands over whatever handful of postings happened to surface in a keyword sweep. Mercor is the
worked example — LinkedIn gave 4 roles, its public Ashby board has 77.

So this walks every company that appears in the DB only via `li:` roles and is NOT already in
`config.companies`, guesses board slugs from the name, and asks the three public board APIs
whether such a board exists. Read-only reconnaissance:

  * it hits Greenhouse / Lever / Ashby public endpoints — never LinkedIn, so the "don't touch
    the logged-in session" rule is not in play here at all
  * it NEVER edits config.yaml. Adding a company means accepting its roles into scoring while
    company_notes.yaml has no culture/wlb prior for it, which quietly biases the score — that
    is the owner's call, so this only writes a report to review.

    jobscan/.venv/bin/python jobscan/tools/probe_ats.py [--limit N] [--delay 0.4]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfgmod, store  # noqa: E402

BOARDS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}


def slugs(name: str) -> list[str]:
    """Plausible board slugs for a display name, most likely first.

    Boards are usually the bare lowercased name; the suffix-stripped and hyphenated forms
    cover "Foo Inc." / "Foo Technologies" / "Foo AI" style entries.
    """
    n = name.strip().lower()
    n = re.sub(r"[‘’'`,.]", "", n)
    # split on any bracket or vertical bar, INCLUDING the box-drawing │ (U+2502) that some
    # LinkedIn company names use: "Ceribell │ AI-Powered Point-of-Care EEG"
    n = re.sub(r"\s*[|｜│(\[].*$", "", n).strip()
    # Suffix-stripped variant is a SECOND guess, never the only one: "Cockroach Labs" really
    # is board `cockroachlabs`, so dropping "labs" first would miss it.
    stripped = re.sub(r"\s+", " ", re.sub(
        r"\b(inc|llc|ltd|limited|corp|corporation|co|gmbh|plc|"
        r"technologies|technology|labs|software|systems|group|holdings)\b", "", n)).strip()
    # Brand-domain prefixes. Teleport was missed on the first sweep: its board is
    # `goteleport` after goteleport.com, while the display name is just "Teleport". The
    # go/get/try/join/use pattern is common enough in startup naming to be worth 5 extra
    # probes on the companies that would otherwise come back empty.
    variants = [n, stripped]
    for p in ("go", "get", "try", "join", "use"):
        variants.append(p + n)
    out: list[str] = []
    for base in variants:
        for cand in (re.sub(r"[^a-z0-9]", "", base),                # foobar
                     re.sub(r"[^a-z0-9]+", "-", base).strip("-")):  # foo-bar
            # ≥4 chars, and never a bare stripped stem: "Open Systems Technologies" reduced to
            # "open" would have probed — and matched — OpenAI's board and credited it to the
            # wrong company. A wrong board is worse than no board.
            if cand and cand not in out and len(cand) >= 4:
                out.append(cand)
    return out


def count_jobs(ats: str, payload) -> int:
    if ats == "greenhouse":
        return len((payload or {}).get("jobs") or [])
    if ats == "lever":
        return len(payload or [])
    return len((payload or {}).get("jobs") or [])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="probe at most N companies")
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--out", default="output/ats_probe.md")
    args = ap.parse_args()

    cfg = cfgmod.load_config()
    known = {cfgmod.norm_key(c["name"]) for c in (cfg.get("companies") or [])}
    conn = store.connect_jobs(cfg)
    rows = conn.execute(
        "SELECT company, company_norm, COUNT(*) n, "
        "       SUM(CASE WHEN job_id LIKE 'li:%' THEN 1 ELSE 0 END) n_li "
        "FROM roles GROUP BY company_norm ORDER BY n DESC").fetchall()
    conn.close()

    orphans = [r for r in rows if r["company_norm"] not in known and r["n_li"] > 0]
    if args.limit:
        orphans = orphans[:args.limit]
    print(f"{len(orphans)} orphan companies (seen via LinkedIn, not in config.companies)")

    found, misses = [], 0
    with httpx.Client(timeout=12, follow_redirects=True,
                      headers={"User-Agent": "jobscan-probe/1.0"}) as client:
        for i, r in enumerate(orphans, 1):
            hit = None
            for slug in slugs(r["company"]):
                for ats, url in BOARDS.items():
                    time.sleep(args.delay)
                    try:
                        resp = client.get(url.format(slug=slug))
                    except Exception:  # noqa: BLE001
                        continue
                    if resp.status_code != 200:
                        continue
                    try:
                        n = count_jobs(ats, resp.json())
                    except Exception:  # noqa: BLE001
                        continue
                    if n > 0:
                        hit = {"company": r["company"], "company_norm": r["company_norm"],
                               "ats": ats, "slug": slug, "board_jobs": n,
                               "in_db_now": r["n"], "via_linkedin": r["n_li"]}
                        break
                if hit:
                    break
            if hit:
                found.append(hit)
                print(f"  [{i}/{len(orphans)}] ✅ {hit['company'][:28]:28} {hit['ats']}:{hit['slug']} "
                      f"→ {hit['board_jobs']} jobs (库里现有 {hit['in_db_now']})")
            else:
                misses += 1

    found.sort(key=lambda h: -h["board_jobs"])
    gain = sum(h["board_jobs"] - h["in_db_now"] for h in found)
    lines = ["# 孤儿公司 ATS 探测结果", "",
             f"探测 {len(orphans)} 家（只在 LinkedIn 出现、不在 `config.companies` 里）",
             f"**找到公开板 {len(found)} 家**，未找到 {misses} 家。",
             f"这些板上共 {sum(h['board_jobs'] for h in found)} 个岗位，库里现有 "
             f"{sum(h['in_db_now'] for h in found)} 个 → **潜在新增约 {gain} 个**。", "",
             "接进来的岗位走正规 ATS，apply_url 天然正确，浏览器插件能直接认出。", "",
             "> ⚠️ 未自动写入 config。新公司在 `company_notes.yaml` 里没有 culture/wlb 先验，",
             "> 打分会退回默认值——先确认要哪几家，再加。", "",
             "| 公司 | ATS | slug | 板上岗位 | 库里现有 | 净增 |",
             "|---|---|---|---:|---:|---:|"]
    for h in found:
        lines.append(f"| {h['company']} | {h['ats']} | `{h['slug']}` | {h['board_jobs']} | "
                     f"{h['in_db_now']} | +{h['board_jobs'] - h['in_db_now']} |")
    lines += ["", "## 可直接粘进 config.yaml `companies:` 的片段", "", "```yaml"]
    # Quote any name YAML's flow mapping would mis-split. "Scribd, Inc." does NOT raise — it
    # parses to {name: "Scribd", "Inc.": None}, i.e. a silently wrong company plus a junk key.
    def _y(name: str) -> str:
        return f'"{name}"' if re.search(r'[,:{}\[\]#&*!|>%@`"\']', name) else name

    lines += [f"  - {{ name: {_y(h['company'])}, ats: {h['ats']}, slug: {h['slug']} }}"
              for h in found]
    lines += ["```", ""]
    out = cfgmod.resolve_path(args.out)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n{len(found)} boards found → {out}  (潜在新增 ~{gain} 个岗位)")


if __name__ == "__main__":
    main()
