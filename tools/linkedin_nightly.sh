#!/bin/bash
# linkedin_nightly.sh — the LinkedIn pipeline, run by launchd at 08:20 daily
# (com.candidate.jobscan.linkedin.plist), before the 09:00 main scan. DESIGN.md §5c.
#
#   alerts (IMAP digests) ─┐
#                          ├─> pending.json ─> enrich (guest JD fetch) ─> ingest + rescore
#   search (guest sweep) ──┘
#
# Every step is fail-soft: a blocked/broken step logs and the rest still runs, so a
# LinkedIn throttle never kills the alert->ingest path (official email channel).
set -u
cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python

# Single-flight: alerts/search/enrich all read-modify-write pending.json, so a manual run
# overlapping the 08:20 launchd run would clobber it. macOS ships no flock(1); mkdir is
# the portable mutex. A lock dir older than 2h belongs to a crashed run — steal it.
LOCK=data/linkedin_nightly.lock
if ! mkdir "$LOCK" 2>/dev/null; then
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +120 2>/dev/null)" ]; then
        echo "stale lock (>2h) — taking over"
    else
        echo "another linkedin_nightly run is in progress — exiting"
        exit 0
    fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

echo "=== linkedin nightly $(date '+%F %T') ==="
# alerts keeps its 7-day default window: launchd skips runs missed while powered off,
# and the seen-set makes re-reading idempotent, so the wide window costs nothing.
$PY tools/linkedin_alerts.py  || echo "!! alerts step failed (continuing)"
$PY tools/linkedin_search.py  || echo "!! search step failed (continuing)"
$PY tools/linkedin_enrich.py  || echo "!! enrich step failed (continuing)"
if $PY tools/linkedin_ingest.py scratch_priors/linkedin_enriched; then
    # archive processed files so tomorrow's ingest log stays readable; keep 30 days
    mkdir -p scratch_priors/linkedin_enriched_archive
    if compgen -G "scratch_priors/linkedin_enriched/*.json" > /dev/null; then
        mv scratch_priors/linkedin_enriched/*.json scratch_priors/linkedin_enriched_archive/ \
            || echo "!! archive move failed — files left in place"
    fi
    find scratch_priors/linkedin_enriched_archive -name '*.json' -mtime +30 -delete 2>/dev/null
else
    echo "!! ingest step failed — enriched files kept for retry"
fi

# Link li: rows to their real ATS postings (dedupe + direct apply links; owner-approved
# 2026-09-15). Runs even when ingest failed — the historical backlog still deserves the
# pass. Caps at 40 orphan-company probes per night and remembers dead ends in
# data/li_resolve_state.json, so it chips away day by day.
$PY tools/li_resolve.py --limit-companies 40 || echo "!! li_resolve step failed (continuing)"

# Tailor whatever just landed, instead of waiting for the next 09:45 run. Ingest adds roles
# (and new companies) to the review queue; without this they sit there résumé-less until the
# following morning, which is exactly the "好多都没有 resume" the owner hit on 2026-07-30.
# auto_tailor.sh holds its own lock, so overlapping with the scheduled run is a no-op.
bash tools/auto_tailor.sh || echo "!! auto_tailor step failed (continuing)"

echo "=== linkedin nightly done $(date '+%F %T') ==="
