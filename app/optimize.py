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


def _surface(tailored: dict, keywords: list[str], base: dict) -> int:
    """Add true-but-missing HARD skills into the tailored skills, each into the base group it
    belongs to. Returns how many added.

    Credibility rules (2026-08-06): soft skills are never surfaced (they live in bullet verbs,
    and the ingest validator rejects them as list items); no "Additional Relevant Skills"
    catch-all group (that label itself reads as stuffing); never exceed 4 groups — a keyword
    with no natural home stays out, and the honest ceiling is the ceiling."""
    max_groups = tailoring._MAX_SKILL_GROUPS
    # canonical keyword -> (base group label, base item wording)
    home: dict[str, tuple[str, str]] = {}
    for label, items in (base.get("skills_master") or {}).items():
        for it in items:
            for k in keywords:
                if k in resume_score._HARD_SKILLS and k not in home and (
                        k.lower() in it.lower() or it.lower() in k.lower()):
                    home[k] = (label, it)
    skills = tailored.setdefault("skills", [])
    have = {i.lower() for g in skills for i in g.get("items", [])}
    added = 0
    for k, (label, item) in home.items():
        if item.lower() in have or k.lower() in have:
            continue
        group = next((g for g in skills if g.get("label") == label), None)
        if group is None:
            if len(skills) >= max_groups:
                continue                      # no honest home left — stop, don't stuff
            group = {"label": label, "items": []}
            skills.append(group)
        group["items"].append(item)
        have.add(item.lower())
        added += 1
    return added


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
        sc = resume_score.score_resume(jd, merged, cfg, base_text, title=role.get("title"))
        if sc.family == "non_eng":
            break                              # wrong role family — no keyword can fix that
        if sc.overall >= sc_gate(cfg):
            break
        n = _surface(tailored, sc.coverable, base)   # only ever surfaces TRUE-for-her hard skills
        added_total += n
        if n == 0:
            break                              # nothing truthful left to add → human gate
    # Write ONLY when _surface actually added something (2026-08-10). This used to rewrite
    # unconditionally, which reset mtime on every file it touched — and staleness was decided
    # by mtime, so one documented `python -m app.optimize --all` marked all 3,298 résumés
    # fresh without regenerating a single bullet, silently voiding the credibility cutover.
    # Staleness no longer trusts mtime at all (see tailoring.resume_is_stale), but a no-op
    # rewrite is still a lie to every other mtime consumer — `app.ingest --watch`'s seen-map,
    # rsync, backups. Gated on `added_total` rather than on a text compare because safe_dump
    # normalises the agent's block scalars and quoting, so the serialised form differs from
    # the file on disk even when nothing about the résumé changed.
    if added_total:
        ypath.write_text(yaml.safe_dump(tailored, sort_keys=False, allow_unicode=True),
                         encoding="utf-8")
    res = tailoring.ingest_file(cfg, conn, ypath)   # re-render PDF + final score
    sc = res["score"]
    return {
        "company": role["company"], "job_id": job_id, "overall": sc.overall,
        "verdict": sc.verdict, "family": sc.family, "surfaced": added_total,
        "true_gaps": sc.true_gaps, "line": sc.summary_line(),
    }


def sc_gate(cfg: dict) -> float:
    rs = cfg.get("resume_scoring") or {}
    return float(rs.get("target", rs.get("gate", 80)))      # loop target = the "apply" bar


def main(argv: list[str]) -> None:
    cfg = cfgmod.load_config()
    conn = store.connect_view(cfg)
    if argv and argv[0] == "--all":
        # A batch over every yaml on disk MUST survive individual bad files: purged roles
        # leave orphan yamls whose job_id is no longer in the DB, and one of those used to
        # kill the whole run on file #N with 1300 left to go.
        rdir = cfgmod.resolve_path(cfg["output_dirs"]["resumes"])
        jobs = []
        for p in sorted(rdir.glob("*.yaml")):
            try:
                jid = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("job_id")
            except Exception as e:  # noqa: BLE001
                print(f"⏭️  {p.name}: unreadable yaml ({e})", flush=True)
                continue
            if jid:
                jobs.append(jid)
            else:
                print(f"⏭️  {p.name}: no job_id", flush=True)
    else:
        jobs = argv
    if not jobs:
        print("usage: python -m app.optimize <job_id> | --all"); return
    gate = sc_gate(cfg)
    mark = {"strong": "🟢", "apply": "✅", "improve": "🟡", "drop": "⏸️ "}
    bands: dict = {}
    skipped = wrong_family = 0
    for i, jid in enumerate(jobs, 1):
        try:
            r = optimize(cfg, conn, jid)
        except Exception as e:  # noqa: BLE001 — skip and continue; the batch must finish
            skipped += 1
            print(f"⏭️  [{i}/{len(jobs)}] {jid}: {type(e).__name__}: {e}", flush=True)
            continue
        bands[r["verdict"]] = bands.get(r["verdict"], 0) + 1
        if r.get("family") == "non_eng":
            wrong_family += 1
        print(f"{mark.get(r['verdict'], '  ')} [{i}/{len(jobs)}] {r['company']:16} {r['line']}"
              f"   (+{r['surfaced']} surfaced)", flush=True)
        if r["overall"] < gate and r["true_gaps"] and r.get("family") != "non_eng":
            print(f"    to reach {gate:.0f} you'd need — confirm any you actually have: "
                  f"{', '.join(r['true_gaps'])}", flush=True)
    if len(jobs) > 1:
        print(f"\n=== {len(jobs)} résumés: " +
              " · ".join(f"{k} {v}" for k, v in sorted(bands.items())) +
              f" · wrong-family {wrong_family} · skipped {skipped}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
