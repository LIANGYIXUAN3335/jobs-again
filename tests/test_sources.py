from app.sources import ashby, greenhouse, lever

GH_JOB = {
    "id": 123,
    "title": "Software Engineer, Payments",
    "absolute_url": "https://boards.greenhouse.io/x/jobs/123",
    "location": {"name": "Remote, United States"},
    "offices": [{"name": "Remote, United States"}],
    "departments": [{"name": "Engineering"}],
    "content": "&lt;p&gt;Build &amp;amp; scale &lt;b&gt;distributed systems&lt;/b&gt;.&lt;/p&gt;",
}

LV_JOB = {
    "id": "ab-uuid",
    "text": "Backend Engineer",
    "hostedUrl": "https://jobs.lever.co/acme/ab-uuid",
    "categories": {
        "location": "San Francisco, CA",
        "team": "Infrastructure",
        "allLocations": ["San Francisco, CA", "New York, NY"],
    },
    "workplaceType": "remote",
    "descriptionPlain": "Own our payments ledger.",
    "lists": [{"text": "Requirements", "content": "<li>Go</li><li>Kafka</li>"}],
    "additionalPlain": "Benefits.",
}

AB_JOB = {
    "id": "34413f8d",
    "title": "Backend Engineer, Ledger",
    "department": "Engineering",
    "team": "Backend",
    "employmentType": "FullTime",
    "location": "New York, NY (HQ)",
    "secondaryLocations": [{"location": "San Francisco, CA"}],
    "isListed": True,
    "isRemote": True,
    "jobUrl": "https://jobs.ashbyhq.com/acme/34413f8d",
    "applyUrl": "https://jobs.ashbyhq.com/acme/34413f8d/application",
    "descriptionHtml": "<p>Distributed <b>ledger</b> systems.</p>",
    "descriptionPlain": "Distributed ledger systems.",
}


def test_greenhouse_parse_unescapes_content():
    p = greenhouse.parse_job("Stripe", "stripe", GH_JOB)
    assert p.job_id == "gh:stripe:123"
    assert p.is_remote
    assert "Build & scale distributed systems." in p.jd_text
    assert p.department == "Engineering"
    assert p.location == "Remote, United States"


def test_lever_parse_joins_lists_and_locations():
    p = lever.parse_job("Acme", "acme", LV_JOB)
    assert p.job_id == "lv:acme:ab-uuid"
    assert p.is_remote  # workplaceType=remote
    assert "payments ledger" in p.jd_text
    assert "Kafka" in p.jd_text
    assert "New York, NY" in p.location
    assert "San Francisco, CA" in p.location


def test_ashby_parse_prefers_plain_text():
    p = ashby.parse_job("Acme", "acme", AB_JOB)
    assert p.job_id == "ab:acme:34413f8d"
    assert p.is_remote
    assert p.jd_text == "Distributed ledger systems."
    assert "San Francisco" in p.location and "New York" in p.location
    assert p.apply_url  # jobUrl preferred, non-empty
