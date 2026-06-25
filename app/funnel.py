"""Did any of it work? Conversion from applied → reply → interview → offer, sliced by the
things the system decides on its own.

The four-dimension role score and the résumé match score are both hand-tuned guesses. After a
few hundred applications there is enough evidence to check them against reality, and exactly one
question matters: **do high-scored applications get answered more often than low-scored ones?**
If they don't, the weights in config.yaml are decoration and the per-company cap is prioritising
noise. That answer only exists once outcomes are recorded — see `app/outcomes.py`.

Honesty rules baked in, because a funnel is the easiest place in a job search to fool yourself:
  - A band with fewer than `min_n` applications is printed but explicitly marked too small to
    read. No rate is ever reported as if 3 applications settled anything.
  - Silence is not counted as a rejection. "Quiet" (no reply yet, N+ days) is its own column,
    and a role that has simply not been heard from stays `in_progress`.
  - Applications younger than the response window are held out of the rate entirely — counting
    yesterday's applications as "no reply" would drag every rate toward zero.

    python -m app.funnel                  # the whole board
    python -m app.funnel --by company     # score | match | company | ats | seniority | remote
"""
from __future__ import annotations

import argparse
import datetime
import re
import statistics
import sys

from . import config as cfgmod
from . import store

# Defaults, overridable in config.yaml under `funnel:`.
#   response_window_days — an application younger than this hasn't had a fair chance to be
#     answered, so it is excluded from every rate (still counted under `pending`).
#   quiet_days — when an unanswered application starts counting as gone quiet (followup.py
#     uses its own, earlier threshold for when to nudge).
#   min_n — below this many judgeable applications, a bucket's rate is noise; label it as such.
DEFAULTS = {"response_window_days": 21, "quiet_days": 21, "min_n": 20}


def tuning(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("funnel") or {})}


_PDF_SCORE = re.compile(r" - (\d{1,3})\.pdf$")


def _match_score(path: str | None) -> int | None:
    """The résumé match score, read back off the filename the tailoring step writes
    ("Casey Example - Stripe - Money as a Service [6176758] - 80.pdf"). That filename is the
    system's own convention and the only place the per-role match score is persisted."""
    if not path:
        return None
    m = _PDF_SCORE.search(path)
    return int(m.group(1)) if m else None


def _band(v, edges: list[int], unit: str = "") -> str:
    if v is None:
        return "unknown"
    lo = None
    for e in edges:
        if v < e:
            return f"<{e}{unit}" if lo is None else f"{lo}–{e - 1}{unit}"
        lo = e
    return f"{edges[-1]}+{unit}"


def load(cfg: dict, conn) -> list[dict]:
    """Every application that actually went out, with what has happened to it since."""
    tune = tuning(cfg)
    rows = [dict(r) for r in conn.execute(
        "SELECT r.job_id, r.company, r.ats, r.title, r.seniority, r.is_remote,"
        "       r.weighted_score, r.verdict,"
        "       s.status, s.applied_date, s.stage, s.outcome,"
        "       s.first_response_at, s.last_contact_at, s.tailored_resume_path"
        " FROM jobs.roles r JOIN role_state s ON s.job_id = r.job_id"
        " WHERE s.applied_date IS NOT NULL")]
    today = datetime.date.fromisoformat(store.la_today())
    out = []
    for r in rows:
        try:
            applied = datetime.date.fromisoformat(r["applied_date"])
        except (TypeError, ValueError):
            continue
        age = (today - applied).days
        rank = store.STAGE_RANK.get(r["stage"] or "applied", 0)
        responded = bool(r["first_response_at"]) or rank >= store.STAGE_RANK["responded"] \
            or (r["outcome"] or "") in ("rejected", "offer", "hired", "declined")
        days_to_reply = None
        if r["first_response_at"]:
            try:
                days_to_reply = (datetime.date.fromisoformat(r["first_response_at"]) - applied).days
            except ValueError:
                pass
        out.append({
            **r,
            "age_days": age,
            "match": _match_score(r["tailored_resume_path"]),
            "responded": responded,
            "interviewed": rank >= store.STAGE_RANK["interview"],
            "offered": rank >= store.STAGE_RANK["offer"] or (r["outcome"] or "") in ("offer", "hired"),
            "rejected": (r["outcome"] or "") == "rejected",
            "days_to_reply": days_to_reply,
            "mature": age >= tune["response_window_days"],  # old enough for its silence to mean something
            "quiet": (not responded) and age >= tune["quiet_days"],
        })
    return out


_DIMENSIONS = {
    "score":     lambda a: _band(round(a["weighted_score"] or 0), [60, 70, 80, 90]),
    "match":     lambda a: _band(a["match"], [70, 80, 85, 90]),
    "company":   lambda a: a["company"],
    "ats":       lambda a: a["ats"] or "?",
    "seniority": lambda a: a["seniority"] or "?",
    "remote":    lambda a: "remote" if a["is_remote"] else "onsite/hybrid",
    "verdict":   lambda a: a["verdict"] or "?",
}


def by(apps: list[dict], dim: str, min_n: int = 20) -> list[dict]:
    """Conversion per bucket of `dim`, computed only over applications old enough to judge."""
    key = _DIMENSIONS[dim]
    buckets: dict[str, list[dict]] = {}
    for a in apps:
        buckets.setdefault(key(a), []).append(a)
    out = []
    for name, group in buckets.items():
        mature = [a for a in group if a["mature"]]
        resp = [a for a in mature if a["responded"]]
        lags = [a["days_to_reply"] for a in group if a["days_to_reply"] is not None]
        out.append({
            "bucket": name,
            "applied": len(group),
            "pending": len(group) - len(mature),
            "mature": len(mature),
            "responded": len(resp),
            "interviewed": sum(1 for a in mature if a["interviewed"]),
            "offered": sum(1 for a in mature if a["offered"]),
            "rejected": sum(1 for a in mature if a["rejected"]),
            "quiet": sum(1 for a in group if a["quiet"]),
            "response_rate": (100.0 * len(resp) / len(mature)) if mature else None,
            "median_days_to_reply": statistics.median(lags) if lags else None,
            "enough": len(mature) >= min_n,
        })
    order = ["score", "match", "verdict"]
    if dim in order:
        out.sort(key=lambda d: d["bucket"])
    else:
        out.sort(key=lambda d: -d["applied"])
    return out


def overall(apps: list[dict], window: int = 21) -> dict:
    mature = [a for a in apps if a["mature"]]
    resp = [a for a in mature if a["responded"]]
    lags = [a["days_to_reply"] for a in apps if a["days_to_reply"] is not None]
    return {
        "applied": len(apps),
        "mature": len(mature),
        "pending": len(apps) - len(mature),
        "responded": len(resp),
        "interviewed": sum(1 for a in mature if a["interviewed"]),
        "offered": sum(1 for a in mature if a["offered"]),
        "rejected": sum(1 for a in mature if a["rejected"]),
        "quiet": sum(1 for a in apps if a["quiet"]),
        "response_rate": (100.0 * len(resp) / len(mature)) if mature else None,
        "median_days_to_reply": statistics.median(lags) if lags else None,
        "companies": len({a["company"] for a in apps}),
        "window": window,
    }


def score_signal(apps: list[dict], min_n: int = 20) -> dict:
    """The headline question: is response rate actually higher for higher-scored roles?

    Reported as the gap between the top and bottom half of applications by score, together with
    the sample size behind it — the gap alone would look authoritative on 8 applications.
    """
    mature = sorted((a for a in apps if a["mature"] and a["weighted_score"] is not None),
                    key=lambda a: a["weighted_score"])
    if len(mature) < 2 * min_n:
        return {"verdict": "not enough data", "n": len(mature), "need": 2 * min_n}
    half = len(mature) // 2
    lo, hi = mature[:half], mature[half:]
    lo_r = 100.0 * sum(a["responded"] for a in lo) / len(lo)
    hi_r = 100.0 * sum(a["responded"] for a in hi) / len(hi)
    gap = hi_r - lo_r
    return {
        "verdict": "score predicts replies" if gap >= 5 else
                   "score does not predict replies" if gap <= -5 else "no measurable effect",
        "n": len(mature), "low_half_rate": lo_r, "high_half_rate": hi_r, "gap": gap,
        "cutoff": mature[half]["weighted_score"],
    }


def report(cfg: dict, conn, dims=("score", "match", "company", "ats")) -> dict:
    tune = tuning(cfg)
    apps = load(cfg, conn)
    return {"overall": overall(apps, tune["response_window_days"]),
            "signal": score_signal(apps, tune["min_n"]),
            "by": {d: by(apps, d, tune["min_n"]) for d in dims},
            "tuning": tune, "apps": apps}


def _fmt_rate(row: dict) -> str:
    if row["response_rate"] is None:
        return "  —  "
    s = f"{row['response_rate']:5.1f}%"
    return s if row["enough"] else s + " ?"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--by", default="score,match,company,ats",
                    help="comma-separated: " + " ".join(_DIMENSIONS))
    args = ap.parse_args(argv)
    dims = [d.strip() for d in args.by.split(",") if d.strip() in _DIMENSIONS]

    cfg = cfgmod.load_config()
    conn = store.connect_view(cfg)
    try:
        rep = report(cfg, conn, dims)
    finally:
        conn.close()
    o, tune = rep["overall"], rep["tuning"]
    if not o["applied"]:
        print("no applications recorded yet (nothing has applied_date set)")
        return 1

    print(f"\n{o['applied']} applications across {o['companies']} companies")
    print(f"  {o['mature']} old enough to judge (≥{o['window']}d) · {o['pending']} still too fresh")
    print(f"  replied {o['responded']} · interview+ {o['interviewed']} · offer {o['offered']} "
          f"· rejected {o['rejected']} · quiet {o['quiet']}")
    if o["response_rate"] is not None:
        print(f"  response rate {o['response_rate']:.1f}%"
              + (f" · median {o['median_days_to_reply']:.0f}d to first reply"
                 if o["median_days_to_reply"] is not None else ""))

    s = rep["signal"]
    print("\ndoes the role score predict a reply?")
    if s["verdict"] == "not enough data":
        print(f"  not enough data yet — {s['n']} judgeable applications, need ~{s['need']}."
              f"\n  Record outcomes (`python -m app.outcomes --apply`) and this answers itself.")
    else:
        print(f"  {s['verdict']}: bottom half {s['low_half_rate']:.1f}% vs top half "
              f"{s['high_half_rate']:.1f}% (split at {s['cutoff']:.0f}, n={s['n']})")

    for dim in dims:
        print(f"\nby {dim}")
        print(f"  {'bucket':22} {'applied':>7} {'judgeable':>9} {'replied':>7} {'intv':>5} "
              f"{'offer':>5} {'quiet':>5}   reply-rate")
        for r in rep["by"][dim][:20]:
            print(f"  {r['bucket'][:22]:22} {r['applied']:7d} {r['mature']:9d} {r['responded']:7d} "
                  f"{r['interviewed']:5d} {r['offered']:5d} {r['quiet']:5d}   {_fmt_rate(r)}")
    print(f"\n  ? = fewer than {tune['min_n']} judgeable applications in that bucket; "
          f"the rate is not yet meaningful.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
