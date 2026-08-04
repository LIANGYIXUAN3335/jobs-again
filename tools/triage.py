"""Scheduled inbox triage entrypoint (run by launchd every 30 min, 8am-10pm).
  1. send_due(): fire any ARMED auto-replies whose 30-min undo window elapsed
  2. triage(): scan for new emails needing a reply, draft + queue (+ arm low-risk ones)
  3. macOS notification if there's anything new to look at
Read-only on the mailbox except starring; sends only through the armed/undo-window path.
"""
import subprocess
import sys

sys.path.insert(0, ".")
from app import config as cfgmod, heartbeats, inbox, store  # noqa: E402


def notify(title: str, message: str):
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}" sound name "Ping"'],
            check=False, timeout=10)
    except Exception:
        pass


def main():
    cfg = cfgmod.load_config()

    # 体检巡查：其他定时任务谁失联/谁上次失败。只对"新出现的"红灯叫一次（好→坏翻转），
    # 恢复后再坏会再叫；红灯明细看 /health。放在最前面——就算 inbox 关了也要巡查。
    reds = heartbeats.alert_new_reds()
    if reds:
        names = "、".join(r["zh"] for r in reds[:4])
        print(f"heartbeat red: {[r['name'] for r in reds]}")
        notify("JobScan · 体检红灯", f"{names} — 打开 JobScan → 体检表看详情")

    if not (cfg.get("inbox") or {}).get("enabled"):
        print("inbox triage disabled in config")
        return
    sent = inbox.send_due(cfg)                      # send anything past its undo window first
    for s in sent:
        print(f"auto-sent → {s['to']}: {s['subject']}")
    summary = inbox.triage(cfg)
    print(f"triage: {summary}")

    conn = store.connect_user(cfg)
    pending = store.queue_count_pending(conn)
    bits = []
    if summary["armed"]:
        bits.append(f"{summary['armed']} auto-reply queued (30-min undo)")
    if pending:
        bits.append(f"{pending} awaiting your OK")
    if sent:
        bits.append(f"{len(sent)} sent")
    if bits:
        notify("JobScan · inbox", " · ".join(bits) + " — open JobScan → Replies")


if __name__ == "__main__":
    main()
