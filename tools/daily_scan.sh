#!/bin/bash
# Daily ATS scan, with résumé tailoring chained straight onto it.
#
# Why a wrapper: launchd used to run `python -m app.scan` at 09:00 and auto_tailor.sh at 09:45,
# with nothing connecting them — 45 minutes was just a guess at how long a scan takes. Anything
# that entered the queue outside that window (an ad-hoc ingest, probe_ats onboarding new
# companies, a scan that ran long) waited until the NEXT morning for a résumé. Chaining makes
# the dependency real: tailoring starts when the scan has actually finished.
#
# auto_tailor.sh holds its own lock, so this is safe to run alongside the scheduled 09:45 job
# and alongside linkedin_nightly.sh, which chains it the same way.
set -euo pipefail
cd /path/to/interview/workspace/jobscan
mkdir -p data/logs
LOG=data/logs/daily_scan.log

echo "===== $(date) daily scan start =====" >>"$LOG"
if .venv/bin/python -m app.scan >>"$LOG" 2>&1; then
    echo "$(date) scan ok — tailoring" >>"$LOG"
else
    # Still tailor: a partial scan usually means one ATS source was down, and the roles that
    # DID land should not be held hostage to it.
    echo "$(date) !! scan exited non-zero — tailoring what landed anyway" >>"$LOG"
fi
bash tools/auto_tailor.sh || echo "$(date) !! auto_tailor failed" >>"$LOG"
# Other workspaces (owner 2026-09-23): every config.<name>.yaml gets its own tailoring pass AFTER
# the owner's — sequential on purpose (one Max subscription, never 8 claude sessions at once).
# Scanning stays shared: one jobs.db for everyone.
for WSCFG in config.*.yaml; do
    [ -f "$WSCFG" ] || continue
    WSNAME="${WSCFG#config.}"; WSNAME="${WSNAME%.yaml}"
    case "$WSNAME" in example|*.bak*|yaml) continue ;; esac
    echo "$(date) auto_tailor for workspace $WSNAME" >>"$LOG"
    JOBSCAN_CONFIG="$WSCFG" JOBSCAN_WS="$WSNAME" bash tools/auto_tailor.sh \
        || echo "$(date) !! auto_tailor($WSNAME) failed" >>"$LOG"
done
echo "===== $(date) daily scan done =====" >>"$LOG"
# 鸭先知每日快照（2026-09-09）：把当天的主题份额/挂龄/新词落成 JSON，攒趋势历史。
# 纯本地统计几秒钟，失败不拦路。
.venv/bin/python -m app.trends --snapshot >>"$LOG" 2>&1 || echo "$(date) !! trends snapshot failed" >>"$LOG"

# 每周日死链大扫除（owner 2026-09-16 拍板：并进扫描任务，不另立 launchd 单元）。
# 跟在当天扫描后面跑是刻意的——prune_delisted 的判据就是「今天扫完还在不在」，此刻最新鲜。
# 两步都只删「确定死了」的岗位（板子上消失≥2天 / 链接明确 404 或写着已关闭），删前全量
# 备份到 data/purged_*_backup.json；拿不准的（机器人墙/登录墙）一律不动。放在最后是因为
# 逐条点链接要一两个小时，别挡住上面几步的产出。hb_run 打卡名 deadlinks，/health 有灯。
if [ "$(date +%u)" = "7" ]; then
    echo "$(date) weekly dead-link sweep start" >>"$LOG"
    bash tools/hb_run.sh deadlinks 192 10800 -- bash -c \
        '.venv/bin/python tools/prune_delisted.py --apply --min-lag-days 2 && .venv/bin/python tools/check_dead_links.py --apply --delay 2' \
        >>"$LOG" 2>&1 || echo "$(date) !! weekly dead-link sweep failed" >>"$LOG"
    echo "$(date) weekly dead-link sweep done" >>"$LOG"
fi
