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
