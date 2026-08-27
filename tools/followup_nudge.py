#!/usr/bin/env python3
"""跟进提醒：把「投了没回音」的名单推到她眼前。只提醒，永远不发信。

`app/followup.py` 早就能算出哪些申请安静了，`/followups` 页面也早就在那儿 —— 但
followups 表在 2026-08-10 之前一行都没有：826 份申请、9 个回音、0 封跟进。功能不是坏了，
是没人告诉她它存在。这个脚本补的就是这一环，和值班员(tools/triage.py)、点子雷达
(tools/ideas_digest.py) 一样：定时跑一次，有事弹 Mac 通知，顺手打个卡给 /health 看。

刻意不做的事（app/followup.py 的设计合同，不许破）：
  - 不起草。起草要花 LLM 的钱，而且草稿只有在她真要发的时候才有意义 —— 那一步在 /followups
    页面上点「draft」。这里只数数、只报名字。
  - 不发送。这个文件里没有任何东西 import mail 的发送路径。定时器发出去的跟进信是另一个、
    更糟的产品。她自己看、自己发、发完自己点「I sent this」——那一下才是两次上限的依据。

不天天叫（2026-08-10 定的节奏）：每周一、周四各一次。同一份名单反复弹通知，三天就会被她
学会忽略，那和没有通知是一回事。所以只在「有新面孔进榜」时叫；名单一直没动、她也一直没动
的话，隔 remind_days 天温和地再提一次，此外一律只写日志。上次报过谁记在
data/followup_nudge_state.json。

    python tools/followup_nudge.py              # 定时任务走这条
    python tools/followup_nudge.py --dry-run    # 只算不叫、不写状态
    python tools/followup_nudge.py --force      # 无视静默规则，强制弹一次
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfgmod, followup, store  # noqa: E402

STATE = "data/followup_nudge_state.json"
# 名单没变化时，隔这么多天再温和提醒一次。两周 = 她大概率已经忘了这页存在，但又不至于烦。
REMIND_DAYS = 14
TOP_N = 3                       # 通知里点名几个 —— 多了读不完，等于没点名


def notify(title: str, message: str) -> None:
    """Mac 通知，和 tools/triage.py 同一个写法（osascript + Ping）。"""
    # 公司名/职位名是抓来的，可能带引号 —— 不转义会把 osascript 的字符串截断。
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{esc(message)}" with title "{esc(title)}" sound name "Ping"'],
            check=False, timeout=10)
    except Exception:
        pass


def load_state(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}                       # 状态文件丢了/坏了不是事故，最多多叫一次


def save_state(path: Path, seen: list[str], notified_on: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"seen": sorted(seen), "last_notified": notified_on},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)                   # 原子替换，跑一半被 launchd 杀掉也不会留半个文件


def followups_url(cfg: dict) -> str:
    ui = cfg.get("ui") or {}
    return f"http://{ui.get('host', '127.0.0.1')}:{ui.get('port', 8787)}/followups"


def headline(rows: list[dict], n: int = TOP_N) -> str:
    """点名前 n 个：公司 + 分数 + 安静了多久。「你有待办事项」这种通知没人会点。

    一家公司只点一次 —— Nvidia 连着占掉三个名额，通知就只剩一条信息了。
    """
    out, seen = [], set()
    for r in rows:
        k = cfgmod.norm_key(r["company"])
        if k in seen:
            continue
        seen.add(k)
        out.append(f"{r['company']} {r['weighted_score'] or 0:.0f}分/{r['days_quiet']}天")
        if len(out) >= n:
            break
    return " · ".join(out)


def should_notify(fresh: list[dict], rows: list[dict], state: dict, today: str) -> tuple[bool, str]:
    """(要不要弹, 为什么)。静默是默认，弹通知要有理由。"""
    if not rows:
        return False, "名单是空的"
    if fresh:
        return True, f"{len(fresh)} 个新面孔进榜"
    last = state.get("last_notified") or ""
    try:
        gap = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(last)).days
    except ValueError:
        return True, "没有上次提醒的记录"
    if gap >= REMIND_DAYS:
        return True, f"名单没变但已经 {gap} 天没提过了"
    return False, f"名单没变，距上次提醒才 {gap} 天 —— 不吵她"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="只算不叫、不写状态")
    ap.add_argument("--force", action="store_true", help="无视静默规则强制弹一次")
    args = ap.parse_args(argv)

    cfg = cfgmod.load_config()
    today = store.la_today()
    conn = store.connect_view(cfg)
    try:
        rows = followup.quiet(cfg, conn)
        tune = followup.tuning(cfg)
    finally:
        conn.close()

    state_path = cfgmod.resolve_path(STATE)
    state = load_state(state_path)
    seen_before = set(state.get("seen") or [])
    ids = [r["job_id"] for r in rows]
    fresh = [r for r in rows if r["job_id"] not in seen_before]

    print(f"===== {today} 跟进提醒 =====")
    print(f"安静 {tune['after_days']} 天以上：{len(rows)} 个"
          f"（其中 {len(fresh)} 个是上次之后新进榜的；每家最多 {tune['per_company']} 个，"
          f"每个申请最多 {tune['max_per_app']} 次跟进）")
    for r in rows:
        mark = "NEW " if r["job_id"] in {f["job_id"] for f in fresh} else "    "
        print(f"  {mark}{r['weighted_score'] or 0:5.0f} {r['days_quiet']:4d}d  "
              f"{r['company'][:16]:16} {r['title'][:44]}")

    go, why = should_notify(fresh, rows, state, today)
    if args.force and rows:
        go, why = True, "--force"
    print(f"通知：{'弹' if go else '不弹'} —— {why}")

    if go and not args.dry_run:
        # 新面孔优先点名（那才是「新消息」），没有新面孔就点名榜首。
        pick = fresh if fresh else rows
        # 第一次跑（还没有状态文件）时全部都算「新」，写成「25 新」是废话，不写。
        n_new = f"（{len(fresh)} 新）" if fresh and seen_before else ""
        notify("JobScan · 跟进",
               f"{len(rows)} 份申请没回音{n_new}：{headline(pick)} —— "
               f"去 {followups_url(cfg)} 挑几个，草稿在那儿现写")
    if not args.dry_run:
        save_state(state_path, ids, today if go else (state.get("last_notified") or ""))
    print(f"===== {today} 跟进提醒 done =====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
