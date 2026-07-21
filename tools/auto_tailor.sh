#!/bin/bash
# Daily auto-tailor: prep (no cost) → headless Claude Code on the MAX SUBSCRIPTION tailors the
# day's new qualifying roles. Run by launchd after the scan. Uses the subscription because the
# machine's Claude Code is OAuth-logged-in and no ANTHROPIC_API_KEY is in the environment — we
# also unset it defensively so this NEVER falls back to pay-per-token API billing.
set -euo pipefail
cd /path/to/interview/workspace/jobscan
mkdir -p data/logs
LOG=data/logs/autotailor.log
CLAUDE="$HOME/.local/bin/claude"
echo "===== $(date) auto_tailor start =====" >>"$LOG"

# 1. prep — pick guardrail-passing roles, write prompts + manifest (pure Python, no LLM)
.venv/bin/python tools/prep_autotailor.py >>"$LOG" 2>&1

# 2. anything to do? (manifest is "[]" when empty)
COUNT=$(.venv/bin/python -c "import json;print(len(json.load(open('scratch_priors/autotailor_manifest.json'))))" 2>/dev/null || echo 0)
if [ "$COUNT" -eq 0 ]; then
  echo "nothing to tailor today" >>"$LOG"; exit 0
fi

# 3. headless Claude Code (subscription) works the batch. env -u strips any inherited API key.
#    --allowedTools scopes it to exactly what tailoring needs (no full permission bypass):
#    read prompts, write YAMLs, run the venv python (ingest+score). If a tool ever gets
#    blocked, widen this list or switch to --dangerously-skip-permissions.
echo "tailoring $COUNT roles via headless Claude Code (subscription)…" >>"$LOG"
env -u ANTHROPIC_API_KEY "$CLAUDE" -p "$(cat tools/autotailor_prompt.md)" \
    --allowedTools "Read" "Write" "Edit" "Bash(.venv/bin/python:*)" >>"$LOG" 2>&1 \
    || echo "claude run exited non-zero" >>"$LOG"
echo "===== $(date) auto_tailor done =====" >>"$LOG"
