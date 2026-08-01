"""Daily auto-tailor PREP (no LLM, no cost): pick the review-visible roles that pass the
guardrails, write their tailoring prompts, and emit a manifest for the headless Claude Code
step to work through. Prints how many were queued (0 => the shell wrapper skips the LLM step).

With `application_budget` on (the default since 2026-07-30) the guardrail is simply "is it in
today's review queue" — budget.select_daily already applied binary eligibility, the per-company
stock cap, strict-company pacing, and the daily bucket mix. The legacy score gate
(auto_tailor.min_score) is only used when application_budget.enabled is false.
"""
import json
import sys

sys.path.insert(0, ".")
from app import budget, config as cfgmod, store, tailoring  # noqa: E402

MANIFEST = "scratch_priors/autotailor_manifest.json"


def main():
    cfg = cfgmod.load_config()
    at = cfg.get("auto_tailor") or {}
    if not at.get("enabled"):
        print("auto_tailor disabled"); _write([]); return
    conn = store.connect_view(cfg)

    if budget.enabled(cfg):
        # Contract (owner 2026-07-30): "what the review queue shows me today is what should
        # have a résumé today". No score gate at all — application_budget already decided
        # eligibility (binary) and volume, so re-gating on score here would reintroduce
        # exactly the bug we removed: a 66-point role at a company with zero applications is
        # worth more than Nvidia's 14th 78-point role.
        #
        # ONE PER COMPANY (2026-07-31). The queue now lists every eligible role interleaved by
        # company rather than one per company, so "everything the queue shows" became ~1800
        # rows / ~1000 untailored — a batch no subscription run finishes. The owner's actual
        # ask is company-level: "所有可以投的公司我都需要一份简历". The interleave puts each
        # company's lead role first, so taking the first row per company is exactly that set,
        # and it is the row she reaches first when working top-down.
        d = store.fetch_view(conn, cfg, status="new")
        seen: set[str] = set()
        leads = [r for r in d["rows"]
                 if not (r["company_norm"] in seen or seen.add(r["company_norm"]))]
        todo = [r for r in leads if r.get("tailor_status") != "ready"]
        cap = len(todo)
    else:
        min_score = float(at.get("min_score", 75))
        strong_min = float(at.get("strong_min_score", 70))
        cap = int(at.get("max_per_day", 30))
        d = store.fetch_view(conn, cfg, status="all")
        todo = [r for r in d["rows"]
                if r.get("in_review")
                and r.get("tailor_status") != "ready" and r.get("verdict") != "drop"
                and ((r["weighted_score"] or 0) >= min_score
                     or (r.get("verdict") == "strong"
                         and (r["weighted_score"] or 0) >= strong_min))]
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
    gate = "budget: today's review queue" if budget.enabled(cfg) else f"score>={min_score:g}"
    print(f"auto_tailor prep: {len(manifest)} roles queued ({gate}, cap {cap})")


def _write(manifest):
    cfgmod.resolve_path(MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
