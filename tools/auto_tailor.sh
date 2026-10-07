#!/bin/bash
# Daily auto-tailor: prep (no cost) → headless Claude Code on the MAX SUBSCRIPTION tailors the
# day's new qualifying roles. Run by launchd after the scan. Uses the subscription because the
# machine's Claude Code is OAuth-logged-in and no ANTHROPIC_API_KEY is in the environment — we
# also unset it defensively so this NEVER falls back to pay-per-token API billing.
set -euo pipefail
cd /path/to/interview/workspace/jobscan
mkdir -p data/logs
# Workspace (owner 2026-09-23): JOBSCAN_CONFIG picks the candidate's config for every python call
# below (the headless claude workers inherit it too); JOBSCAN_WS tags this run's log, lock and
# manifest so two candidates' batches never collide. Both unset = the owner's default workspace.
WS="${JOBSCAN_WS:-}"
SFX=""; [ -n "$WS" ] && SFX=".$WS"
LOG="data/logs/autotailor${SFX}.log"
# Manual batch override (owner 2026-09-23, review 页「✎ 一键 tailor 当前列表」): app/web.py
# POST /api/tailor-batch has ALREADY written a manifest for exactly the roles on screen, so
# step 1 (prep_autotailor.py, which rebuilds the manifest from the nightly policy) is skipped,
# and the run logs to its own .manual.log — the status endpoint tails it, and a manual batch
# must stay readable on its own instead of disappearing inside the 90 MB nightly log.
# Chunk files derive from $MANIFEST below, so they follow the override automatically.
if [ -n "${JOBSCAN_MANIFEST:-}" ]; then
    MANIFEST="$JOBSCAN_MANIFEST"
    LOG="data/logs/autotailor${SFX}.manual.log"
else
    MANIFEST=$(.venv/bin/python -c "import sys; sys.path.insert(0,'.'); from app import config as c; print(((c.load_config().get('auto_tailor') or {}).get('manifest')) or 'scratch_priors/autotailor_manifest.json')")
fi
CLAUDE="$HOME/.local/bin/claude"

# Single-run lock. This used to be safe by construction — one launchd job, once a day. It is
# now also invoked at the END of the scan and LinkedIn pipelines (so roles get a résumé the
# moment they land, instead of waiting for the next 09:45), which means two runs can overlap.
# Two headless Claude sessions writing the same YAMLs would interleave garbage.
LOCK="data/logs/.autotailor${SFX}.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    echo "$(date) auto_tailor already running — exiting" >>"$LOG"; exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

echo "===== $(date) auto_tailor start =====" >>"$LOG"

# 1. prep — pick guardrail-passing roles, write prompts + manifest (pure Python, no LLM).
#    Skipped when the caller handed us the manifest (JOBSCAN_MANIFEST): prep would replace the
#    review page's hand-picked list with the nightly selection, which is not what was asked.
if [ -n "${JOBSCAN_MANIFEST:-}" ]; then
    echo "manifest supplied by caller: $MANIFEST — skipping prep" >>"$LOG"
else
    .venv/bin/python tools/prep_autotailor.py >>"$LOG" 2>&1
fi

# 2. anything to do? (manifest is "[]" when empty)
COUNT=$(.venv/bin/python -c "import json;print(len(json.load(open('$MANIFEST'))))" 2>/dev/null || echo 0)
if [ "$COUNT" -eq 0 ]; then
  echo "nothing to tailor today" >>"$LOG"; exit 0
fi

# Fail before fan-out when the subscription session is unavailable. Without this guard, every
# worker starts independently, writes the same OAuth error, and makes the real cause look like a
# 30-role processing failure. Parse the CLI's JSON status rather than matching human-readable text.
if ! "$CLAUDE" auth status 2>/dev/null \
    | .venv/bin/python -c 'import json, sys; sys.exit(0 if json.load(sys.stdin).get("loggedIn") else 1)'; then
  echo "Claude Code subscription is not logged in; run: claude auth login --claudeai" >>"$LOG"
  exit 1
fi

# 3. headless Claude Code (subscription) works the batch. Owner 2026-09-23 evening: 「每五篇简历一个
#    worker，尽可能平铺开」— the batch is cut into chunks of ROLES_PER_WORKER (default 5) and one
#    headless session is started per chunk, so a 1000-role batch means 200 sessions instead of 4
#    sessions grinding 250 each. MAX_WORKERS (default 12 on this 24 GB / 14-core machine) is how
#    many run at the same time — each claude process holds a few hundred MB, so a true 200-wide
#    fan-out would swap the laptop to death; the pool refills the moment a session exits, which is
#    what keeps wall-clock short. All three knobs live under config auto_tailor:
#      roles_per_worker: 5 · max_workers: 12 · model: claude-opus-5-5
#    env -u strips any inherited API key so this never bills the API. Workers only write YAMLs and
#    run the READ-ONLY scorer (tools/score_yaml.py) — the single-writer rule is preserved by the
#    ONE ingest pass in step 4.
read -r PER MAXW MODEL <<<"$(.venv/bin/python -c "import sys; sys.path.insert(0,'.'); from app import config as c; at=(c.load_config().get('auto_tailor') or {}); print(int(at.get('roles_per_worker',5)), int(at.get('max_workers',12)), at.get('model','claude-opus-5-5'))")"
NW=$(( (COUNT + PER - 1) / PER ))
echo "tailoring $COUNT roles via headless Claude Code (subscription, model $MODEL): $NW workers × ≤$PER roles, $MAXW at a time…" >>"$LOG"
MANIFEST="$MANIFEST" PER="$PER" .venv/bin/python - <<'PYCHUNK'
import json, os
mp = os.environ["MANIFEST"]; per = int(os.environ["PER"])
m = json.load(open(mp))
for i in range(0, len(m), per):
    with open(mp[:-5] + f"_{i // per}.json", "w") as f:
        json.dump(m[i:i + per], f, ensure_ascii=False, indent=1)
PYCHUNK
PROMPT_TMPL="$(cat tools/autotailor_prompt.md)"
PIDS=""
for ((i=0; i<NW; i++)); do
  CHUNK="${MANIFEST%.json}_$i.json"
  # pool: wait until fewer than MAXW sessions are running
  while [ "$(jobs -rp | wc -l | tr -d ' ')" -ge "$MAXW" ]; do sleep 2; done
  env -u ANTHROPIC_API_KEY "$CLAUDE" -p "${PROMPT_TMPL//scratch_priors\/autotailor_manifest.json/$CHUNK}" --model "$MODEL" \
      --allowedTools "Read" "Write" "Edit" "Bash(.venv/bin/python:*)" >"$LOG.worker$i" 2>&1 &
  PIDS="$PIDS $!"
done
WORKER_FAILED=0
for PID in $PIDS; do
  wait "$PID" || WORKER_FAILED=1
done
for ((i=0; i<NW; i++)); do
  [ -f "$LOG.worker$i" ] && { echo "--- worker $i ---" >>"$LOG"; cat "$LOG.worker$i" >>"$LOG"; rm -f "$LOG.worker$i"; }
done
rm -f "${MANIFEST%.json}"_[0-9]*.json
if [ "$WORKER_FAILED" -ne 0 ]; then
  echo "one or more Claude workers failed; see worker output above" >>"$LOG"
  exit 1
fi

# 4. single-writer ingest: render + score + mark ready everything the workers produced.
#    Default pass (no --all) only touches new/changed YAMLs, so it stays fast.
echo "ingesting…" >>"$LOG"
.venv/bin/python -m app.ingest >>"$LOG" 2>&1 || echo "ingest exited non-zero" >>"$LOG"
echo "===== $(date) auto_tailor done =====" >>"$LOG"
