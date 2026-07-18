"""Pre-screen every scanned role by potential résumé match — before spending effort tailoring.

Scores Casey's FULL base résumé (all true skills + all real bullets = her truthful ceiling)
against each role's JD, ranks high→low, and marks which clear the gate. Use it to pick the
roles worth tailoring instead of forcing stretch roles. No LLM, no API key.

    python -m app.prescreen                 # rank all, show the whole board
    python -m app.prescreen --min 70        # only roles whose ceiling ≥ 70
    python -m app.prescreen --top 25
"""
from __future__ import annotations

import sys

from . import config as cfgmod
from . import resume_score, store, tailoring


def base_as_merged(base: dict) -> dict:
    """The base résumé shaped like a merged/tailored one, using every true skill and bullet."""
    return {
        "name": base["name"], "contact": base["contact"], "summary": "",
        "education": base["education"],
        "skills": [{"label": k, "items": v} for k, v in base["skills_master"].items()],
        "experience": [{"company": j["company"], "title": j["title"],
                        "location": j["location"], "dates": j["dates"],
                        "bullets": j["bullets_master"]} for j in base["experience"]],
    }


def prescreen(cfg: dict, min_score: float = 0.0) -> list[dict]:
    base = tailoring.load_base(cfg)
    merged = base_as_merged(base)
    base_text = tailoring._base_text(base)
    conn = store.connect_jobs(cfg)
    rows = conn.execute(
        "SELECT job_id, company, title, jd_text FROM roles "
        "WHERE jd_text IS NOT NULL AND jd_text != ''").fetchall()
    out = []
    for r in rows:
        sc = resume_score.score_resume(r["jd_text"], merged, cfg, base_text)
        req = len(sc.matched_hard) + len(sc.missing_hard)      # hard skills the JD asks for
        # Pure coverage% is degenerate: a thin / non-engineering JD asks for almost nothing,
        # so she "covers" ~100%. Rank by ABSOLUTE hard-skill overlap (technical richness she
        # actually meets); coverage% only breaks ties. `req` exposes shallow JDs.
        out.append({"ceiling": sc.overall, "hit": len(sc.matched_hard), "req": req,
                    "company": r["company"], "title": r["title"], "job_id": r["job_id"],
                    "missing": sc.missing_hard})
    out = [r for r in out if r["ceiling"] >= min_score]
    out.sort(key=lambda x: (x["hit"], x["ceiling"]), reverse=True)
    return out


def main(argv: list[str]) -> None:
    cfg = cfgmod.load_config()
    gate = float((cfg.get("resume_scoring") or {}).get("gate", 80))
    min_score = 0.0
    top = None
    for i, a in enumerate(argv):
        if a == "--min" and i + 1 < len(argv):
            min_score = float(argv[i + 1])
        if a == "--top" and i + 1 < len(argv):
            top = int(argv[i + 1])
    ranked = prescreen(cfg, min_score)
    # a genuine fit = technically rich JD (asks for ≥5 hard skills) she covers well
    worth = [r for r in ranked if r["req"] >= 5 and r["ceiling"] >= 70]
    shown = (worth[:top] if top else worth)
    print(f"\nPre-screen — base résumé vs {len(ranked)} roles.  "
          f"'hit/req' = hard eng skills she meets / the JD asks for.\n"
          f"Ranked by absolute overlap (thin & non-eng JDs sink).  gate {gate:.0f}\n")
    for r in shown:
        mark = "✅" if r["ceiling"] >= gate else "🟡"
        print(f"  {mark} {r['ceiling']:>3}  {r['hit']:>2}/{r['req']:<2}  "
              f"{r['company'][:20]:<20} {r['title'][:42]}")
    print(f"\n  {len(worth)} roles worth tailoring (JD asks ≥5 hard skills & ceiling ≥70).")
    print(f"  Showing top {len(shown)}. Run with --top N to see more.")


if __name__ == "__main__":
    main(sys.argv[1:])
