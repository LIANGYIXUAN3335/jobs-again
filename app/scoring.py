"""Heuristic scoring — pure functions over JD text + hand-maintained company notes.
No model, no API key. Sub-scores 0-5; weighted total normalized to 0-100."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .config import norm_key
from .filters import _hit
from .sources.base import Posting

MAX_RAW = None  # computed from weights at call time


@dataclass
class Score:
    s_stability: float
    s_culture: float
    s_depth: float
    s_wlb: float
    weighted: float          # 0-100
    verdict: str             # strong | consider | weak
    why: str
    caution: str


def config_hash(cfg: dict) -> str:
    """Any change to weights/thresholds/keyword maps triggers a full rescore."""
    payload = {k: cfg.get(k) for k in ("weights", "verdict_thresholds", "scoring")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def jd_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _clamp(x: float, lo: float = 0.0, hi: float = 5.0) -> float:
    return max(lo, min(hi, x))


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _truthy_layoffs(v) -> bool:
    return bool(v) and str(v).lower() not in ("no", "false", "none", "tbd", "0")


def score(posting: Posting, notes: dict, cfg: dict) -> Score:
    sc = cfg.get("scoring") or {}
    text = f"{posting.title}\n{posting.jd_text}".lower()
    pos: list[tuple[str, float]] = []   # (label, weight) — for `why`
    neg: list[tuple[str, float]] = []   # for `caution`

    # --- depth: base + keyword evidence in the JD ---
    depth = float(sc.get("depth_base", 2.5))
    for kw, w in (sc.get("depth") or {}).items():
        if _hit(kw.strip(), text):
            depth += float(w)
            (pos if w > 0 else neg).append((kw.strip(), float(w)))
    s_depth = _clamp(depth)

    # --- wlb / culture: company prior dominates; JD keywords nudge, capped ±1 ---
    cap = float(sc.get("jd_adjust_cap", 1.0))

    def prior_plus_keywords(sub: str) -> float:
        base = _num(notes.get(f"{sub}_prior"))
        if base is not None and base != 3.0:
            bucket = pos if base > 3.0 else neg
            bucket.append((f"{sub} prior {base:g}", base - 3.0))
        adj = 0.0
        for kw, w in (sc.get(sub) or {}).items():
            if _hit(kw.strip(), text):
                adj += float(w)
                (pos if w > 0 else neg).append((kw.strip(), float(w)))
        if sub == "wlb" and posting.is_remote:
            bonus = float(sc.get("remote_wlb_bonus", 0.4))
            adj += bonus
            pos.append(("remote role", bonus))
        return _clamp((base if base is not None else 3.0) + _clamp(adj, -cap, cap))

    s_wlb = prior_plus_keywords("wlb")
    s_culture = prior_plus_keywords("culture")

    # --- stability: company financial health (notes) + role's business-criticality (JD).
    # Revenue-proximate / core work is harder to cut; cost-center work is first to go. ---
    st = _num(notes.get("stability_prior"))
    layoffs = notes.get("recent_layoffs")
    if st is None:
        st = 3.0
        if notes.get("size") == "large":
            st += 0.5
            pos.append(("large company", 0.5))
        if _truthy_layoffs(layoffs):
            st -= 1.5
    if _truthy_layoffs(layoffs):
        neg.append((f"recent layoffs ({layoffs})", -1.5))
    st_adj = 0.0
    for kw, w in (sc.get("stability") or {}).items():
        if _hit(kw.strip(), text):
            st_adj += float(w)
            (pos if w > 0 else neg).append((kw.strip(), float(w)))
    s_stability = _clamp(st + _clamp(st_adj, -cap, cap))

    # --- weighted total, normalized to 0-100 ---
    w = cfg.get("weights") or {}
    ws, wc, wd, ww = (float(w.get(k, 1.0)) for k in ("stability", "culture", "depth", "wlb"))
    raw = ws * s_stability + wc * s_culture + wd * s_depth + ww * s_wlb
    weighted = round(raw / (5.0 * (ws + wc + wd + ww)) * 100, 1)

    th = cfg.get("verdict_thresholds") or {}
    if weighted >= float(th.get("strong", 70)):
        verdict = "strong"
    elif weighted >= float(th.get("consider", 60)):
        verdict = "consider"
    else:
        verdict = "weak"

    why = " · ".join(l for l, _ in sorted(pos, key=lambda t: -t[1])[:4])
    caution = " · ".join(l for l, _ in sorted(neg, key=lambda t: t[1])[:4])
    return Score(
        s_stability=round(s_stability, 1),
        s_culture=round(s_culture, 1),
        s_depth=round(s_depth, 1),
        s_wlb=round(s_wlb, 1),
        weighted=weighted,
        verdict=verdict,
        why=why,
        caution=caution,
    )


def notes_for(company: str, all_notes: dict[str, dict]) -> dict:
    return all_notes.get(norm_key(company), {})
