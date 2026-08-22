"""Batch wrapper around tailoring.ingest_file — same call the autotailor runbook specifies,
run over several tailored YAMLs at once. Prints SCORE plus the coverable keywords still
missing, so a revision pass can target truthful placements instead of guessing."""
import sys
from pathlib import Path

from app import config, store, tailoring

cfg = config.load_config()
conn = store.connect_view(cfg)
for p in sys.argv[1:]:
    try:
        r = tailoring.ingest_file(cfg, conn, Path(p))
        sc = r["score"]
        print(f"SCORE {sc.overall} {Path(p).name} | skills {sc.skills_coverage} "
              f"title {sc.title_alignment} metrics {sc.achievements} | "
              f"still-coverable: {', '.join(sc.coverable) or '-'} | "
              f"true-gaps: {', '.join(sc.true_gaps) or '-'}")
    except Exception as e:  # noqa: BLE001 - report and keep going through the batch
        print(f"FAIL {Path(p).name}: {type(e).__name__}: {e}")
