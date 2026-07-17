from app import config as cfgmod
from app.filters import classify
from app.sources.base import Posting

FILTERS = cfgmod.load_config()["filters"]


def mk(title, location="San Jose, CA", dept="Engineering"):
    return Posting(
        ats="greenhouse", company="X", slug="x", raw_id="1", title=title,
        location=location, jd_text="", apply_url="", department=dept,
    )


def test_staff_and_up_dropped():
    for t in ["Staff Software Engineer", "Principal Engineer", "Director of Engineering",
              "Engineering Manager", "Software Engineer Intern", "Junior Developer"]:
        assert not classify(mk(t), FILTERS).keep, t


def test_mid_kept_and_tagged():
    r = classify(mk("Software Engineer II, Payments"), FILTERS)
    assert r.keep and r.seniority == "mid"


def test_senior_kept_and_tagged_by_default():
    r = classify(mk("Senior Software Engineer, Storage"), FILTERS)
    assert r.keep and r.seniority == "senior"


def test_senior_dropped_when_flag_off():
    f = {**FILTERS, "keep_senior_titles": False}
    assert not classify(mk("Senior Software Engineer"), f).keep


def test_role_excludes():
    for t in ["Frontend Engineer", "Software Engineer, iOS", "Machine Learning Engineer",
              "Site Reliability Engineer", "Solutions Engineer", "Product Manager"]:
        assert not classify(mk(t), FILTERS).keep, t


def test_non_engineering_dropped_by_no_include():
    for t, d in [("Content Marketer", "Marketing"), ("Chief Audit Officer", "Finance")]:
        assert not classify(mk(t, dept=d), FILTERS).keep, t


def test_location_rules():
    assert not classify(mk("Backend Engineer", location="Amsterdam"), FILTERS).keep
    assert not classify(mk("Backend Engineer", location="Remote - EMEA"), FILTERS).keep
    assert classify(mk("Backend Engineer", location="Remote"), FILTERS).keep
    assert classify(mk("Backend Engineer", location="Remote, United States"), FILTERS).keep
    # multi-location with a US option survives the London mention
    assert classify(mk("Backend Engineer", location="New York, NY · London"), FILTERS).keep


def test_member_of_technical_staff_is_an_ic_title():
    # Cockroach Labs' standard IC title — "staff" here is not a level
    r = classify(mk("Member of Technical Staff (Storage)"), FILTERS)
    assert r.keep and r.seniority == "mid"
    r = classify(mk("Member of Technical Staff (Senior - Specialized Indexing)"), FILTERS)
    assert r.keep and r.seniority == "senior"
    # ...but a real Staff title is still dropped
    assert not classify(mk("Staff Software Engineer, Storage"), FILTERS).keep


def test_word_boundaries_no_false_hits():
    # "ios" must not match "bios"; "api" must not match "rapid"
    assert classify(mk("Backend Engineer, BIOS Platform"), FILTERS).keep
    r = classify(mk("Rapid Response Engineer"), FILTERS)
    assert r.keep  # 'rapid' must not be read as 'api'
