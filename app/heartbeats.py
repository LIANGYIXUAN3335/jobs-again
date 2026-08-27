"""定时任务的体检数据：读 hb_run.sh 留下的打卡卡片，判绿灯红灯。

每个 launchd 任务经 tools/hb_run.sh 启动，干完活在 ~/.local/state/heartbeats/<name>.json
留一张卡片（几点干完、exit code、花了多久、多久没打卡算失联）。这里只读卡片、算状态：

  ok     按时打卡且上次成功
  fail   上次运行 exit != 0
  stale  超过 stale_hours 没有新卡片（任务失联：launchd 掉了 / 机器一直睡 / 脚本挂死）
  never  注册过但一张卡片都没有（新任务还没到点，灰色，不算红灯）

/health 页面画表格；tools/triage.py 拿 red() 弹通知。跨项目零耦合：谁都往同一个
文件夹打卡，jobscan 只是读的人。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

HB_DIR = Path.home() / ".local" / "state" / "heartbeats"

# 注册表：让 /health 能显示"还没打过卡"的任务，而不是等出了事才知道它存在。
# (打卡名, 中文名, 节奏说明)
REGISTRY = [
    ("linkedin", "LinkedIn 夜航", "每天 08:20"),
    ("ideas", "点子雷达", "每天 08:30"),
    ("maillabel", "邮件贴标签", "08:40 起每 2 小时"),
    ("scan", "抓岗位→做简历（链式）", "每天 09:00"),
    ("autotailor", "做简历（兜底）", "每天 09:45"),
    ("triage", "值班员", "每天 9/11/13/15/17 点"),
    ("outcomes", "记账员（邮箱→结果）", "每天 11:00 / 16:00"),
    ("followup", "跟进提醒（投了没回音）", "每周一/四 09:20"),
    ("bqautoprep", "BQ 自动备考", "每天 9/11/13/15/17 点"),
    ("atoms-backfill", "atoms 卡片质检（另一项目）", "每周日 02:30"),
]
_NAMES = {n for n, _, _ in REGISTRY}


def read() -> list[dict]:
    """所有任务的当前状态，注册表顺序在前，没注册但打了卡的排最后。"""
    now = time.time()
    cards: dict[str, dict] = {}
    if HB_DIR.is_dir():
        for f in HB_DIR.glob("*.json"):
            if f.name.startswith("."):        # .alert_state.json / hb_run 的临时文件不是卡片
                continue
            try:
                card = json.loads(f.read_text())
                cards[f.stem] = card if isinstance(card, dict) else {"corrupt": True}
            except Exception:
                cards[f.stem] = {"name": f.stem, "corrupt": True}

    rows = []
    for name, zh, cadence in REGISTRY:
        rows.append(_status(name, zh, cadence, cards.pop(name, None), now))
    for name, card in sorted(cards.items()):          # 没在注册表里的照样显示
        rows.append(_status(name, name, "", card, now))
    return rows


def _status(name: str, zh: str, cadence: str, card: dict | None, now: float) -> dict:
    row = {"name": name, "zh": zh, "cadence": cadence, "status": "never",
           "detail": "还没打过卡", "finished": "", "duration_s": None, "age_h": None}
    if not card:
        return row
    if card.get("corrupt"):
        row.update(status="fail", detail="打卡文件损坏")
        return row
    age_h = (now - card.get("finished_epoch", 0)) / 3600
    row.update(finished=card.get("finished", ""), duration_s=card.get("duration_s"),
               age_h=round(age_h, 1))
    stale_h = float(card.get("stale_hours") or 26)
    if card.get("exit", 0) != 0:
        row.update(status="fail", detail=f"上次运行失败（exit {card['exit']}）")
    elif age_h > stale_h:
        row.update(status="stale", detail=f"失联 {age_h:.0f} 小时（超过 {stale_h:.0f}h 没打卡）")
    else:
        row.update(status="ok", detail="正常")
    return row


def red(rows: list[dict] | None = None) -> list[dict]:
    """亮红灯的任务（fail / stale）。never 不算——新任务没到点不是事故。"""
    return [r for r in (rows if rows is not None else read()) if r["status"] in ("fail", "stale")]


def alert_new_reds(rows: list[dict] | None = None) -> list[dict]:
    """只在状态翻转（好→坏）时返回该任务一次，供 triage 弹通知用，避免每小时重复轰炸。
    上次已知的红灯名单存在打卡文件夹里的 .alert_state.json。恢复正常自动出名单。"""
    reds = red(rows)
    state_f = HB_DIR / ".alert_state.json"
    try:
        known = set(json.loads(state_f.read_text()))
    except Exception:
        known = set()
    fresh = [r for r in reds if r["name"] not in known]
    try:
        HB_DIR.mkdir(parents=True, exist_ok=True)
        tmp = HB_DIR / ".alert_state.json.tmp"
        tmp.write_text(json.dumps(sorted(r["name"] for r in reds)))
        tmp.replace(state_f)
    except Exception:
        pass
    return fresh
