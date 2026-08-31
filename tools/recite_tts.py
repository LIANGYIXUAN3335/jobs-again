#!/usr/bin/env python3
"""念稿练习 · 范读生成（幂等 CLI）。

    python -m tools.recite_tts --only-missing
    python -m tools.recite_tts --item iv:amazon-interview-3:a02 --force

`say -v Samantha -r <rate>` → aiff → ffmpeg → `data/recite/ref/<item_id>.mp3`。

**这个音轨的定位写死在标签里：语速参考（机器音 · 只对节奏，不学语调）。**
Samantha 作为 pacing anchor 完全够用（稳定、可变速、无情绪噪声），作为 prosody model 有污染
风险 —— 语调的范本本来就该是她自己的原声，而那条音轨也在页面上。

语速与 `target_sec` 同源：**成品的时长就是 target_sec**（见下面 SAY_RATE 处的实测和做法）。
config 的 `recite.tts_rate`（120）是页面标签上写的那个墙钟 wpm，也是 target_sec 的推导常数。
不这么做的话范读会跑在 ~160 wpm 上，等于给她示范一个比目标更快的节奏 —— 而她的病正是赶。

**失败显示「[重试]」，不静默隐藏**：一个悄悄消失的范读会让她以为这条稿没有范读，
而不是以为生成挂了。所以失败的条目会打印出来并写进 data/recite/tts_failed.md。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as cfgmod          # noqa: E402
from app import recite                    # noqa: E402

# 逐字跨度标记只是给前端高亮用的，别念出来。
_STRIP = re.compile(r"[«»]")

# `say -r N` 的 N **不是**墙钟 wpm，而且不是线性的。这批稿上实测（Samantha）：
#   -r 120 → 154 / 160 / 166 wpm（中位 160）
#   -r  90 → 141 / 147 / 152 wpm（中位 147）
# 30 个 rate 单位只换来 13 wpm，外推到 120 wpm 要 -r ≈ 28，那已经不是一个能听的嗓音了。
#
# 所以不去调 `-r`，改成**按目标时长收尾**：合成完量一次实际时长，用 ffmpeg 的 atempo
# （变速不变调）把它拉到 `target_sec`。这个音轨的职责就是示范目标节奏，让它的长度**就是**
# 目标长度是这个职责最诚实的实现 —— 她按着它念完，计时器正好停在目标线上。
# atempo 单次只吃 0.5–2.0，超出就串联。
SAY_RATE = 120


def _atempo_chain(f: float) -> str:
    parts = []
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    parts.append(f"atempo={f:.4f}")
    return ",".join(parts)


def _dur(p: Path) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(p)],
                           capture_output=True, text=True, timeout=60)
        return float((r.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def synth(text: str, dst: Path, voice: str, target_sec: int = 0) -> tuple[bool, str]:
    """合成一条范读。时长由 atempo 收到 `target_sec`，所以 `say -r` 取一个好听的固定值即可。"""
    text = _STRIP.sub("", text or "").strip()
    rate = SAY_RATE
    if not text:
        return False, "空稿"
    with tempfile.TemporaryDirectory(prefix="recite-tts-") as td:
        aiff = Path(td) / "s.aiff"
        try:
            r = subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", str(aiff), text],
                               capture_output=True, text=True, timeout=300)
        except Exception as e:
            return False, f"say 起不来：{e}"
        if r.returncode != 0 or not aiff.exists():
            # 退路 ①：换默认嗓（这台机器上可能没装 Samantha）
            r = subprocess.run(["say", "-r", str(rate), "-o", str(aiff), text],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0 or not aiff.exists():
                return False, (r.stderr or "say 失败")[:200]
        dst.parent.mkdir(parents=True, exist_ok=True)
        af = []
        raw = _dur(aiff)
        if target_sec and raw > 1:
            f = raw / float(target_sec)
            if abs(f - 1.0) > 0.03:          # 3% 以内不折腾，避免无谓的重采样
                af = ["-filter:a", _atempo_chain(f)]
        try:
            c = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(aiff),
                 *af, "-c:a", "libmp3lame", "-q:a", "5", str(dst)],
                capture_output=True, text=True, timeout=300)
        except Exception as e:
            return False, f"ffmpeg 起不来：{e}"
        if c.returncode != 0 or not dst.exists():
            return False, (c.stderr or "ffmpeg 失败")[:200]
    return True, ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.recite_tts")
    ap.add_argument("--only-missing", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--item", action="append", default=[])
    ap.add_argument("--limit", type=int, default=0)
    # 默认只给会进队列的条目生成范读：playbook 粗导入的 86 条是 deferred + rough，
    # 给它们跑 TTS 就是花十几分钟合成一批没人会听的音频。
    ap.add_argument("--include-deferred", action="store_true")
    a = ap.parse_args(argv)

    cfg = cfgmod.load_config()
    c = recite.cfg_recite(cfg)
    conn = recite.connect(cfg)
    sql = "SELECT item_id, script, target_sec FROM recite_items WHERE script!=''"
    if not a.include_deferred and not a.item:
        sql += " AND COALESCE(deferred,0)=0 AND COALESCE(quality,'ok')='ok'"
    args: list = []
    if a.item:
        sql += " AND item_id IN (%s)" % ",".join("?" * len(a.item))
        args += a.item
    rows = list(conn.execute(sql + " ORDER BY item_id", args))
    if a.limit:
        rows = rows[:a.limit]

    out = recite.audio_dir("ref")
    ok = skip = 0
    failed: list[tuple[str, str]] = []
    for r in rows:
        dst = out / f"{r['item_id']}.mp3"
        if dst.exists() and dst.stat().st_size > 0 and not a.force:
            skip += 1
            continue
        good, err = synth(r["script"], dst, c["tts_voice"], int(r["target_sec"] or 0))
        if good:
            ok += 1
        else:
            failed.append((r["item_id"], err))
            print(f"  ✗ {r['item_id']}: {err}")
    conn.close()

    print(f"\n  范读：新生成 {ok} · 已存在跳过 {skip} · 失败 {len(failed)} "
          f"（{c['tts_voice']} @ {c['tts_rate']} wpm）")
    fp = recite.data_dir() / "tts_failed.md"
    if failed:
        fp.write_text("# 范读生成失败（页面上显示 [重试]，不静默隐藏）\n\n"
                      + "\n".join(f"- `{i}` — {e}" for i, e in failed) + "\n",
                      encoding="utf-8")
        print(f"  🔴 红灯：{len(failed)} 条没有范读 → {fp}")
        return 1
    if fp.exists():
        fp.unlink()
    if rows and ok + skip < len(rows):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
