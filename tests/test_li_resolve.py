"""li_resolve: the guarded fuzzy matcher, the dedupe pass, and the orphan board resolve.

The matcher's job is to be NARROW — most of these tests assert that it refuses to link
(seniority mismatch, ambiguity, location conflict), because a wrong link cascades
application status onto a job never applied to (see find_twin's docstring)."""
from __future__ import annotations

import httpx

from app import store
from app.config import norm_key
from tests.test_store import role_row
from tools import li_resolve as lr


# ---------------------------------------------------------------- matcher (pure)

def _m(li_title, cands, li_loc="Remote"):
    return lr.title_match(li_title, li_loc, [(f"k{i}", t, loc) for i, (t, loc) in enumerate(cands)])


def test_exact_squash_match_ignores_punctuation():
    verdict, keys = _m("Software Engineer - Backend", [("Software Engineer, Backend", "Remote")])
    assert verdict == "high" and keys == ["k0"]


def test_seniority_mismatch_never_links():
    assert _m("Software Engineer", [("Senior Software Engineer", "Remote")])[0] is None
    assert _m("Software Engineer II", [("Software Engineer III", "Remote")])[0] is None


def test_sr_abbreviation_and_arabic_levels_normalise():
    assert _m("Sr. Software Engineer", [("Senior Software Engineer", "Remote")])[0] == "high"
    assert _m("Software Engineer II", [("Software Engineer 2", "Remote")])[0] == "high"


def test_fuzzy_containment_single_candidate_links():
    verdict, keys = _m("Software Engineer, Backend",
                       [("Software Engineer - Backend Infrastructure", "Remote"),
                        ("Product Designer", "Remote")])
    assert verdict == "high" and keys == ["k0"]


def test_specific_word_swap_never_links():
    """Caught on the first live dry run: 'User Co-Experience' vs 'User Frameworks' shared
    3/4 core tokens but are different teams. Swapped specifics = different job."""
    assert _m("Software Engineer, User Co-Experience",
              [("Software Engineer, User Frameworks", "Remote")])[0] is None


def test_specialised_li_title_never_collapses_onto_generic_board_title():
    """Google posts dozens of 'SWE III, <area>' reqs plus a bare 'SWE III' — the bare row
    is its own req, not a catch-all twin (first dry run linked 10+ li rows to it)."""
    assert _m("Software Engineer III, BigQuery", [("Software Engineer III", "Remote")])[0] is None
    # the other direction stays allowed: boards carry the fuller req name…
    assert _m("Software Engineer", [("Software Engineer, Enterprise Systems", "Remote")])[0] == "high"
    # …UNLESS the added word is a discipline word — then it's a different profession, and
    # safety wins over recall ("SWE 3" does not link to "SWE 3, Networking").
    assert _m("Software Engineer 3", [("Software Engineer 3, Networking", "Remote")])[0] is None


def test_discipline_swap_never_links():
    assert _m("Backend Engineer", [("DevOps Engineer Backend", "Remote")])[0] is None
    assert _m("Network Reliability Engineer",
              [("Software Engineer, Network Performance & Reliability (Argo)", "Remote")])[0] is None


def test_newline_junk_in_li_title_is_stripped():
    assert _m("Software Engineer\nSan Francisco", [("Software Engineer", "San Francisco, CA")],
              li_loc="San Francisco, CA")[0] == "high"


def test_two_plausible_candidates_is_ambiguous_not_a_guess():
    verdict, keys = _m("Software Engineer, Backend",
                       [("Software Engineer - Backend Payments", "Remote"),
                        ("Software Engineer - Backend Infra", "Remote")])
    assert verdict == "ambiguous" and len(keys) == 2


def test_location_gate_is_strict_on_the_fuzzy_tier():
    """Token overlap let SF≈San Jose, New York≈Newark and 'Remote-US'≈'Bengaluru' through
    (review M2c). Fuzzy tier now needs city agreement — remote only matches remote."""
    assert _m("Backend Engineer", [("Backend Engineer, Ads", "New York, NY")],
              li_loc="San Francisco, CA")[0] is None
    assert _m("Backend Engineer", [("Backend Engineer, Ads", "San Jose, CA")],
              li_loc="San Francisco, CA")[0] is None
    assert _m("Backend Engineer", [("Backend Engineer, Ads", "Newark, New Jersey")],
              li_loc="New York, NY")[0] is None
    assert _m("Backend Engineer", [("Backend Engineer, Ads", "Bengaluru, India")],
              li_loc="Remote - United States")[0] is None
    # one-sided remote no longer carries a fuzzy link…
    assert _m("Backend Engineer", [("Backend Engineer, Ads", "Remote")],
              li_loc="San Francisco, CA")[0] is None
    # …but both-remote and same-city still do
    assert _m("Backend Engineer", [("Backend Engineer, Ads", "Remote")],
              li_loc="Remote - US")[0] == "high"
    assert _m("Backend Engineer", [("Backend Engineer, Ads", "San Francisco Bay Area")],
              li_loc="San Francisco, CA")[0] == "high"
    # the EXACT tier keeps the lenient rule: an identical title is strong evidence
    assert _m("Backend Engineer", [("Backend Engineer", "Remote")],
              li_loc="San Francisco, CA")[0] == "high"


def test_version_number_is_not_a_seniority_level():
    """'Payments 2.0' read as level 2 and matched 'Engineer II' (review M2a). A digit only
    counts as a level when it ends the title or follows 'level'/'L'."""
    assert _m("Software Engineer II",
              [("Software Engineer, Payments 2.0", "Remote")], li_loc="Remote")[0] is None
    assert _m("Software Engineer Level 2",
              [("Software Engineer II", "Remote")], li_loc="Remote")[0] == "high"


def test_spelled_out_discipline_phrases_are_caught():
    """'Machine Learning'/'Site Reliability'/'Quality Assurance' walked past the token set
    that stops ML/SRE/QA (review M2b)."""
    for phrase in ("Machine Learning", "Site Reliability", "Quality Assurance"):
        assert _m("Software Engineer",
                  [(f"Software Engineer, {phrase}", "Remote")], li_loc="Remote")[0] is None


# ---------------------------------------------------------------- pass A: dedupe

def test_dedupe_links_fuzzy_pairs_and_hides_them_from_review(cfg):
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        # exact twin (handled by reconcile_duplicates' tier)
        role_row("li:11", company="Acme", title="Backend Engineer"),
        role_row("gh:acme:1", company="Acme", title="Backend Engineer"),
        # fuzzy twin: Sr. vs Senior — exact norm differs, seniority signature matches
        role_row("li:10", company="Beta", title="Sr. Software Engineer"),
        role_row("gh:beta:1", company="Beta", title="Senior Software Engineer"),
        # ambiguous: two plausible backend candidates → report, never link
        role_row("li:12", company="Gamma", title="Software Engineer, Backend"),
        role_row("gh:gamma:1", company="Gamma", title="Software Engineer - Backend Payments"),
        role_row("gh:gamma:2", company="Gamma", title="Software Engineer - Backend Infra"),
    ])
    view = store.connect_view(cfg)
    out = lr.dedupe(view)

    assert {x[0] for x in out["fuzzy"]} == {"li:10"}
    assert [x[0] for x in out["ambiguous"]] == ["li:12"]
    dup = {r["job_id"]: r["duplicate_of"] for r in view.execute(
        "SELECT job_id, duplicate_of FROM role_state")}
    assert dup.get("li:10") == "gh:beta:1"
    assert dup.get("li:11") == "gh:acme:1"        # exact tier via reconcile_duplicates
    assert "li:12" not in dup or not dup["li:12"]

    # linked shadows leave the review queue; the canonical rows and the ambiguous li stay
    data = store.fetch_view(view, cfg, status="all")
    in_review = {r["job_id"] for r in data["rows"] if r.get("in_review")}
    assert "li:10" not in in_review and "li:11" not in in_review
    assert "gh:beta:1" in in_review and "li:12" in in_review


def test_dedupe_fuzzy_never_touches_rows_with_real_status(cfg):
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("li:20", company="Delta", title="Sr. Software Engineer"),
        role_row("gh:delta:1", company="Delta", title="Senior Software Engineer"),
    ])
    view = store.connect_view(cfg)
    store.set_status(view, "gh:delta:1", "applied")     # a human statement — hands off
    out = lr.dedupe(view)
    assert not out["fuzzy"]
    row = view.execute("SELECT duplicate_of FROM role_state WHERE job_id='li:20'").fetchone()
    assert not (row and row["duplicate_of"])


def test_dedupe_uniqueness_is_judged_on_the_full_candidate_set(cfg):
    """One applied + one new copy of the same title: filtering to fresh rows first would
    make the new copy look like the ONLY candidate and link it (review M1). The full set
    has two → ambiguous → no link."""
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("li:21", company="Epsilon", title="Senior Software Engineer"),
        role_row("gh:eps:1", company="Epsilon", title="Senior Software Engineer"),
        role_row("gh:eps:2", company="Epsilon", title="Senior Software Engineer"),
    ])
    view = store.connect_view(cfg)
    # upsert_state, not set_status — set_status's twin-cascade would link the pair itself
    # before dedupe() ever runs, which is exactly the machinery this guard protects.
    store.upsert_state(view, "gh:eps:1", status="applied")
    out = lr.dedupe(view)
    assert [x[0] for x in out["ambiguous"]] == ["li:21"]
    row = view.execute("SELECT duplicate_of FROM role_state WHERE job_id='li:21'").fetchone()
    assert not (row and row["duplicate_of"])


# ---------------------------------------------------------------- pass B: orphan resolve

def _gh_payload():
    return {"jobs": [{"id": 987, "title": "Backend Engineer",
                      "location": {"name": "Remote"}, "content": "jd",
                      "absolute_url": "https://vanity.example/x"}]}


def _mock_client():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404, text="")
        if "boards-api.greenhouse.io/v1/boards/orphanco/jobs" in url:
            return httpx.Response(200, json=_gh_payload())
        return httpx.Response(404, json={})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_resolve_orphans_swaps_apply_url_and_remembers_board(cfg):
    jobs = store.connect_jobs(cfg)
    li = role_row("li:30", company="OrphanCo", title="Backend Engineer")
    li["apply_url"] = "https://www.linkedin.com/jobs/view/30/"
    store.upsert_roles(jobs, [li])
    view = store.connect_view(cfg)

    state: dict = {}
    out = lr.resolve_orphans(view, jobs, _mock_client(), limit_companies=5, delay=0.0,
                             recheck_days=30, state=state, dry_run=False)
    assert out["boards_found"] == 1 and len(out["swapped"]) == 1
    url = jobs.execute("SELECT apply_url FROM roles WHERE job_id='li:30'").fetchone()["apply_url"]
    # PUBLIC path-form URL, never the embed form: the embed URL's id sits behind `token=`
    # which the extension's id-channel refuses, and its canonical form collides across all
    # greenhouse rows — the combination resolved a different company's role (review H1).
    assert url == "https://job-boards.greenhouse.io/orphanco/jobs/987"
    assert state["companies"][norm_key("OrphanCo")]["board"] == "greenhouse:orphanco"
    assert state["companies"][norm_key("OrphanCo")]["last_swaps"] == 1


def test_resolve_orphans_weak_slug_hit_is_reported_not_written(cfg):
    """A board answering on a go/get/try-prefixed or stem-stripped guess proves nothing
    about ownership ('Thrive' answers for the wrong Thrive — review H2)."""
    jobs = store.connect_jobs(cfg)
    li = role_row("li:50", company="OrphanCo", title="Backend Engineer")
    li["apply_url"] = "https://www.linkedin.com/jobs/view/50/"
    store.upsert_roles(jobs, [li])
    view = store.connect_view(cfg)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404, text="")
        if "boards-api.greenhouse.io/v1/boards/goorphanco/jobs" in url:
            return httpx.Response(200, json=_gh_payload())
        return httpx.Response(404, json={})

    state: dict = {}
    out = lr.resolve_orphans(view, jobs, httpx.Client(transport=httpx.MockTransport(handler)),
                             limit_companies=5, delay=0.0, recheck_days=30, state=state,
                             dry_run=False)
    assert out["boards_found"] == 0 and not out["swapped"]
    assert len(out["unconfirmed"]) == 1 and out["unconfirmed"][0][1] == "greenhouse:goorphanco"
    url = jobs.execute("SELECT apply_url FROM roles WHERE job_id='li:50'").fetchone()["apply_url"]
    assert "linkedin.com" in url                          # untouched
    entry = state["companies"][norm_key("OrphanCo")]
    assert entry["board"] is None and entry["board_unconfirmed"] == "greenhouse:goorphanco"


def test_resolve_orphans_network_failure_never_poisons_state(cfg):
    """Offline ≠ no board (review M4): a transport failure must not write a 30-day
    'no board' verdict."""
    jobs = store.connect_jobs(cfg)
    li = role_row("li:60", company="NetFail Co", title="Backend Engineer")
    li["apply_url"] = "https://www.linkedin.com/jobs/view/60/"
    store.upsert_roles(jobs, [li])
    view = store.connect_view(cfg)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    state: dict = {}
    out = lr.resolve_orphans(view, jobs, httpx.Client(transport=httpx.MockTransport(handler)),
                             limit_companies=5, delay=0.0, recheck_days=30, state=state,
                             dry_run=False)
    assert out["network_skipped"] >= 1 and out["no_board"] == 0
    assert norm_key("NetFail Co") not in state.get("companies", {})


def test_resolve_orphans_rests_recently_probed_dead_ends(cfg):
    jobs = store.connect_jobs(cfg)
    li = role_row("li:40", company="NoBoardInc", title="Backend Engineer")
    li["apply_url"] = "https://www.linkedin.com/jobs/view/40/"
    store.upsert_roles(jobs, [li])
    view = store.connect_view(cfg)

    import datetime
    state = {"companies": {norm_key("NoBoardInc"): {
        "checked": datetime.date.today().isoformat(), "board": None, "display": "NoBoardInc"}}}
    out = lr.resolve_orphans(view, jobs, _mock_client(), limit_companies=5, delay=0.0,
                             recheck_days=30, state=state, dry_run=False)
    assert out["probed"] == 0                      # rested — no re-probe inside the window
