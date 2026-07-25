"""What the market keeps asking for that her résumé can't answer — aggregated, priced, triaged.

`resume_score` already computes `true_gaps` for every single role (JD wants X, the locked base
résumé doesn't support X). That signal was only ever read one role at a time, so a term missing
from a quarter of the corpus looked exactly like a one-off. This adds them up.

Every gap is priced in MATCH POINTS AT STAKE: closing gap k on a role raises that résumé's
overall by 0.60 · 100 · w / need   (w = 2 hard / 1 soft, need = the JD's total keyword weight),
summed over every role that wants it. That is what makes "learn Rust" comparable to "the word
'ownership' never appears in my résumé" — one number, same units, ranked together.

Two kinds of gap hide in that list and they need opposite fixes:
  wording — she has done the work, the résumé just never used the industry word for it.
            Fix by editing resumes/resume_base.yaml. Free, instant, applies everywhere.
  real    — she genuinely doesn't have it. Fix by learning it, or by not applying to those roles.
Nothing can tell them apart automatically, so the report never guesses: it ranks the untriaged
ones by cost and asks. Your verdicts live in config.yaml under `gap_triage:` and persist.

    python tools/gap_report.py                 # all scanned roles
    python tools/gap_report.py --min-score 70  # only roles worth applying to
    python tools/gap_report.py --status new    # only the live review queue
    python tools/gap_report.py --top 25 --out output/gap_report.md

No network, no LLM, no API key.
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as cfgmod  # noqa: E402
from app import prescreen, resume_score, store, tailoring  # noqa: E402

VERDICTS = ("real", "wording", "wontfix")


def _load_roles(cfg, conn, *, min_score: float, status: str, company: str) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT r.job_id, r.company, r.title, r.jd_text, r.weighted_score, r.verdict,"
        "       COALESCE(s.status,'new') AS own_status"
        " FROM jobs.roles r LEFT JOIN role_state s ON s.job_id = r.job_id"
        " WHERE r.jd_text IS NOT NULL AND r.jd_text != ''")]
    out = []
    for r in rows:
        if min_score and (r["weighted_score"] or 0) < min_score:
            continue
        if status and status != "all" and r["own_status"] != status:
            continue
        if company and cfgmod.norm_key(company) not in cfgmod.norm_key(r["company"]):
            continue
        out.append(r)
    return out


def analyse(cfg: dict, conn, *, min_score=0.0, status="", company="") -> dict:
    """Score every selected role against the base résumé's ceiling and total up the gaps."""
    base = tailoring.load_base(cfg)
    base_text = tailoring._base_text(base)
    ceiling = prescreen.base_as_merged(base)      # every true skill + every real bullet
    roles = _load_roles(cfg, conn, min_score=min_score, status=status, company=company)

    n_roles = collections.Counter()               # gap -> how many roles want it
    points = collections.Counter()                # gap -> match points at stake
    companies: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    unsurfaced = collections.Counter()            # TRUE for her, JD wants it, résumé omits it
    scores = []
    non_eng = 0                                   # wrong-family roles: their gaps aren't yours

    for r in roles:
        jd = r["jd_text"]
        sc = resume_score.score_resume(jd, ceiling, cfg, base_text, title=r["title"])
        if sc.family == "non_eng":
            # A consultant/reporter/sales JD's unmet asks are not skills to learn — counting
            # them would put "stakeholder management" on the learn list next to Rust.
            non_eng += 1
            continue
        scores.append(sc.overall)
        need = (2 * len(resume_score._present(jd, resume_score._HARD_SKILLS))
                + len(resume_score._present(jd, resume_score._SOFT_SKILLS)))
        if not need:
            continue
        for k in sc.true_gaps:
            w = 2 if k in resume_score._HARD_SKILLS else 1
            n_roles[k] += 1
            points[k] += 0.60 * 100 * w / need
            companies[k][r["company"]] += 1
        for k in sc.coverable:                    # she has it; this résumé just didn't say it
            unsurfaced[k] += 1

    triage = {str(k).lower(): v for k, v in (cfg.get("gap_triage") or {}).items()}
    return {
        "roles": len(roles) - non_eng, "non_eng": non_eng, "scores": scores,
        "n_roles": n_roles, "points": points, "companies": companies,
        "unsurfaced": unsurfaced, "triage": triage,
    }


def _verdict(triage: dict, gap: str) -> tuple[str, str]:
    t = triage.get(gap.lower())
    if isinstance(t, dict):
        return str(t.get("verdict", "")).lower(), str(t.get("note", ""))
    if isinstance(t, str):
        return t.lower(), ""
    return "", ""


def _table(rows: list[str], header: str) -> list[str]:
    if not rows:
        return []
    return [header, "|---|---:|---:|---|", *rows, ""]


def render(a: dict, *, top: int) -> str:
    n, pts, cos, triage = a["n_roles"], a["points"], a["companies"], a["triage"]
    total_roles = a["roles"] or 1
    scores = a["scores"]
    buckets: dict[str, list[tuple[str, int, float]]] = {v: [] for v in VERDICTS}
    buckets["untriaged"] = []
    for gap, cnt in n.most_common():
        v, _note = _verdict(triage, gap)
        buckets.setdefault(v if v in VERDICTS else "untriaged", []).append((gap, cnt, pts[gap]))

    def rows(key: str, limit: int) -> list[str]:
        out = []
        for gap, cnt, p in buckets[key][:limit]:
            _v, note = _verdict(triage, gap)
            who = ", ".join(f"{c} ({k})" for c, k in cos[gap].most_common(3))
            out.append(f"| {gap} | {cnt} ({100 * cnt // total_roles}%) | {p:.0f} | {note or who} |")
        return out

    hdr = "| gap | roles asking | match pts at stake | "
    L = [
        "# Gap report",
        "",
        f"{a['roles']} engineering roles scored against the base résumé's ceiling "
        f"(every true skill + every real bullet). Average ceiling "
        f"**{sum(scores) / max(len(scores), 1):.1f}/100**; "
        f"{sum(1 for s in scores if s >= 80) * 100 // max(len(scores), 1)}% clear the apply gate."
        + (f" ({a['non_eng']} non-engineering-titled roles excluded — their asks are not your gaps.)"
           if a.get("non_eng") else ""),
        "",
        "\"Match pts at stake\" = how much résumé match score closing that gap would recover "
        "across the whole corpus. It is an upper bound (coverage caps at 100), so read it as a "
        "ranking, not a promise.",
        "",
    ]

    if buckets["untriaged"]:
        L += [
            "## Untriaged — your call",
            "",
            "For each: have you actually done this and the résumé just never said so (`wording`), "
            "or is it a real gap (`real`)? Record the answer in `config.yaml` under `gap_triage:` "
            "and it stops showing up here.",
            "",
            *_table(rows("untriaged", top), hdr + "top companies asking |"),
        ]
    if buckets["real"]:
        L += [
            "## Real gaps — the learn list",
            "",
            "Ranked by what they actually cost you, not by how often you notice them.",
            "",
            *_table(rows("real", top), hdr + "note / top companies |"),
        ]
    if buckets["wording"]:
        L += [
            "## Wording gaps — fix in `resumes/resume_base.yaml`",
            "",
            "You have done this work; the résumé doesn't use the word for it. Free points.",
            "",
            *_table(rows("wording", top), hdr + "note |"),
        ]
    if buckets["wontfix"]:
        L += ["## Won't fix", "", *_table(rows("wontfix", top), hdr + "note |")]

    if a["unsurfaced"]:
        L += [
            "## Already true, just not on the page",
            "",
            "The JD wants it, the base résumé supports it, and this role's tailored résumé still "
            "left it out. `python -m app.optimize --all` surfaces these; no judgement call needed.",
            "",
            "| skill | roles |", "|---|---:|",
            *[f"| {k} | {v} |" for k, v in a["unsurfaced"].most_common(top)],
            "",
        ]
    return "\n".join(L) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--min-score", type=float, default=0.0, help="only roles at/above this weighted score")
    ap.add_argument("--status", default="", help="new | applied | skip | rejected | all")
    ap.add_argument("--company", default="", help="restrict to one company")
    ap.add_argument("--top", type=int, default=20, help="rows per section")
    ap.add_argument("--out", default="output/gap_report.md")
    args = ap.parse_args(argv)

    cfg = cfgmod.load_config()
    conn = store.connect_view(cfg)
    try:
        a = analyse(cfg, conn, min_score=args.min_score, status=args.status, company=args.company)
    finally:
        conn.close()
    if not a["roles"]:
        print("no roles matched those filters")
        return 1

    md = render(a, top=args.top)
    out = cfgmod.resolve_path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")

    untriaged = [(g, c) for g, c in a["n_roles"].most_common()
                 if _verdict(a["triage"], g)[0] not in VERDICTS]
    print(f"{a['roles']} roles · avg ceiling {sum(a['scores']) / len(a['scores']):.1f}/100")
    print(f"→ {out}")
    if untriaged:
        print(f"\n{len(untriaged)} untriaged gaps — top by cost:")
        for g, c in sorted(untriaged, key=lambda x: -a['points'][x[0]])[:8]:
            print(f"   {a['points'][g]:6.0f} pts  {c:5d} roles  {g}")
        print("\n   classify them in config.yaml → gap_triage: {<gap>: {verdict: real|wording|wontfix, note: ...}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
