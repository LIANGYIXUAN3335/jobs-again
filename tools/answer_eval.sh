#!/bin/bash
# Nightly autofill-answer quality eval (see tools/answer_eval.py).
#
# Runs at 21:30 — after the day's applying is done — so the report covers everything the
# extension filled today. Wrapped in hb_run.sh so /health shows a light and triage.py
# alerts if it goes stale (>26h with no finish card).
set -uo pipefail
cd /path/to/interview/workspace/jobscan
mkdir -p data/logs
LOG=data/logs/answer_eval.log

echo "===== $(date) answer eval start =====" >>"$LOG"
bash tools/hb_run.sh answer_eval 26 -- .venv/bin/python -m tools.answer_eval >>"$LOG" 2>&1 \
  || echo "$(date) !! answer_eval failed" >>"$LOG"
echo "===== $(date) answer eval done =====" >>"$LOG"
