#!/usr/bin/env python3
"""念稿练习 · 完整对话重建（幂等 CLI）。

    python -m tools.recite_dialog                 # 全量：所有 interview/reference 条目
    python -m tools.recite_dialog --item iv:amazon-interview:a02
    python -m tools.recite_dialog --force         # 重切已存在的音频

背景（她的要求，8/31）：库里每条卡当初只切了她自己那段 ≥30s 的回答，面试官的追问和她对
追问的短答全被丢掉了 —— 而「他追问什么」恰恰是「哪里没讲清楚」的直接证据。本工具把每条
卡还原成完整往返：**本题提问 → 回答 → 全部 follow-up 往返 → 到下一题提问为止**。

数据源：`data/recite/source/real-interviews/`（8/29 scratchpad 的持久拷贝）里每场的
  *.turns.json    逐句转写（speaker/start/end/text）
  *.answers.json  每条回答的 turn_i（精确到 turn 下标）+ interviewee_speakers
  *.mp3           整场原声

窗口规则（确定性，不走 LLM）：
  起点 = 回答首 turn 往前收连续的非受访者 turn（就是提问；最多回收 4 turn / 180s）
  终点 = 下一条卡的窗口起点；末条或间隔过大时封顶在 回答结束 + TRAIL_CAP 秒并标 truncated
封顶不是偷懒：coding 场两条卡之间可能隔着 20 分钟写码，那不是本题的 follow-up。

产物：recite_items.dialog（json turns，sp: me/iv）+ data/recite/dialog/<item_id>.mp3。
跑完打印分布（时长分位、追问 turn 数、truncated 数）—— 生成类功能必须跑批验收，不许只看两条。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config as cfgmod          # noqa: E402
from app import recite                    # noqa: E402
from app.recite_import import _slugify    # noqa: E402

Q_BACK_TURNS = 4        # 提问回收：最多往前收几个非受访者 turn
Q_BACK_SEC = 180        # …以及最远往前收多少秒
TRAIL_CAP = 300         # 无下一条卡（或下一条太远）时，回答结束后最多再带多少秒


def _clean_speaker(s: str) -> str:
    return re.sub(r"\s*[（(].*$", "", (s or "").strip())


def load_interview(recap_path: Path):
    """recap.json → (doc, turns, answers_by_short, ivee_speakers, mp3_path)。缺件返回 None。"""
    stem = recap_path.name[: -len(".recap.json")]
    turns_p = recap_path.parent / f"{stem}.turns.json"
    ans_p = recap_path.parent / f"{stem}.answers.json"
    mp3_p = recap_path.parent / f"{stem}.mp3"
    if not (turns_p.exists() and mp3_p.exists()):
        return None
    doc = json.loads(recap_path.read_text(encoding="utf-8"))
    turns = json.loads(turns_p.read_text(encoding="utf-8"))["turns"]
    ans_by_short: dict[str, dict] = {}
    ivee: set[str] = set()
    if ans_p.exists():
        aj = json.loads(ans_p.read_text(encoding="utf-8"))
        ivee = {_clean_speaker(s) for s in (aj.get("interviewee_speakers") or [])}
        for a in aj.get("answers") or []:
            ans_by_short[str(a.get("id") or "")] = a
    return doc, turns, ans_by_short, ivee, mp3_p


def answer_turn_span(item: dict, ans: dict | None, turns: list[dict]) -> tuple[int, int]:
    """回答覆盖的 turn 下标区间 [lo, hi]。优先 answers.json 的 turn_i，缺了按时间重叠兜底。"""
    if ans and ans.get("turn_i"):
        ti = [i for i in ans["turn_i"] if 0 <= i < len(turns)]
        if ti:
            return min(ti), max(ti)
    oa = item.get("orig_audio") or {}
    s, e = oa.get("start", 0), oa.get("end", 0)
    hit = [i for i, t in enumerate(turns) if t["end"] > s and t["start"] < e]
    return (min(hit), max(hit)) if hit else (0, 0)


def question_start(turns: list[dict], lo: int, ans_start: int, ivee: set[str]) -> int:
    """从回答首 turn 往前收提问 run，返回窗口起点（秒）。"""
    j, taken = lo - 1, 0
    start = turns[lo]["start"] if lo < len(turns) else ans_start
    while (j >= 0 and taken < Q_BACK_TURNS
           and _clean_speaker(turns[j]["speaker"]) not in ivee
           and ans_start - turns[j]["start"] <= Q_BACK_SEC):
        start = turns[j]["start"]
        j -= 1
        taken += 1
    return min(start, ans_start)


def cut(mp3: Path, t0: int, t1: int, dst: Path, force: bool) -> bool:
    if dst.exists() and dst.stat().st_size > 0 and not force:
        return True
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", str(t0), "-t", str(max(1, t1 - t0)),
         "-i", str(mp3), "-ac", "1", "-b:a", "64k", str(dst)],
        capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  !! ffmpeg {dst.name}: {r.stderr.strip()[:140]}")
        return False
    return True


def build(conn, root: Path, only_item: str | None, force: bool) -> None:
    dial_dir = recite.audio_dir("dialog")
    stats = {"items": 0, "built": 0, "no_src": 0, "truncated": 0, "no_followup": 0}
    durs: list[int] = []
    fu_counts: list[int] = []

    for f in sorted(root.rglob("*.recap.json")):
        loaded = load_interview(f)
        if loaded is None:
            print(f"  ~ 缺 turns/mp3，跳过 {f.name}")
            continue
        doc, turns, ans_by_short, ivee, mp3 = loaded
        slug = _slugify(doc.get("interview") or f.stem.replace(".recap", ""))
        items = [it for it in (doc.get("items") or []) if it.get("orig_audio")]
        items.sort(key=lambda it: it["orig_audio"].get("start", 0))
        if not ivee:  # 兜底：回答 turn 的说话人就是受访者
            for it in items:
                a = ans_by_short.get(str(it.get("id") or "").split("-")[-1])
                lo, hi = answer_turn_span(it, a, turns)
                for i in range(lo, hi + 1):
                    ivee.add(_clean_speaker(turns[i]["speaker"]))

        # 先算每条的窗口起点，终点才有着落
        spans = []
        for it in items:
            short = str(it.get("id") or "").split("-")[-1]
            a = ans_by_short.get(short)
            lo, hi = answer_turn_span(it, a, turns)
            oa = it["orig_audio"]
            q0 = question_start(turns, lo, oa.get("start", turns[lo]["start"]), ivee)
            spans.append((short, it, q0, oa.get("end", turns[hi]["end"])))

        for k, (short, it, q0, ans_end) in enumerate(spans):
            item_id = f"iv:{slug}:{short}"
            if only_item and item_id != only_item:
                continue
            row = conn.execute("SELECT 1 FROM recite_items WHERE item_id=?",
                               (item_id,)).fetchone()
            if row is None:
                continue  # 没进库的条目（script null 且未参考导入）不建
            stats["items"] += 1
            raw_end = spans[k + 1][2] if k + 1 < len(spans) else turns[-1]["end"]
            t1 = min(raw_end, ans_end + TRAIL_CAP)
            truncated = raw_end > ans_end + TRAIL_CAP
            t1 = max(t1, ans_end)  # 窗口至少盖完回答本身
            dlg = [{"i": i, "sp": "me" if _clean_speaker(t["speaker"]) in ivee else "iv",
                    "who": t["speaker"], "s": t["start"], "e": t["end"], "x": t["text"]}
                   for i, t in enumerate(turns) if t["end"] > q0 and t["start"] < t1]
            n_fu = sum(1 for d in dlg if d["sp"] == "iv" and d["s"] >= ans_end)
            if n_fu == 0:
                stats["no_followup"] += 1
            fu_counts.append(n_fu)
            durs.append(t1 - q0)
            stats["truncated"] += int(truncated)

            dst = dial_dir / f"{item_id}.mp3"
            ok = cut(mp3, q0, t1, dst, force)
            conn.execute(
                "UPDATE recite_items SET dialog=?, dialog_audio=? WHERE item_id=?",
                (json.dumps({"t0": q0, "t1": t1, "truncated": truncated, "turns": dlg},
                            ensure_ascii=False),
                 dst.name if ok else "", item_id))
            stats["built"] += 1
    conn.commit()

    durs.sort()
    fu_counts.sort()

    def pct(xs, p):
        return xs[int(len(xs) * p)] if xs else 0

    print(f"\n== 验收分布 ==")
    print(f"条目 {stats['items']}，建成 {stats['built']}，truncated {stats['truncated']}，"
          f"回答后无面试官追问 {stats['no_followup']}")
    print(f"窗口时长 p25/p50/p90/max: {pct(durs,.25)}/{pct(durs,.5)}/{pct(durs,.9)}/"
          f"{durs[-1] if durs else 0}s")
    print(f"追问 turn 数 p50/p90/max: {pct(fu_counts,.5)}/{pct(fu_counts,.9)}/"
          f"{fu_counts[-1] if fu_counts else 0}")
    if stats["built"] < stats["items"]:
        print("⚠️ 有条目没建成，往上翻错误行")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None, help="real-interviews 源目录")
    ap.add_argument("--item", default=None, help="只处理这一条 item_id")
    ap.add_argument("--force", action="store_true", help="重切已存在的音频")
    a = ap.parse_args()
    root = Path(a.root) if a.root else recite.data_dir() / "source" / "real-interviews"
    if not root.exists():
        sys.exit(f"源目录不存在：{root}")
    cfg = cfgmod.load_config()
    conn = recite.connect(cfg)
    build(conn, root, a.item, a.force)


if __name__ == "__main__":
    main()
