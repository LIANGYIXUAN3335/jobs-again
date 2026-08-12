#!/bin/bash
# hb_run.sh — 定时任务的"打卡器"。launchd 任务经它启动：先干活，干完在
# ~/.local/state/heartbeats/<name>.json 留一张卡片（几点干完、成功没、花了多久）。
# /health 页面读这些卡片画绿灯红灯；triage.py 发现红灯弹通知。
#
# 用法: hb_run.sh <name> <stale_hours> -- <command...>
#   <stale_hours>: 超过这么多小时没有新卡片就算"失联"（红灯）。
set -u
NAME="$1"; STALE_H="$2"; shift 2
[ "${1:-}" = "--" ] && shift
DIR="$HOME/.local/state/heartbeats"
mkdir -p "$DIR"
START=$(date +%s)
"$@"
CODE=$?
END=$(date +%s)
TMP="$DIR/.$NAME.tmp"
printf '{"name":"%s","stale_hours":%s,"finished":"%s","finished_epoch":%d,"exit":%d,"duration_s":%d}\n' \
  "$NAME" "$STALE_H" "$(date '+%Y-%m-%d %H:%M:%S')" "$END" "$CODE" "$((END-START))" > "$TMP" && mv -f "$TMP" "$DIR/$NAME.json"
exit $CODE
