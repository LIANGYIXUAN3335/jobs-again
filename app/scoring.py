"""Heuristic scoring — pure functions over JD text + hand-maintained company notes.
No model, no API key. Sub-scores 0-5; weighted total normalized to 0-100."""
from __future__ import annotations

import datetime
import hashlib
import json
import re
from dataclasses import dataclass

from .config import norm_key
from .filters import _hit
from .filters import _first_hit
from .resume_score import role_family
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
    # `freshness` lives under scoring: for tidiness but never touches a stored score — it is
    # applied at rank time. Tuning it must not trigger a pointless rescore of every role.
    if isinstance(payload.get("scoring"), dict):
        payload["scoring"] = {k: v for k, v in payload["scoring"].items()
                              if k not in ("freshness", "diversify", "location_pref")}   # rank-time knobs — no rescore needed
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


# --- eligibility: can she plausibly WIN this role (separate question from "is it a good job") ---
_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?year", re.I)
# One unified "hard tech gate" vocabulary — low-level/systems depth she does not have.
# Deliberately NOT additive: hitting three of these is still ONE penalty, because the
# candidate-side reality is binary (she either has this background or she doesn't).
_GATE = r"(?<![\w+])C\+\+|\bRust\b|\.NET\b|(?<!\w)C#|\bCUDA\b|\bGPU\b|\bcompiler\b|\bkernel\b|device driver|\bfirmware\b|\bVerilog\b|\bVHDL\b|\bRTL\b|\bembedded\b|\bassembly language\b"
_HARD_GATE = re.compile(_GATE, re.I)
# A stated bar ("expertise in CUDA") vs a passing mention in a laundry list.
_HARD_REQUIRED = re.compile(
    r"(?:expert(?:ise)?|strong|proficien\w*|advanced|deep|solid|extensive|mastery)"
    rf"[^.\n]{{0,60}}?(?:{_GATE})"
    rf"|(?:{_GATE})[^.\n]{{0,40}}?(?:expert|required|must)", re.I)
_ALT_LANG = re.compile(r"\b(?:Go|Golang|Python|Java)\b", re.I)

# Unfilled ATS template postings (seen on Twilio's greenhouse board): the JD still carries
# literal authoring placeholders, so there is nothing real to match a résumé against and the
# posting is probably not an active search. Penalized (sinks in review), NOT dropped —
# owner's call: keep them visible so a few can still leak through to apply.
_TEMPLATE_JD = re.compile(
    r"<\s*job\s*title\s*>|&lt;\s*job\s*title\s*&gt;|\[\s*insert\s+[a-z ]{0,30}\]"
    r"|\{\{\s*[a-z_ ]{1,30}\s*\}\}", re.I)

# --- geography: she is US-based; a Tel Aviv or Hanoi posting is not an opportunity ---
_US_STATES = (r"Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|"
              r"Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|"
              r"Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|"
              r"New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|"
              r"Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|"
              r"Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming")
_US_HINT = re.compile(rf"\bUSA?\b|\bU\.S\.|United States|{_US_STATES}|"
                      r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DC|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|"
                      r"MI|MN|MO|MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA|"
                      r"VT|WA|WI|WV|WY)\b", re.I)
_FOREIGN = re.compile(
    r"\b(?:India|Israel|China|Taiwan|Japan|Korea|Vietnam|Singapore|Germany|Poland|"
    r"United Kingdom|Ireland|France|Netherlands|Spain|Italy|Sweden|Switzerland|Canada|"
    r"Australia|Brazil|Mexico|Romania|Czech|Hungary|Denmark|Norway|Finland|Portugal|Belgium|"
    r"Austria|Greece|Turkey|Egypt|Argentina|Chile|Colombia|Philippines|Indonesia|Thailand|"
    r"Malaysia|Hong Kong|New Zealand|South Africa|Costa Rica|Bulgaria|Serbia|Ukraine|"
    r"Lithuania|Estonia|Latvia|Slovakia|Slovenia|Croatia|Luxembourg|Bengaluru|Bangalore|"
    r"Hyderabad|Gurgaon|Noida|Tel Aviv|Yokneam|Herzliya|Shenzhen|Hsinchu|Ho Chi Minh|Hanoi|"
    # Gulf states were missing entirely (found 2026-09-10: a Doha posting reached 'applied')
    r"Qatar|Doha|United Arab Emirates|UAE|Dubai|Abu Dhabi|Saudi Arabia|Riyadh)\b",
    re.I)


def is_non_us(location: str, apply_url: str) -> bool:
    """True only when a posting is positively foreign AND offers no US location at all.
    Multi-site reqs ('Remote - Canada · San Francisco', 'Dublin, Ireland (Mountain View)')
    are real opportunities and must survive. Uncertain stays False — a false drop silently
    hides a job forever, which is far worse than one stray row in the list."""
    loc = location or ""
    m = re.search(r"/job/([^/]+)", apply_url or "")
    url_token = m.group(1).replace("-", " ").replace("_", " ") if m else ""
    blob = f"{loc} {url_token}"
    # Unambiguous US cities only — 'San Jose', 'Dublin', 'Ontario', 'Paris', 'Vienna' and
    # 'Cambridge' all exist abroad too, so they must carry a state/US marker to count.
    us = bool(_US_HINT.search(blob)) or bool(re.search(
        r"\b(?:San Francisco|Mountain View|Sunnyvale|Santa Clara|Palo Alto|Menlo Park|Cupertino|"
        r"Seattle|Redmond|Bellevue|Austin|Boston|Chicago|Los Angeles|San Diego|Denver|Atlanta|"
        r"Dallas|Houston|Phoenix|Portland|New York|NYC|Pittsburgh|Raleigh|Durham|Miami|"
        r"Philadelphia|Arlington|Reston|Herndon|Boulder|Nashville|Detroit|Minneapolis|"
        r"Ann Arbor|Columbus|Cincinnati|Kansas City|St\.? Louis|O'?Fallon|Tempe|Scottsdale|"
        r"Plano|Frisco|Sacramento|Santa Monica|Culver City|Irvine|Bentonville|Hillsboro|"
        r"Folsom|Westford|Marlborough|Chelmsford)\b", blob, re.I))
    if us or re.match(r"\s*(?:US|USA|United States)\b", url_token, re.I):
        return False
    return bool(_FOREIGN.search(blob))


def years_required(jd_text: str) -> int | None:
    """The effective years bar a recruiter would screen on: the LARGEST credible 'N+ years'
    in the JD. Figures outside 1–15 are noise (company age, '25 years of NVIDIA'), not a bar."""
    yrs = [int(m.group(1)) for m in _YEARS.finditer(jd_text or "")]
    yrs = [y for y in yrs if 1 <= y <= 15]
    return max(yrs) if yrs else None


def _eligibility(text: str, jd_text: str, sc: dict) -> tuple[float, list[tuple[str, float]], bool]:
    """Returns (penalty_points, caution_entries, hard_drop). Penalties are in 0-100 points and
    are subtracted from the weighted total AFTER normalization, so they stay legible."""
    el = sc.get("eligibility") or {}
    if not el.get("enabled", True):
        return 0.0, [], False
    pen, notes_out, drop = 0.0, [], False

    yr = years_required(jd_text)
    drop_at = float(el.get("years_drop_at", 10))       # this many years or more -> drop
    soft = float(el.get("years_penalty_over", 6))
    if yr is not None:
        if yr >= drop_at:
            drop = True
            notes_out.append((f"needs {yr}+ yrs", -99.0))
        elif yr > soft:
            p = float(el.get("years_penalty", 10))
            pen += p
            notes_out.append((f"needs {yr}+ yrs", -p))

    # ONE unified hard-gate penalty. C++ AND CUDA AND kernel still costs the same as any one
    # of them — stacking would punish a JD for being verbose, not for being a worse fit.
    if _HARD_GATE.search(jd_text or ""):
        full = float(el.get("hard_gate_penalty", el.get("hard_lang_penalty", 6)))
        if _HARD_REQUIRED.search(jd_text or "") or not _ALT_LANG.search(jd_text or ""):
            pen += full
            notes_out.append(("hard tech gate (C++/CUDA/kernel…)", -full))
        else:
            pen += full / 2
            notes_out.append(("hard tech gate mentioned", -full / 2))

    # Unfilled template posting: heavy penalty so it sinks to the bottom of review, but no
    # hard drop — it stays visible and applyable if the owner wants to take the flyer.
    if _TEMPLATE_JD.search(jd_text or ""):
        tp = float(el.get("template_jd_penalty", 25))
        pen += tp
        notes_out.append(("unfilled template JD (placeholders in posting)", -tp))
    return pen, notes_out, drop


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

    # Eligibility is a SEPARATE question from desirability — it answers "can I win this",
    # not "is this a good job". Applied after normalization and always spelled out in
    # `caution`, so a docked score is never a mystery.
    pen, el_notes, hard_drop = _eligibility(text, posting.jd_text or "", sc)
    if (sc.get("eligibility") or {}).get("drop_non_us", True) and is_non_us(
            getattr(posting, "location", "") or "", getattr(posting, "apply_url", "") or ""):
        hard_drop = True
        el_notes.append(("outside the US", -99.0))
    # Wrong role family (consultant / strategist / sales engineer / reporter / …): the intake
    # filter deliberately biases to recall — "Professional Services Consultant" survives it via
    # the 'services' substring, a fintech Reporter via 'fintech'. That is intake's job; being
    # the gate is THIS layer's job. Without it these reach the review queue and auto-tailor
    # (78 wrong-family roles got real tailored résumés before this check existed).
    if sc.get("drop_non_eng_titles", True) and role_family(posting.title, cfg) == "non_eng":
        hard_drop = True
        el_notes.append(("non-engineering role family", -99.0))
    # Clearance/citizenship gate, re-checked at SCORE time with the full JD. The intake
    # filter (filters.py) runs before lazy-JD adapters (workday/smartrecruiters/rippling/
    # avature) have fetched the body, so citizenship requirements buried in the JD slip
    # past it — this is the net that catches those. Same config list keeps the two in sync.
    fl = cfg.get("filters") or {}
    if fl.get("drop_clearance", True):
        hit = _first_hit(fl.get("clearance_exclude", []), text)
        if hit:
            hard_drop = True
            el_notes.append((f"clearance/citizenship: {hit}", -99.0))
    neg.extend(el_notes)
    weighted = round(max(0.0, weighted - pen), 1)

    th = cfg.get("verdict_thresholds") or {}
    if hard_drop:
        verdict = "drop"                       # over the years ceiling — not worth applying
    elif weighted >= float(th.get("strong", 70)):
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


# --- freshness: a RANKING preference, deliberately not part of weighted_score ---
# A job's four-dimension score answers "is this a good job", and that does not change as the
# posting ages — but how urgently it is worth looking at does. Keeping the two apart means:
# (a) weighted_score stays comparable over time, so funnel.py's "which score bands actually
# convert" calibration is not polluted by when a role happened to be scanned; (b) the bonus
# is recomputed on every page load, so it decays on its own without a nightly rescore, which
# a stored bonus would need in order not to go stale.

def freshness_tuning(cfg: dict) -> dict:
    f = ((cfg.get("scoring") or {}).get("freshness") or {})
    return {
        "enabled": bool(f.get("enabled", True)),
        "bonus": float(f.get("bonus", 10)),
        "full_days": int(f.get("full_days", 2)),
        "zero_days": int(f.get("zero_days", 14)),
        "infer_from_first_seen": bool(f.get("infer_from_first_seen", True)),
        "inferred_factor": float(f.get("inferred_factor", 1.0)),
    }


def freshness(posted_at: str | None, first_seen: str | None, cfg: dict,
              today: str | None = None, allow_inferred: bool = True) -> dict:
    """Points to add to a role's RANK score for being newly posted.

    Full `bonus` while the posting is <= full_days old, then a straight line down to 0 at
    zero_days. Never negative: an old posting is not penalised, it just stops being boosted.

    `posted_at` is what the board said. `first_seen` is only a proxy for the two boards that
    publish no date (Rippling, Avature) — accurate for roles that appeared while the daily
    scan was already running, wrong for anything swept up in a first-ever scan of a board,
    which is why it is separated out in the return value and can be down-weighted.

    `allow_inferred=False` disables the first_seen fallback entirely — pass it for
    hand-captured / manually-added roles, where first_seen is the moment the OWNER clicked
    capture, which says nothing about when the job was posted (a months-old posting captured
    today is NOT fresh).
    """
    t = freshness_tuning(cfg)
    out = {"bonus": 0.0, "age_days": None, "posted_known": bool(posted_at), "date": None}
    if not t["enabled"]:
        return out
    day = (posted_at or "")[:10]
    inferred = False
    if not day and t["infer_from_first_seen"] and allow_inferred:
        day = (first_seen or "")[:10]
        inferred = True
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
        return out
    try:
        d0 = datetime.date.fromisoformat(day)
        d1 = datetime.date.fromisoformat(today) if today else datetime.date.today()
    except ValueError:
        return out
    age = (d1 - d0).days
    out["date"] = day
    out["age_days"] = age
    if age <= t["full_days"]:                     # includes clock-skew negatives
        raw = t["bonus"]
    elif age >= t["zero_days"]:
        raw = 0.0
    else:
        span = max(1, t["zero_days"] - t["full_days"])
        raw = t["bonus"] * (t["zero_days"] - age) / span
    if inferred:
        raw *= t["inferred_factor"]
    out["bonus"] = round(max(0.0, raw), 1)
    return out


# ── Location preference (owner 2026-09-22: 优先远程或湾区) ─────────────────────────────────
# Rank-only, like freshness: weighted_score / verdict never move. A remote role, or one in the
# SF Bay Area, gets `bonus` on rank_score, so it leads its company's pool and the company ring.
# Anything else — including Workday's opaque "3 Locations" — gets nothing and is never
# penalised. The same classifier drives the review page's "remote or Bay Area" filter.
_BAY_PLAIN = (
    r"san francisco|\bsf\b|silicon valley|san jose|santa clara|sunnyvale|mountain view|"
    r"palo alto|menlo park|redwood city|san mateo|foster city|burlingame|san bruno|"
    r"south san francisco|daly city|cupertino|milpitas|los altos|los gatos|campbell|saratoga|"
    r"oakland|berkeley|emeryville|walnut creek|pleasanton|san ramon|hayward|san carlos|"
    r"stanford|sausalito|mill valley|san rafael|novato|larkspur|fremont|livermore|"
    r"san leandro|petaluma|half moon bay|pacifica|millbrae|redwood shores"
)
# names that exist elsewhere too (Newark NJ, Richmond VA, Dublin IE, Belmont MA…) — only with
# a California marker within a few words
_BAY_NEEDS_CA = r"newark|richmond|dublin|concord|alameda|brisbane|albany|martinez|vallejo|belmont|union city"
_BAY_RE = re.compile(rf"\b(?:{_BAY_PLAIN})\b", re.I)
_BAY_CA_RE = re.compile(rf"\b(?:{_BAY_NEEDS_CA})\b[^;|·/]{{0,25}}\b(?:CA|Calif\w*)\b", re.I)
_BAY_AREA_RE = re.compile(r"\bbay area\b", re.I)
_NOT_SF_BAY = re.compile(r"tampa|monterey|chesapeake|green bay|bay city|back bay", re.I)
_REMOTE_RE = re.compile(r"\bremote\b|work from home|\bwfh\b|anywhere in the (?:us|u\.s\.|united states)", re.I)


def location_kind(location: str | None, is_remote) -> str | None:
    """'remote' | 'bay' | None for a posting's location string (+ the board's remote flag)."""
    loc = location or ""
    # "Remote - Canada" is remote for Canadians: a foreign country named without any US marker
    # is not a location this candidate can take (found 2026-09-23 on a Twilio row in the
    # Bay-Area/remote queue).
    if _FOREIGN.search(loc) and not _US_HINT.search(loc):
        return None
    if is_remote or _REMOTE_RE.search(loc):
        return "remote"
    if _BAY_RE.search(loc) or _BAY_CA_RE.search(loc):
        return "bay"
    if _BAY_AREA_RE.search(loc) and not _NOT_SF_BAY.search(loc):
        return "bay"
    return None


def location_pref(location: str | None, is_remote, cfg: dict) -> dict:
    """{kind, bonus}: the rank lift a remote / Bay Area role earns under scoring.location_pref."""
    lp = (cfg.get("scoring") or {}).get("location_pref") or {}
    kind = location_kind(location, is_remote)
    if not kind or not lp.get("enabled", True):
        return {"kind": kind, "bonus": 0.0}
    wanted = {"remote": bool(lp.get("remote", True)), "bay": bool(lp.get("bay_area", True))}
    return {"kind": kind, "bonus": float(lp.get("bonus", 8)) if wanted[kind] else 0.0}


# ── Sponsorship dead-ends (workspace knob, owner 2026-09-23) ──────────────────────────────
# A candidate who NEEDS visa sponsorship (F-1 OPT → H-1B) should never see a posting whose JD
# says the employer will not sponsor. Read at view time from roles.jd_text, so the shared
# jobs.db keeps one score per role and each workspace applies its own eligibility.
_NO_SPONSOR = re.compile(
    # "(?!\s+(?:always\s+)?guarantee)": "We can't guarantee sponsorship for every role, but…" (Wispr Flow,
    # Exa) is a sponsor hedging, not a refusal — without the lookahead it hid every Wispr Flow role.
    r"\b(?:not|no|never|unable|cannot|can't|won't|will not|does not|do not|isn't|is not|are not)\b(?!\s+(?:always\s+)?guarantee)"
    r"[^.;\n]{0,40}?\b(?:sponsor(?:ship)?|visa sponsorship|h-?1b|immigration (?:support|sponsorship))"
    # 2026-09-28: "without sponsorship for an export license" is Cloudflare's export-control boilerplate
    # (every one of its ~100 US JDs), not a visa statement — it hid the whole company from any
    # needs_sponsorship workspace. Palo Alto Networks carries a similar clause.
    r"|\bwithout (?:the need for |requiring |any )?(?:visa |employment |immigration )?sponsorship\b(?! for an? export)"
    r"|\bsponsorship (?:is )?not (?:available|offered|provided)\b"
    r"|\bmust be (?:legally )?authori[sz]ed to work[^.;\n]{0,60}\bwithout\b"
    r"|\bno visa sponsorship\b|\bnot eligible for sponsorship\b", re.I)


def no_sponsorship_jd(jd_text: str | None) -> bool:
    """True when the JD states the employer will not sponsor a work visa."""
    return bool(jd_text) and bool(_NO_SPONSOR.search(jd_text))
