#!/usr/bin/env python3
"""ideas_digest.py — prep/save halves of the daily ideas 晨报 (the LLM step runs between
them via headless Claude Code on the subscription, mirroring auto_tailor.sh).

    prep : write the digest prompt for today's fetched items (no LLM, no cost).
           Exits 3 when there is nothing to digest (wrapper then skips the LLM step).
    save : validate the JSON the LLM wrote (structure + every url must be one of today's
           real item urls — hallucinated links are dropped) and upsert into ideas.db.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import ideas as ideasmod  # noqa: E402
from app.config import load_config, resolve_path  # noqa: E402

PROMPT = "scratch_priors/ideas_digest_prompt.md"
OUT = "scratch_priors/ideas_digest_out.json"
GROUPS = ["AI", "开发工具", "内容灵感", "求职相关", "其他"]


def _today_items(conn):
    day = ideasmod._today()
    rows = conn.execute(
        "SELECT source, title, url, meta, score FROM items WHERE first_seen=?"
        " ORDER BY score DESC", (day,)).fetchall()
    return day, [dict(r) for r in rows]


def prep() -> int:
    cfg = load_config()
    conn = ideasmod.connect_ideas(cfg)
    day, items = _today_items(conn)
    conn.close()
    if len(items) < 5:
        print(f"only {len(items)} items for {day} — skipping digest")
        return 3
    out_path = resolve_path(OUT)
    if out_path.exists():
        out_path.unlink()          # stale output must never be ingested as today's
    lines = "\n".join(
        f'- [{it["source"]}] {it["title"]}'
        + (f' ({it["meta"]})' if it["meta"] else "")
        + f'\n  URL: {it["url"]}'
        for it in items)
    prompt = f"""你是 JobScan Ideas 雷达的晨报编辑。下面是 {day} 抓到的 {len(items)} 条原始条目(HN 首页 / Show HN / GitHub Trending / Product Hunt)。

读者画像:资深后端工程师,正在求职(AI 应用 / 分布式系统方向),业余做 AI 动物短视频内容管线,喜欢新工具。

任务:选出 5–8 条最值得她花时间的,写成中文晨报,输出一个 JSON 文件。

规则:
- 只能从下面的条目里选;url 必须原样照抄条目里的 URL,绝不编造、绝不改写。
- 每条 why 用 1–2 句中文人话(≤80 字):这是什么 + 她能拿它干什么。落到具体应用场景,
  最好挂上她的三条线之一(求职工具链 / 后端·AI 工程 / 猫视频内容管线)。
- 「开发工具」组每条必须给一个典型使用场景,句式如「比如用它来 ___」。
- 大模型发版/评测类新闻至多 2 条 —— 优先能直接上手的工具和玩法,不是新闻联播。
- 分组 label 只准用:{ " / ".join(GROUPS) },只输出用到的组。
- tldr:一句话概括今天最重要的 1–2 个信号。
- 把 JSON 写到 {resolve_path(OUT)} ,格式严格为:
  {{"tldr": "...", "groups": [{{"label": "AI", "items": [{{"title": "...", "url": "...", "why": "..."}}]}}]}}
- 只写这一个文件,不做任何其他事,写完即结束。

条目:
{lines}
"""
    p = resolve_path(PROMPT)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(prompt, encoding="utf-8")
    print(f"prompt for {day} written ({len(items)} items) -> {p}")
    return 0


def save() -> int:
    cfg = load_config()
    conn = ideasmod.connect_ideas(cfg)
    day, items = _today_items(conn)
    valid_urls = {it["url"] for it in items}
    out_path = resolve_path(OUT)
    if not out_path.exists():
        conn.close()
        print("no digest output file — LLM step failed?")
        return 1
    try:
        d = json.loads(out_path.read_text(encoding="utf-8"))
        tldr = str(d["tldr"]).strip()
        groups = d["groups"]
        assert tldr and isinstance(groups, list)
    except Exception as e:  # noqa: BLE001
        conn.close()
        print(f"digest JSON invalid: {e}")
        return 1
    clean_groups, dropped = [], 0
    for g in groups:
        its = []
        for it in g.get("items") or []:
            if it.get("url") in valid_urls and it.get("title") and it.get("why"):
                its.append({"title": str(it["title"]), "url": str(it["url"]),
                            "why": str(it["why"])})
            else:
                dropped += 1
        if its:
            clean_groups.append({"label": str(g.get("label") or "其他"), "items": its})
    kept = sum(len(g["items"]) for g in clean_groups)
    if kept < 3:
        conn.close()
        print(f"only {kept} valid items after url check ({dropped} dropped) — not saving")
        return 1
    ideasmod.save_digest(conn, day, json.dumps(
        {"tldr": tldr, "groups": clean_groups}, ensure_ascii=False))
    conn.close()
    print(f"digest saved for {day}: {kept} items in {len(clean_groups)} groups"
          + (f" ({dropped} hallucinated/incomplete dropped)" if dropped else ""))
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "prep":
        sys.exit(prep())
    if cmd == "save":
        sys.exit(save())
    sys.exit("usage: ideas_digest.py prep|save")
