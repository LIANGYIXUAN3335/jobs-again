"""Read-only résumé↔JD scorer for the tailoring refine-loop.

Usage:  python tools/score_yaml.py output/resumes/<stem>.yaml

Loads the tailored YAML, merges it with the locked base (facts from base), scores it
against the role's JD with the SAME scorer the ingest step uses, and prints JSON feedback:

  overall (0-100), verdict, sub-scores, and — crucially for refining —
  `coverable`: JD skills that are TRUE for her (present in base) but missing from THIS
               résumé  → ADD these truthfully to lift skills_coverage (the 60% lever).
  `true_gaps`: JD skills not in her background → put in `gaps`, never claim.

Strong-match target = `resume_scoring.strong` (85). Read-only: safe while the web app runs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as cfgmod          # noqa: E402
from app import resume_score, store, tailoring  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        print(json.dumps({"error": "usage: score_yaml.py <yaml>"}))
        sys.exit(2)
    yf = Path(sys.argv[1])
    try:
        tailored = yaml.safe_load(yf.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"yaml parse: {e}"}))
        sys.exit(1)

    job_id = (tailored or {}).get("job_id")
    if not job_id:
        print(json.dumps({"error": "missing job_id in yaml"}))
        sys.exit(1)

    cfg = cfgmod.load_config()
    conn = store.connect_view(cfg)          # read-only use of the jobs DB
    try:
        role = store.get_role(conn, job_id)
    finally:
        conn.close()
    if not role:
        print(json.dumps({"error": f"job_id {job_id!r} not in DB"}))
        sys.exit(1)

    base = tailoring.load_base(cfg)
    # validate first so the agent gets a clear structural error instead of a scorer crash
    try:
        tailoring.validate_tailored(tailored)
    except tailoring.TailorError as e:
        print(json.dumps({"error": f"invalid tailored yaml: {e}", "overall": 0}))
        sys.exit(0)

    merged = tailoring.merge(base, tailored)
    sc = resume_score.score_resume(
        role.get("jd_text", ""), merged, cfg, tailoring._base_text(base)
    )
    strong = float((cfg.get("resume_scoring") or {}).get("strong", 85))
    out = {
        "job_id": job_id,
        "company": role["company"],
        "title": role["title"],
        "overall": sc.overall,
        "strong_target": strong,
        "is_strong": sc.overall >= strong,
        "verdict": sc.verdict,
        "skills_coverage": sc.skills_coverage,   # 60% of overall — the main lever
        "title_alignment": sc.title_alignment,   # 15% — echo the JD role family
        "achievements": sc.achievements,         # 15% — bullets carrying a number/%
        "parseability": sc.parseability,         # 10%
        "coverable_add_these": sc.coverable,     # TRUE for her, missing here → add truthfully
        "true_gaps_do_not_claim": sc.true_gaps,  # not in her background → gaps only
        "missing_hard": sc.missing_hard,
        "missing_soft": sc.missing_soft,
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
