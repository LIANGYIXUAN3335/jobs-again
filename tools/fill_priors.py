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
    rows = {}
    for f in sorted(glob.glob(os.path.join(HERE, "scratch_priors", "*.json"))):
        for r in json.load(open(f)):
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
    table = []
    for name in sorted(rows):
        r = rows[name]
        cult = calibrate(r["culture_cv"])
        wlb = calibrate(r["wlb"])
        stab = stability(r["financial_bucket"], r["layoff_year"])
        size = r.get("size", "mid")
        note_bits = [f"GD{r['gd_overall']}", f"C&V{r['culture_cv']}", f"WLB{r['wlb']}({r['wlb_scope']})"]
        if str(r["layoff_year"]) not in ("none", "None"):
            note_bits.append(f"layoff {r['layoff_year']}")
        note_bits.append(f"conf:{r.get('confidence','?')}")
        note = "; ".join(note_bits)
        lay = f", recent_layoffs: {r['layoff_year']}" if str(r["layoff_year"]) not in ("none", "None") else ""
        out_lines.append(
            f"{name}: {{ size: {size}, culture_prior: {cult}, wlb_prior: {wlb}, "
            f"stability_prior: {stab}{lay}, notes: \"{note}\" }}"
        )
        table.append((name, r["culture_cv"], cult, r["wlb"], r["wlb_scope"], wlb,
                      r["financial_bucket"], r["layoff_year"], stab))

    with open(os.path.join(HERE, "company_notes.yaml"), "w") as f:
        f.write("\n".join(out_lines) + "\n")

    print(f"wrote {len(rows)} companies to company_notes.yaml\n")
    print(f"{'company':14} {'C&V→cult':>12} {'WLB→wlb':>16} {'stability':>28}")
    print("-" * 74)
    for name, ccv, cult, w, ws, wlb, bucket, lay, stab in table:
        print(f"{name:14} {ccv:>5}→{cult:<5} {w:>5}({ws})→{wlb:<5} "
              f"{bucket:>24}{'/'+str(lay) if str(lay) not in ('none','None') else '':>4}→{stab}")


if __name__ == "__main__":
    main()
