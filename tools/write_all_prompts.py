"""Phase A of the batch tailor run: write a tailoring prompt for EVERY live role that
isn't already queued/ready, so nothing is stuck (fixes 'clicked the button, no response').
Deterministic, no LLM. Read-only on jobs DB; writes role_state (queued) via connect_view.
Prints a JSON worklist of ALL queued roles (old + new) sorted by weighted_score desc.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as cfgmod          # noqa: E402
from app import store, tailoring          # noqa: E402


def main() -> None:
    cfg = cfgmod.load_config()
    conn = store.connect_view(cfg)
    try:
        live = {r["job_id"]: r for r in
                conn.execute("SELECT job_id, company, title, weighted_score "
                             "FROM jobs.roles").fetchall()}
        state = {row[0]: row[1] for row in
                 conn.execute("SELECT job_id, COALESCE(tailor_status,'') FROM role_state")}

        wrote = 0
        for job_id in live:
            if state.get(job_id) in ("queued", "ready"):
                continue
            tailoring.write_prompt(cfg, conn, job_id)   # sets tailor_status='queued'
            wrote += 1
        conn.commit()

        # rebuild worklist: every queued (not yet ready) role, richest-score first
        worklist = []
        for job_id, r in live.items():
            st = conn.execute("SELECT COALESCE(tailor_status,''), prompt_path "
                              "FROM role_state WHERE job_id=?", (job_id,)).fetchone()
            status = st[0] if st else ""
            if status != "queued":
                continue
            worklist.append({
                "job_id": job_id,
                "company": r["company"],
                "title": r["title"],
                "score": r["weighted_score"] if r["weighted_score"] is not None else 0,
                "prompt_path": str(tailoring.prompt_path(cfg, r["company"], job_id)),
                "yaml_path": str(tailoring.resume_yaml_path(cfg, r["company"], job_id)),
            })
    finally:
        conn.close()

    worklist.sort(key=lambda x: x["score"], reverse=True)
    Path("output/worklist.json").write_text(json.dumps(worklist, ensure_ascii=False, indent=2))
    print(json.dumps({"prompts_written": wrote, "queued_total": len(worklist),
                      "worklist_file": "output/worklist.json"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
