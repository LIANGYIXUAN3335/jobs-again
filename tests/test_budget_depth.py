"""④ DEPTH — the floor under the stock cap (application_budget.depth, 2026-08-10).

The funnel found one significant driver of replies in 840 applications: how many reqs she
sent to the same company. 1 → 0/112 companies answered; 4-15 → 7/61; Fisher p = 0.0005.
① stock already caps the top at 8; nothing pushed a company UP off 1, because ③ rotation
interleaves one lead role per company and a company's #2 lands ~400 rows down the page.

Four properties are load-bearing here and each is a way this could quietly go wrong:
  the flag OFF must be byte-identical (this is a strategy change, she turns it on, not us);
  it must re-ORDER and never withhold (holding roles back is the 2026-07-31 bug);
  it must not be able to out-vote stock / pace / blocked (those are hard limits, not hints);
  and it must lead with companies already part-way, not with untouched ones.
"""
from app import budget, store
from tests.test_store import role_row


def _cfg(cfg, *, depth=None, total=50, pool=3, tie=3, stock=None, pace=None, blocked=None):
    """Same shape as test_store._rot_cfg, plus the depth knob. `depth=None` omits the key
    entirely, which is exactly how config.yaml looked before 2026-08-10."""
    ab = {"enabled": True, "stock": stock or {"default": 8}, "pace": pace or {},
          "daily": {"total": total, "per_company": 1, "pool": pool, "tie_break": tie}}
    if blocked:
        ab["blocked"] = blocked
    if depth is not None:
        ab["depth"] = depth
    return {**cfg, "application_budget": ab}


DEPTH_ON = {"enabled": True, "target": 6, "floor": 4,
            "min_company_score": 55, "max_score_drop": 15}


def _seed(cfg, spec):
    """spec = {company: (n_new_roles, n_already_applied, base_score)}."""
    jobs = store.connect_jobs(cfg)
    rows, applied = [], []
    for co, (n_new, n_applied, score) in spec.items():
        for i in range(n_new + n_applied):
            jid = f"gh:{co.lower()}:{i}"
            rows.append(role_row(jid, company=co, title=f"Backend Engineer {i}",
                                 score=score - i * 0.1))
            if i >= n_new:
                applied.append(jid)
    store.upsert_roles(jobs, rows)
    view = store.connect_view(cfg)
    for jid in applied:
        store.upsert_state(view, jid, status="applied", applied_date="2026-07-20")
    view.commit()
    return view


def _queue(view, cfg, **kw):
    d = store.fetch_view(view, cfg, status="new", **kw)
    return [r["company"] for r in d["rows"]], d


# --- 1. the flag is OFF, and OFF means nothing changed --------------------------------------

def test_flag_off_is_byte_identical_to_the_pre_depth_queue(cfg):
    """The whole point of shipping it dark. `depth` absent and `depth.enabled: false` must
    both reproduce the old page exactly — same rows, same order, same info dict."""
    view = _seed(cfg, {"Alpha": (6, 1, 80.0), "Beta": (5, 3, 70.0), "Gamma": (1, 0, 60.0)})
    absent = store.fetch_view(view, _cfg(cfg), status="new")
    off = store.fetch_view(view, _cfg(cfg, depth={**DEPTH_ON, "enabled": False}), status="new")
    assert [r["job_id"] for r in absent["rows"]] == [r["job_id"] for r in off["rows"]]
    assert absent["budget"] == off["budget"]
    assert "depth" not in off["budget"]          # not even an extra key on the readout


def test_config_yaml_ships_the_flag_off(cfg):
    """cfg here is the REAL config.yaml. She has to be the one who turns this on."""
    assert budget.depth_enabled(cfg) is False


# --- 2. it re-orders; it never withholds ----------------------------------------------------

def test_depth_reorders_and_returns_exactly_the_same_roles(cfg):
    """2026-07-31 rule: nothing may be held back, or a refresh reads as new arrivals.
    A depth floor built by withholding would bring that straight back."""
    view = _seed(cfg, {"Alpha": (6, 1, 80.0), "Beta": (5, 3, 70.0), "Gamma": (1, 0, 60.0)})
    off, _ = _queue(view, _cfg(cfg))
    on_rows = store.fetch_view(view, _cfg(cfg, depth=DEPTH_ON), status="new")["rows"]
    off_rows = store.fetch_view(view, _cfg(cfg), status="new")["rows"]
    assert {r["job_id"] for r in on_rows} == {r["job_id"] for r in off_rows}
    assert len(on_rows) == len(off_rows)
    assert [r["job_id"] for r in on_rows] != [r["job_id"] for r in off_rows]   # order moved


def test_a_one_req_company_keeps_its_row_it_just_stops_leading(cfg):
    """66 of the 112 one-application companies only ever had ONE eligible req — a floor
    cannot help them and this knob does not pretend it can. It demotes them; it never
    deletes them (Maincode replied off 2 reqs, so 'thin' is not 'worthless')."""
    view = _seed(cfg, {"Solo": (1, 0, 90.0), "Deep": (8, 1, 70.0)})
    order, d = _queue(view, _cfg(cfg, depth=DEPTH_ON))
    assert order[0] == "Deep"                        # depth block leads despite the lower score
    assert "Solo" in order                           # still on the page
    assert d["budget"]["depth"]["companies"] == 1    # Solo cannot reach the floor of 4


def test_rotation_stays_deterministic_and_covers_every_focus_company(cfg):
    """Refresh must not move the page, and 「换一批」 must still reach everyone — the
    coupon-collector argument that made ③ a rotation rather than a shuffle applies to the
    focus ring too."""
    c = _cfg(cfg, depth=DEPTH_ON, total=6)
    view = _seed(cfg, {f"C{i}": (8, 1, 90.0 - i) for i in range(6)})
    first = [r["job_id"] for r in store.fetch_view(view, c, rot=2, status="new")["rows"]]
    assert first == [r["job_id"] for r in store.fetch_view(view, c, rot=2, status="new")["rows"]]
    info = store.fetch_view(view, c, status="new")["budget"]["depth"]
    led = set()
    for rot in range(info["rounds"]):
        led |= {e["company"] for e in
                store.fetch_view(view, c, rot=rot, status="new")["budget"]["depth"]["lead"]}
    assert led == {f"C{i}" for i in range(6)}


# --- 3. it can never out-vote stock / pace / blocked ----------------------------------------

def test_depth_never_spends_past_the_stock_cap(cfg):
    """Stock is a hard limit; depth is a preference. A company at 2 with a cap of 5 gets 3."""
    view = _seed(cfg, {"Alpha": (10, 2, 70.0)})
    c = _cfg(cfg, depth=DEPTH_ON, stock={"default": 5})
    d = store.fetch_view(view, c, status="new")
    assert d["budget"]["depth"]["lead"] == [{"company": "Alpha", "reqs": 3, "already": 2}]
    assert [r["company"] for r in d["rows"][:3]] == ["Alpha"] * 3


def test_a_stock_override_of_one_keeps_a_company_out_of_the_depth_block(cfg):
    """Hightouch's JD says a second application auto-rejects the whole thing. `stock.overrides`
    must win outright — one slot cannot be pushed toward four."""
    view = _seed(cfg, {"Hightouch": (9, 0, 80.0), "Deep": (8, 1, 70.0)})
    c = _cfg(cfg, depth=DEPTH_ON, stock={"default": 8, "overrides": {"hightouch": 1}})
    d = store.fetch_view(view, c, status="new")
    assert [e["company"] for e in d["budget"]["depth"]["lead"]] == ["Deep"]
    assert d["rows"][0]["company"] == "Deep"


def test_a_retired_company_is_still_retired(cfg):
    view = _seed(cfg, {"Alpha": (5, 8, 90.0), "Deep": (8, 1, 70.0)})
    d = store.fetch_view(view, _cfg(cfg, depth=DEPTH_ON), status="new")
    assert "Alpha" not in {r["company"] for r in d["rows"]}
    assert "Alpha" in d["budget"]["retired"]


def test_pace_limited_and_blocked_companies_never_reach_the_depth_block(cfg):
    """pace (ATS rate limits — Plaid's Ashby 3/60d, hit for real 2026-07-20) and blocked (they
    interviewed and said no) are filters that run before grouping. Depth reads the survivors
    and must not be able to resurrect one. Names are synthetic: the real config.yaml also
    cooldowns Plaid/Amazon a layer above, in fetch_view, which would mask the thing under test."""
    view = _seed(cfg, {"Paceco": (9, 3, 90.0), "Deadco": (9, 0, 88.0), "Deep": (8, 1, 70.0)})
    c = _cfg(cfg, depth=DEPTH_ON,
             pace={"paceco": {"max": 3, "window_days": 60, "reason": "Ashby 3/60d"}},
             blocked={"days": 180, "companies": {"deadco": {"since": "2026-07-21"}}})
    d = store.fetch_view(view, c, status="new")
    shown = {r["company"] for r in d["rows"]}
    assert "Paceco" not in shown and "Deadco" not in shown
    assert {e["company"] for e in d["budget"]["depth"]["lead"]} == {"Deep"}
    assert set(d["budget"]["blocked_companies"]) == {"Paceco", "Deadco"}


# --- 4. who leads: part-way companies before untouched ones ---------------------------------

def test_a_company_at_three_applications_leads_an_untouched_one(cfg):
    """The cheapest half of the finding: 68 companies were parked at 2-3 applications (159
    applications, 1 reply) one short push from the 4-15 band, while an untouched company
    needs six from scratch. Note Fresh has the STRONGER roles and still yields — under the
    flag, closeness to the floor outranks score, which is the whole behaviour change."""
    view = _seed(cfg, {"Fresh": (9, 0, 95.0), "Partway": (9, 3, 60.0)})
    order, d = _queue(view, _cfg(cfg, depth=DEPTH_ON))
    assert [e["company"] for e in d["budget"]["depth"]["lead"]] == ["Partway", "Fresh"]
    assert [e["reqs"] for e in d["budget"]["depth"]["lead"]] == [3, 6]   # to 6 either way
    assert order[:3] == ["Partway"] * 3
    assert order[3:9] == ["Fresh"] * 6
    # ...and with the flag off, the strongest company leads, one row each, as before.
    off, _ = _queue(view, _cfg(cfg))
    assert off[:2] == ["Fresh", "Partway"]


def test_a_company_already_inside_the_band_is_left_alone(cfg):
    """This is a FLOOR, not a second cap. 4-15 is the sweet spot the data found; topping a
    company up from 5 to 6 buys nothing measurable, so it drops back to the plain interleave
    and its slot goes to a company still stuck below 4."""
    view = _seed(cfg, {"InBand": (9, 5, 90.0), "Stuck": (9, 1, 60.0)})
    d = store.fetch_view(view, _cfg(cfg, depth=DEPTH_ON), status="new")
    assert [e["company"] for e in d["budget"]["depth"]["lead"]] == ["Stuck"]
    assert d["rows"][0]["company"] == "Stuck"


def test_the_company_quality_bar_is_a_bar_on_depth_not_on_eligibility(cfg):
    """The module rule is that the score never gates eligibility, and that survives here: a
    company under the bar loses its depth block, not its rows. The bar is deliberately low —
    the 8 companies that actually answered scored 48-81, while the 85+ band went 0 for 41."""
    view = _seed(cfg, {"Cheap": (9, 1, 50.0), "Good": (9, 1, 60.0)})
    d = store.fetch_view(view, _cfg(cfg, depth=DEPTH_ON), status="new")
    assert [e["company"] for e in d["budget"]["depth"]["lead"]] == ["Good"]
    assert "Cheap" in {r["company"] for r in d["rows"]}          # never removed


def test_max_score_drop_keeps_a_companys_junk_tail_out_of_the_block(cfg):
    """Same reason `daily.pool` caps rotation at top-3: within one company the spread is real
    (Snowflake's eligible roles run 70.9 down to 32.6). Depth reaches further than pool, so it
    needs the same guard — the 35-point role goes back to the interleave, not into the block."""
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [
        role_row("gh:a:0", company="A", title="Backend Engineer", score=80.0),
        role_row("gh:a:1", company="A", title="Data Engineer", score=79.0),
        role_row("gh:a:2", company="A", title="Platform Engineer", score=78.0),
        role_row("gh:a:3", company="A", title="Mobile Engineer", score=77.0),
        role_row("gh:a:4", company="A", title="Sales Engineer", score=35.0),
        role_row("gh:a:5", company="A", title="Support Engineer", score=34.0),
    ])
    view = store.connect_view(cfg)
    d = store.fetch_view(view, _cfg(cfg, depth=DEPTH_ON, pool=1, tie=0), status="new")
    head = [r["job_id"] for r in d["rows"][:4]]
    assert head == ["gh:a:0", "gh:a:1", "gh:a:2", "gh:a:3"]      # the 35/34 are not in it
    assert {r["job_id"] for r in d["rows"]} == {f"gh:a:{i}" for i in range(6)}   # still present


def test_the_flavour_tie_break_still_picks_each_companys_lead_role(cfg):
    """Depth must not quietly disable ③'s variety rule — that rule is what stops 'each
    company's best role' from meaning 'each company's backend role' on every row."""
    jobs = store.connect_jobs(cfg)
    rows = [role_row(f"gh:a:{i}", company="A", title="Backend Engineer",
                     score=90.0 - i * 0.1) for i in range(6)]
    rows += [role_row("gh:b:0", company="B", title="Backend Engineer", score=80.0),
             role_row("gh:b:1", company="B", title="Data Engineer", score=79.0)]   # within the band
    rows += [role_row(f"gh:b:{i}", company="B", title="Backend Engineer",
                      score=78.0 - i * 0.1) for i in range(2, 6)]
    store.upsert_roles(jobs, rows)
    view = store.connect_view(cfg)
    d = store.fetch_view(view, _cfg(cfg, depth=DEPTH_ON), status="new")
    order = [r["job_id"] for r in d["rows"]]
    assert order[:6] == [f"gh:a:{i}" for i in range(6)]    # A's block, six deep
    assert order[6] == "gh:b:1"        # B's block still LEADS with the under-used flavour
    assert d["budget"]["buckets"]      # and the readout still reports the flavour mix


# --- 5. the plan itself, unit level ---------------------------------------------------------

def test_depth_plan_is_empty_when_the_flag_is_off(cfg):
    by_co = {"a": [{"weighted_score": 80.0, "rank_score": 80.0}] * 9}
    stock = {"a": {"used": 1, "left": 7, "limit": 8, "retired": False}}
    assert budget.depth_plan(by_co, stock, _cfg(cfg)) == {}
    assert budget.depth_plan(by_co, stock, _cfg(cfg, depth={"enabled": False})) == {}
    assert budget.depth_plan(by_co, stock, _cfg(cfg, depth=DEPTH_ON)) == {"a": 5}


def test_depth_plan_is_bounded_by_pool_stock_and_target(cfg):
    c = _cfg(cfg, depth=DEPTH_ON)
    role = {"weighted_score": 80.0, "rank_score": 80.0}
    cases = {
        # (pool, used, left) -> planned reqs, or absent
        (9, 0, 8): 6,      # untouched, plenty of room -> the target
        (9, 3, 5): 3,      # part-way -> just enough to land on the target
        (4, 0, 8): 4,      # pool is the binding limit
        (9, 0, 2): None,   # only 2 slots left, cannot reach the floor of 4
        (9, 4, 4): None,   # already inside the band
        (3, 0, 8): None,   # three reqs is not four
    }
    for (pool, used, left), want in cases.items():
        plan = budget.depth_plan({"a": [role] * pool},
                                 {"a": {"used": used, "left": left}}, c)
        assert plan.get("a") == want, (pool, used, left)
