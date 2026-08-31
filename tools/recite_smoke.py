#!/usr/bin/env python3
"""念稿练习 · 最小 smoke：把整条链真跑一遍（含 ASR 和 LLM 判官）。

    python -m tools.recite_smoke                 # 全链，真调 Parakeet + Anthropic
    python -m tools.recite_smoke --no-llm        # 只跑到转写，不烧 token
    python -m tools.recite_smoke --item op:01

拿这条稿自己的**范读 mp3** 当"她的录音"提交上去 —— 它是一段真实的、内容已知的语音，所以
转写、时长、wpm、逐字重合、覆盖判定全都会走真实路径，而且 r 应该接近 1.0（范读念的就是
原稿），这本身就是一次对 verbatim_ratio 的自检。

跑完打印每一环的产出，任何一环空了都会明说是哪一环空的 —— 这个链有五个外部依赖
（ffmpeg / ffprobe / say / Parakeet 子进程 / Anthropic），静默降级会让人查半天。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient   # noqa: E402

from app import config as cfgmod            # noqa: E402
from app import recite                      # noqa: E402
from app import web                         # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.recite_smoke")
    ap.add_argument("--item", default="")
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args(argv)

    cfg = cfgmod.load_config()
    if a.no_llm:
        cfg.setdefault("ai", {})["enabled"] = False
        web.cfg["ai"] = dict(web.cfg.get("ai") or {}, enabled=False)
    conn = recite.connect(cfg)

    item_id = a.item
    if not item_id:
        q = recite.today_queue(conn, cfg)
        if not q:
            print("✗ 队列是空的 —— 先跑 python -m app.recite_import <dir>")
            return 1
        for r in q:
            if (recite.audio_dir("ref") / f"{r['item_id']}.mp3").exists():
                item_id = r["item_id"]
                break
        item_id = item_id or q[0]["item_id"]
    ref = recite.audio_dir("ref") / f"{item_id}.mp3"
    print(f"■ 条目 {item_id}")
    if not ref.exists():
        print(f"✗ 没有范读 {ref} —— 先跑 python -m tools.recite_tts --only-missing --item {item_id}")
        return 1

    c = TestClient(web.app)
    for url in ("/recite", f"/recite/{item_id}"):
        r = c.get(url)
        print(f"  GET {url} → {r.status_code} ({len(r.text)} bytes)")
        if r.status_code != 200:
            print(r.text[:600])
            return 1
    r = c.get(f"/recite/audio/{item_id}/ref")
    print(f"  GET 范读音频 → {r.status_code} ({len(r.content)} bytes)")

    print("■ 提交一次 take（用范读当录音，真走 ffprobe → Parakeet → 判官）…")
    with ref.open("rb") as f:
        r = c.post(f"/api/recite/{item_id}/take",
                   files={"audio": ("take.mp3", f, "audio/mpeg")},
                   data={"why_line": "因为规则散在三个地方会漂移，状态模型只有一处真相。",
                         "mode": "recall", "variant_type": "original",
                         "variant_sec": "60", "first_exposure": "0"})
    if r.status_code != 200:
        print(f"✗ POST take → {r.status_code}\n{r.text[:800]}")
        return 1
    d = r.json()
    print(json.dumps({k: v for k, v in d.items()
                      if k in ("take_id", "verdict", "total", "coverage", "passed",
                               "hard_ok", "gate_note", "verbatim_ratio", "r_prev",
                               "judge_error")}, ensure_ascii=False, indent=2))
    m = d.get("metrics") or {}
    print(f"  ASR 引擎 {m.get('asr_engine') or '(空)'} · 时长 {m.get('duration')}s · "
          f"wpm {m.get('wpm')} · 停顿>2s {m.get('pauses_over_2s')}")
    print(f"  转写前 160 字：{(d.get('transcript') or '(空)')[:160]}")
    print(f"  没说到：{d.get('missed_cues')}")
    print(f"  fix_next：{d.get('fix_next') or '(空)'}")
    print(f"  one_line：{d.get('one_line') or '(空)'}")
    print(f"  事实核对：{d.get('fact_check')}")
    print(f"  新状态：{d.get('state')}")

    bad = []
    if not (d.get("transcript") or "").strip():
        bad.append(f"转写是空的（{m.get('asr_error') or '没有错误信息'}）")
    if not a.no_llm and not (d.get("scores") or {}):
        bad.append(f"判官没给分（{d.get('judge_error')}）")
    row = conn.execute("SELECT COUNT(*) n FROM recite_takes WHERE why_line!=''").fetchone()
    if not row["n"]:
        bad.append("recite_takes 里没有 why_line 非空的行")
    if not list(recite.audio_dir("takes").glob("*")):
        bad.append("data/recite/takes/ 是空的")
    print("\n" + ("✅ smoke 全链通过" if not bad else "🔴 " + " · ".join(bad)))
    conn.close()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
