from app import config as cfgmod
from app import scoring
from app.sources.base import Posting

CFG = cfgmod.load_config()


def mk(jd, title="Software Engineer", remote=False):
    return Posting(
        ats="greenhouse", company="X", slug="x", raw_id="1", title=title,
        location="San Jose, CA", jd_text=jd, apply_url="", is_remote=remote,
    )


DEEP_JD = ("You will build distributed systems with strong consistency, Kafka "
           "pipelines, a payments ledger, low latency and high throughput services "
           "with fault tolerance and concurrency control.")
SHALLOW_JD = "You will build React components with CSS and our design system in Figma."


def test_depth_rewards_backend_keywords():
    deep = scoring.score(mk(DEEP_JD), {}, CFG)
    shallow = scoring.score(mk(SHALLOW_JD), {}, CFG)
    assert deep.s_depth > 4.0
    assert shallow.s_depth < 2.5
    assert deep.weighted > shallow.weighted
    assert "ledger" in deep.why or "distributed" in deep.why


def test_layoffs_penalty_and_caution():
    notes = {"size": "large", "recent_layoffs": 2026}
    s = scoring.score(mk(DEEP_JD), notes, CFG)
    assert s.s_stability == 2.4  # 3.0 + 0.5 (large) - 1.5 (layoffs) + 0.4 (payments)
    assert "layoffs" in s.caution


def test_stability_prior_replaces_notes_but_keeps_jd_signal():
    s = scoring.score(mk(DEEP_JD), {"stability_prior": 2.5, "recent_layoffs": 2026}, CFG)
    assert s.s_stability == 2.9


def test_wlb_prior_dominates_and_jd_capped():
    happy_jd = DEEP_JD + " flexible hours, work-life balance, remote-first, async."
    s = scoring.score(mk(happy_jd, remote=True), {"wlb_prior": 1.5}, CFG)
    assert s.s_wlb <= 2.5  # prior 1.5 + JD adjustments capped at +1


def test_neutral_unknown_company():
    s = scoring.score(mk("Software engineer role."), {}, CFG)
    assert s.s_stability == 3.0 and s.s_culture == 3.0 and s.s_wlb == 3.0


def test_verdicts_span():
    strong = scoring.score(mk(DEEP_JD, remote=True), {"size": "large"}, CFG)
    weak = scoring.score(mk(SHALLOW_JD), {"recent_layoffs": True}, CFG)
    assert strong.verdict in ("strong", "consider")
    assert weak.verdict == "weak"


def test_config_hash_tracks_scoring_changes():
    h1 = scoring.config_hash(CFG)
    tweaked = {**CFG, "scoring": {**CFG["scoring"], "depth": {**CFG["scoring"]["depth"], "zookeeper": 0.4}}}
    assert scoring.config_hash(tweaked) != h1
    assert scoring.config_hash({**CFG, "companies": []}) == h1  # non-scoring keys ignored


# --- freshness: a ranking boost, deliberately outside weighted_score ---

def test_freshness_full_bonus_inside_the_window_then_decays_to_zero():
    at = lambda d: scoring.freshness(d, None, CFG, today="2026-07-24")   # noqa: E731
    assert at("2026-07-24")["bonus"] == 10.0    # posted today
    assert at("2026-07-22")["bonus"] == 10.0    # 2 days = still the full bonus
    assert at("2026-07-21")["bonus"] < 10.0     # 3 days = decaying
    assert at("2026-07-17")["bonus"] < at("2026-07-21")["bonus"]
    assert at("2026-07-10")["bonus"] == 0.0     # 14 days = expired
    assert at("2025-01-01")["bonus"] == 0.0     # never negative


def test_freshness_falls_back_to_first_seen_but_flags_it():
    known = scoring.freshness("2026-07-23", "2026-01-01", CFG, today="2026-07-24")
    assert known["bonus"] == 10.0 and known["posted_known"] is True
    inferred = scoring.freshness(None, "2026-07-23T10:00:00-07:00", CFG, today="2026-07-24")
    assert inferred["bonus"] == 10.0 and inferred["posted_known"] is False


def test_freshness_ignores_undatable_rows():
    assert scoring.freshness(None, None, CFG, today="2026-07-24")["bonus"] == 0.0
    assert scoring.freshness("Immediately", "soon", CFG, today="2026-07-24")["bonus"] == 0.0


def test_freshness_capture_date_is_not_a_posting_date():
    """allow_inferred=False (hand-captured/manual roles): first_seen is when the OWNER
    clicked capture, so it must never earn the boost — only a real posted_at may."""
    captured_today = scoring.freshness(None, "2026-07-24T17:31:25-07:00", CFG,
                                       today="2026-07-24", allow_inferred=False)
    assert captured_today["bonus"] == 0.0 and captured_today["date"] is None
    # a real posted_at on a captured role still counts
    real_date = scoring.freshness("2026-07-23", "2026-07-24T17:31:25-07:00", CFG,
                                  today="2026-07-24", allow_inferred=False)
    assert real_date["bonus"] == 10.0 and real_date["posted_known"] is True


def test_freshness_is_not_baked_into_the_score_or_the_rescore_trigger():
    # The 4-dim score answers "is this a good job", which does not change as a req ages.
    s = scoring.score(mk(DEEP_JD), {}, CFG)
    assert s.weighted == scoring.score(mk(DEEP_JD), {}, CFG).weighted
    # ...so retuning freshness must not trigger a full rescore of every stored role.
    tuned = dict(CFG, scoring=dict(CFG["scoring"], freshness={"enabled": True, "bonus": 25}))
    assert scoring.config_hash(tuned) == scoring.config_hash(CFG)


def test_location_kind_recognises_remote_and_the_sf_bay_area_only():
    """优先远程或湾区 (owner 2026-09-22). Strings are real ones from roles.location."""
    from app.scoring import location_kind as k
    assert k("San Francisco, CA", 0) == "bay"
    assert k("US, CA, Santa Clara", 0) == "bay"            # Nvidia's Workday format
    assert k("San Francisco Bay Area", 0) == "bay"
    assert k("San Mateo, CA, United States · San Mateo, CA", 0) == "bay"
    assert k("Sunnyvale", 0) == "bay"
    assert k("Newark, CA", 0) == "bay" and k("Newark, NJ", 0) is None
    assert k("Belmont, MA", 0) is None and k("Dublin, Ireland", 0) is None
    assert k("Tampa Bay Area", 0) is None                  # a bay, not THE Bay
    assert k("2 Locations", 0) is None and k("Seattle, WA", 0) is None and k("", 0) is None
    assert k("Remote - US", 0) == "remote" and k("New York, NY", 1) == "remote"
    assert k("Remote - Canada", 1) is None and k("Remote - United Kingdom", 0) is None   # foreign remote is not for a US candidate
    assert k("Remote - US or Canada", 1) == "remote"                                       # a US marker keeps it


def test_location_pref_is_rank_only_and_configurable():
    from app.scoring import location_pref
    cfg = {"scoring": {"location_pref": {"enabled": True, "bonus": 8}}}
    assert location_pref("Palo Alto, CA", 0, cfg) == {"kind": "bay", "bonus": 8.0}
    assert location_pref("Austin, TX", 0, cfg) == {"kind": None, "bonus": 0.0}
    off = {"scoring": {"location_pref": {"enabled": False}}}
    assert location_pref("Palo Alto, CA", 0, off)["bonus"] == 0.0
    no_remote = {"scoring": {"location_pref": {"bonus": 8, "remote": False}}}
    assert location_pref("Remote", 1, no_remote) == {"kind": "remote", "bonus": 0.0}


# 2026-09-28 — no_sponsorship_jd: real refusals from live JDs must flag, and the two boilerplate
# false positives that hid whole companies (Cloudflare export-control clause, Wispr Flow hedge)
# must not. Phrases are verbatim from the 2026-09-28 sponsorship research.
def test_no_sponsorship_jd_flags_real_refusals():
    refusals = [
        "We're not able to sponsor US visas right now, though that may change down the line.",   # Firecrawl
        "However, we are currently unable to sponsor employment visas.",                           # Runpod
        "We are not able to provide visa sponsorship for this position at this time.",             # Lightning AI
        "we do not sponsor visas or transfers for new hires",                                      # Voltus
        "We are unable to sponsor or take over sponsorship of an employment visa for this role.",  # Verkada IT
        "The company can support visa transfers but will not sponsor individuals for H-1B CAP applications.",  # Adyen
        "Is role eligible for Immigration Sponsorship? No. Please note that we will not sponsor applicants for work visas for this position.",  # PANW
    ]
    for text in refusals:
        assert scoring.no_sponsorship_jd(text), text


def test_no_sponsorship_jd_ignores_export_control_and_hedges():
    not_refusals = [
        # Cloudflare: every US JD carries this export-law clause; it is not about visas.
        "you must be eligible to receive software or technology controlled under these U.S. export laws "
        "without sponsorship for an export license. Cloudflare is proud to be an equal opportunity employer.",
        # Wispr Flow / Exa: sponsors hedging, not refusing.
        "We sponsor H1B, O1, EB1, L1, STEM OPT, and more. We can't guarantee sponsorship for every role, "
        "but if we make you an offer we'll make every reasonable effort.",
        "Visas: We're happy to sponsor international candidates (e.g., STEM OPT, OPT, H1B, O1, E3). "
        "While we cannot guarantee your visa, we have historically been successful.",
        "We provide visa sponsorship support and assess each circumstance on a case-by-case basis.",   # Cartesia
    ]
    for text in not_refusals:
        assert not scoring.no_sponsorship_jd(text), text
