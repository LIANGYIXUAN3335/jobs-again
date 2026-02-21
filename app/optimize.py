"""Auto-optimize a tailored résumé toward the match gate — the honest way.

Loop: score → if below gate, SURFACE keywords that are already TRUE for Casey (present in
the locked base) but missing from this draft → re-score → repeat. It NEVER invents skills.
When only "true gaps" remain (JD wants them, the base doesn't support them), it STOPS and
returns them for human confirmation: a real skill the base just didn't list yet → confirm →
add to resume_base.yaml → re-run. A skill she genuinely lacks → leave it; the role is a HOLD.

    python -m app.optimize gh:datadog:7993551          # optimize one
    python -m app.optimize --all                       # every tailored résumé on disk
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from . import config as cfgmod
from . import resume_score, store, tailoring


def _surface(tailored: dict, keywords: list[str]) -> int:
    """Add true-but-missing skill keywords into the tailored skills. Returns how many added."""
    skills = tailored.setdefault("skills", [])
    have = {i.lower() for g in skills for i in g.get("items", [])}
    fresh = [k for k in keywords if k.lower() not in have]
    if not fresh:
        return 0
    # drop them into a clearly-labelled group so it reads as a skills list, not stuffing
    for g in skills:
        if g.get("label") == "Additional Relevant Skills":
            g["items"].extend(fresh)
            break
    else:
        skills.append({"label": "Additional Relevant Skills", "items": fresh})
    return len(fresh)


def optimize(cfg: dict, conn, job_id: str, max_rounds: int = 6) -> dict:
    role = store.get_role(conn, job_id)
    if not role:
        raise ValueError(f"unknown job_id {job_id!r}")
    ypath = tailoring.resume_yaml_path(cfg, role["company"], job_id)
    if not ypath.exists():
        raise FileNotFoundError(f"no tailored résumé yet: {ypath}")
    tailored = yaml.safe_load(ypath.read_text(encoding="utf-8"))
    base = tailoring.load_base(cfg)
    base_text = tailoring._base_text(base)
    jd = role.get("jd_text", "")

    added_total = 0
    for _ in range(max_rounds):
        merged = tailoring.merge(base, tailored)
        sc = resume_score.score_resume(jd, merged, cfg, base_text)
        if sc.overall >= sc_gate(cfg):
            break
        n = _surface(tailored, sc.coverable)   # only ever surfaces TRUE-for-her keywords
        added_total += n
        if n == 0:
            break                              # nothing truthful left to add → human gate
    ypath.write_text(yaml.safe_dump(tailored, sort_keys=False, allow_unicode=True), encoding="utf-8")
    res = tailoring.ingest_file(cfg, conn, ypath)   # re-render PDF + final score
    sc = res["score"]
    return {
        "company": role["company"], "job_id": job_id, "overall": sc.overall,
        "verdict": sc.verdict, "surfaced": added_total,
        "true_gaps": sc.true_gaps, "line": sc.summary_line(),
    }


def sc_gate(cfg: dict) -> float:
    return float((cfg.get("resume_scoring") or {}).get("gate", 80))


def main(argv: list[str]) -> None:
    cfg = cfgmod.load_config()
    conn = store.connect_view(cfg)
    if argv and argv[0] == "--all":
        rdir = cfgmod.resolve_path(cfg["output_dirs"]["resumes"])
        jobs = [yaml.safe_load(p.read_text())["job_id"] for p in sorted(rdir.glob("*.yaml"))]
    else:
        jobs = argv
    if not jobs:
        print("usage: python -m app.optimize <job_id> | --all"); return
    gate = sc_gate(cfg)
    for jid in jobs:
        r = optimize(cfg, conn, jid)
        flag = "✅" if r["overall"] >= gate else "⏸️ "
        print(f"\n{flag} {r['company']:16} {r['line']}   (+{r['surfaced']} surfaced)")
        if r["overall"] < gate and r["true_gaps"]:
            print(f"    need human confirm — do you actually have any of these? "
                  f"{', '.join(r['true_gaps'])}")


if __name__ == "__main__":
    main(sys.argv[1:])
