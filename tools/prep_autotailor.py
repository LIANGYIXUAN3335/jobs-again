"""Daily auto-tailor PREP (no LLM, no cost): pick the review-visible roles that pass the
guardrails (score >= auto_tailor.min_score, no résumé yet, within the per-company cap),
write their tailoring prompts, and emit a manifest for the headless Claude Code step to work
through. Prints how many were queued (0 => the shell wrapper skips the LLM step entirely).
"""
import json
import sys

sys.path.insert(0, ".")
from app import config as cfgmod, store, tailoring  # noqa: E402

MANIFEST = "scratch_priors/autotailor_manifest.json"


def main():
    cfg = cfgmod.load_config()
    at = cfg.get("auto_tailor") or {}
    if not at.get("enabled"):
        print("auto_tailor disabled"); _write([]); return
    min_score = float(at.get("min_score", 75))
    strong_min = float(at.get("strong_min_score", 70))   # strong-verdict roles qualify earlier
    cap = int(at.get("max_per_day", 30))

    conn = store.connect_view(cfg)
    # status="all": the per-company DISPLAY cap only applies to the "new" view, and it was
    # hiding brand-new qualifying roles at full companies (Salesforce's 16th role never
    # reached the nightly tailor). Filter by the per-row in_review flag instead — same
    # eligibility semantics (new + not hidden/cooldown/drop), no display cap.
    d = store.fetch_view(conn, cfg, status="all")
    todo = [r for r in d["rows"]
            if r.get("in_review")
            and r.get("tailor_status") != "ready" and r.get("verdict") != "drop"
            and ((r["weighted_score"] or 0) >= min_score
                 or (r.get("verdict") == "strong"
                     and (r["weighted_score"] or 0) >= strong_min))]
    # Rank, not raw score: within a day's cap, spend the tailoring budget on the roles that
    # only just went up — the gate above is still the raw score, so freshness cannot let a
    # weak role in, it only reorders the ones already qualified.
    todo.sort(key=lambda r: -r.get("rank_score", r["weighted_score"] or 0))
    todo = todo[:cap]

    manifest = []
    for r in todo:
        try:
            p = tailoring.write_prompt(cfg, conn, r["job_id"])
            out = tailoring.resume_yaml_path(cfg, r["company"], r["job_id"])
            manifest.append({"job_id": r["job_id"], "company": r["company"], "title": r["title"],
                             "prompt": str(p), "resume_out": str(out), "score": r["weighted_score"]})
        except Exception as e:  # noqa: BLE001
            print(f"  skip {r['job_id']}: {e}", file=sys.stderr)
    _write(manifest)
    print(f"auto_tailor prep: {len(manifest)} roles queued (score>={min_score:g}, cap {cap})")


def _write(manifest):
    cfgmod.resolve_path(MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
