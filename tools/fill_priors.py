"""Turn researched Glassdoor/financial data (scratch_priors/*.json) into calibrated
company_notes.yaml priors. Method:
  culture_prior = clamp(3.0 + (Glassdoor Culture&Values - 3.7) * 1.5, 1, 5)
  wlb_prior     = clamp(3.0 + (Glassdoor WLB [SWE-specific if available] - 3.7) * 1.5, 1, 5)
  stability_prior = clamp(financial_bucket_base - recent_layoff_penalty, 1, 5)
Anchor 3.7 = tech-industry median; x1.5 spreads Glassdoor's compressed 3.4-4.4 range.
Run: python tools/fill_priors.py         (writes company_notes.yaml + prints a table)
"""
import glob
import json
import os

ANCHOR, K = 3.7, 1.5
BUCKET_BASE = {
    "megacap-profitable": 4.7,
    "large-public-profitable": 4.0,
    "private-latestage-funded": 3.6,
    "public-unprofitable": 3.2,
    "private-early": 2.9,
    "crypto-volatile": 2.6,
}
LAYOFF_PENALTY = {"2026": 0.6, "2025": 0.4, "2024": 0.2}   # older / none => 0

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def clamp(x, lo=1.0, hi=5.0):
    return round(max(lo, min(hi, x)), 1)


def calibrate(sub):
    return clamp(3.0 + (sub - ANCHOR) * K)


def stability(bucket, layoff_year):
    base = BUCKET_BASE.get(bucket, 3.0)
    pen = LAYOFF_PENALTY.get(str(layoff_year), 0.0)
    return clamp(base - pen)


def load_all():
    """Researched priors live in scratch_priors/batch*.json — one JSON list of company dicts.

    Narrowed from `*.json` on 2026-07-30: that directory has since become the scratch space
    for pipeline manifests too (autotailor_manifest.json, linkedin_alert_seen.json, …), and
    linkedin_alert_seen.json is a plain list of id strings, so the old glob crashed on
    `r["company"]`. Shape-check each record as well, so a future stray file degrades to a
    warning instead of taking the tool down.
    """
    rows = {}
    for f in sorted(glob.glob(os.path.join(HERE, "scratch_priors", "batch*.json"))):
        try:
            data = json.load(open(f))
        except Exception as e:  # noqa: BLE001
            print(f"  ! skip {os.path.basename(f)}: {e}")
            continue
        if not isinstance(data, list):
            print(f"  ! skip {os.path.basename(f)}: not a list")
            continue
        for r in data:
            if isinstance(r, dict) and "company" in r:
                rows[r["company"]] = r
    return rows


def main():
    rows = load_all()
    out_lines = [
        "# Company priors — CALIBRATED FROM REAL GLASSDOOR/FINANCIAL DATA (tools/fill_priors.py).",
        "#   culture_prior = 3.0 + (Glassdoor Culture&Values - 3.7)*1.5",
        "#   wlb_prior     = 3.0 + (Glassdoor WLB[SWE if avail] - 3.7)*1.5",
        "#   stability_prior = financial-bucket base - recent-layoff penalty",
        "# Edit any line by hand to override; re-running fill_priors.py regenerates this file.",
        "",
    ]
    table, partial = [], []
    for name in sorted(rows):
        r = rows[name]
        # Culture and WLB need Glassdoor; stability only needs the funding/profitability
        # bucket. Seed-stage companies routinely have no Glassdoor presence at all, and
        # emitting a made-up 3.0 there would be worse than emitting nothing — the scorer
        # then treats "unknown" as "average", which is a real claim we can't support.
        # So write only what's evidenced, and leave the rest for scoring to default.
        has_gd = r.get("culture_cv") is not None and r.get("wlb") is not None
        stab = stability(r["financial_bucket"], r.get("layoff_year", "none"))
        size = r.get("size", "mid")
        note_bits = []
        if has_gd:
            note_bits += [f"GD{r.get('gd_overall','?')}", f"C&V{r['culture_cv']}",
                          f"WLB{r['wlb']}({r.get('wlb_scope','general')})"]
        else:
            note_bits.append("NO GLASSDOOR DATA — culture/wlb unset on purpose")
            partial.append(name)
        note_bits.append(r["financial_bucket"])
        if str(r.get("layoff_year")) not in ("none", "None", "None"):
            note_bits.append(f"layoff {r['layoff_year']}")
        note_bits.append(f"conf:{r.get('confidence','?')}")
        if r.get("entity_note"):
            note_bits.append(r["entity_note"][:120])
        note = "; ".join(note_bits).replace('"', "'")
        lay = (f", recent_layoffs: {r['layoff_year']}"
               if str(r.get("layoff_year")) not in ("none", "None") else "")
        priors = ""
        if has_gd:
            priors = (f"culture_prior: {calibrate(r['culture_cv'])}, "
                      f"wlb_prior: {calibrate(r['wlb'])}, ")
        out_lines.append(
            f"{name}: {{ size: {size}, {priors}stability_prior: {stab}{lay}, notes: \"{note}\" }}"
        )
        if has_gd:
            table.append((name, r["culture_cv"], calibrate(r["culture_cv"]), r["wlb"],
                          r.get("wlb_scope", "general"), calibrate(r["wlb"]),
                          r["financial_bucket"], r.get("layoff_year", "none"), stab))

    with open(os.path.join(HERE, "company_notes.yaml"), "w") as f:
        f.write("\n".join(out_lines) + "\n")

    print(f"wrote {len(rows)} companies to company_notes.yaml")
    if partial:
        print(f"  {len(partial)} with stability only (no Glassdoor data): {', '.join(partial)}\n")
    else:
        print()
    print(f"{'company':14} {'C&V→cult':>12} {'WLB→wlb':>16} {'stability':>28}")
    print("-" * 74)
    for name, ccv, cult, w, ws, wlb, bucket, lay, stab in table:
        print(f"{name:14} {ccv:>5}→{cult:<5} {w:>5}({ws})→{wlb:<5} "
              f"{bucket:>24}{'/'+str(lay) if str(lay) not in ('none','None') else '':>4}→{stab}")


if __name__ == "__main__":
    main()
