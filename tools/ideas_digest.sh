#!/bin/bash
# Daily ideas run: fetch (no cost) → 晨报 digest via headless Claude Code on the MAX
# SUBSCRIPTION (same pattern as auto_tailor.sh: OAuth login, env -u strips any inherited
# API key so this NEVER falls back to pay-per-token billing). Model pinned to sonnet
# per the 模型分层 rule (content generation never runs on a bigger tier).
set -euo pipefail
cd /path/to/interview/workspace/jobscan
mkdir -p data/logs
LOG=data/logs/ideas.log
CLAUDE="$HOME/.local/bin/claude"
echo "===== $(date) ideas fetch+digest start =====" >>"$LOG"

# 1. fetch all sources into ideas.db (pure Python)
.venv/bin/python -m app.ideas >>"$LOG" 2>&1

# 2. prep the digest prompt; exit 3 = nothing to digest today, stop cleanly
rc=0
.venv/bin/python tools/ideas_digest.py prep >>"$LOG" 2>&1 || rc=$?
if [ "$rc" -ne 0 ]; then
  echo "digest prep rc=$rc — skipping LLM step" >>"$LOG"; exit 0
fi

# 3. headless Claude Code (subscription, sonnet) writes the digest JSON
env -u ANTHROPIC_API_KEY "$CLAUDE" -p "$(cat scratch_priors/ideas_digest_prompt.md)" \
    --model sonnet --allowedTools "Read" "Write" >>"$LOG" 2>&1 \
    || echo "claude digest run exited non-zero" >>"$LOG"

# 4. validate + save into ideas.db (url whitelist, structure checks)
.venv/bin/python tools/ideas_digest.py save >>"$LOG" 2>&1 \
    || echo "digest save failed — page falls back to raw list" >>"$LOG"
echo "===== $(date) ideas fetch+digest done =====" >>"$LOG"
