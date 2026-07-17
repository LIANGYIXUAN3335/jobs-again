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
    assert s.s_stability == 2.0  # 3.0 + 0.5 (large) - 1.5 (layoffs)
    assert "layoffs" in s.caution


def test_stability_prior_overrides_derivation():
    s = scoring.score(mk(DEEP_JD), {"stability_prior": 2.5, "recent_layoffs": 2026}, CFG)
    assert s.s_stability == 2.5


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
