"""robots.txt gate — the fail-open cases are the whole reason this parser exists."""
import httpx
import pytest

from app import robots


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x")


@pytest.fixture(autouse=True)
def _clear_cache():
    robots._cache.clear()
    robots.configure({})
    yield
    robots._cache.clear()


# ---------------------------------------------------------------- parser

def test_longest_match_wins():
    txt = "User-agent: *\nDisallow: /jobs\nAllow: /jobs/public\n"
    assert robots.allowed(txt, "jobscan", "/jobs/public/1") is True
    assert robots.allowed(txt, "jobscan", "/jobs/private") is False


def test_equal_specificity_disallow_wins():
    txt = "User-agent: *\nAllow: /a\nDisallow: /a\n"
    assert robots.allowed(txt, "jobscan", "/a") is False


def test_blank_line_inside_a_record_does_not_end_it():
    """stdlib robotparser drops the Disallow here and fails OPEN."""
    txt = "User-agent: *\n\nDisallow: /jobs\n"
    assert robots.allowed(txt, "jobscan", "/jobs/1") is False


def test_percent_encoded_pattern_matches_decoded_path():
    """Without unquote() this rule is silently skipped — a fail-open."""
    txt = "User-agent: *\nDisallow: /foo%20bar\n"
    assert robots.allowed(txt, "jobscan", "/foo bar") is False


def test_named_agent_group_beats_star():
    txt = "User-agent: *\nDisallow: /\n\nUser-agent: jobscan\nAllow: /\n"
    assert robots.allowed(txt, "jobscan", "/anything") is True
    assert robots.allowed(txt, "other", "/anything") is False


def test_empty_disallow_means_allow_all():
    assert robots.allowed("User-agent: *\nDisallow:\n", "jobscan", "/x") is True


def test_no_rules_at_all_is_allowed():
    assert robots.allowed("", "jobscan", "/x") is True


# ---------------------------------------------------------------- soft-200

def test_html_error_page_served_as_robots_is_not_trusted():
    assert robots.is_robots_body("<html><body>404 not found</body></html>") is False


def test_empty_body_is_a_valid_allow_all():
    assert robots.is_robots_body("   \n  ") is True


# ---------------------------------------------------------------- fetch

def test_404_is_permission():
    c = _client(lambda r: httpx.Response(404))
    assert robots.check(c, "https://x/jobs")[0] is True


def test_500_leaves_permission_unconfirmed():
    c = _client(lambda r: httpx.Response(500))
    ok, why = robots.check(c, "https://x/jobs")
    assert ok is False and "UNCONFIRMED" in why


def test_soft_200_leaves_permission_unconfirmed():
    c = _client(lambda r: httpx.Response(200, text="<html>nope</html>"))
    ok, why = robots.check(c, "https://x/jobs")
    assert ok is False and "UNCONFIRMED" in why


def test_result_is_cached_per_origin():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="User-agent: *\nDisallow: /no\n")

    c = _client(handler)
    robots.check(c, "https://x/a")
    robots.check(c, "https://x/b")
    assert len(calls) == 1


# ---------------------------------------------------------------- modes

def _denying():
    return _client(lambda r: httpx.Response(200, text="User-agent: *\nDisallow: /\n"))


def test_off_mode_never_fetches():
    calls = []
    c = _client(lambda r: (calls.append(1), httpx.Response(404))[1])
    robots.gate(c, "https://x/jobs", cfg={"robots": {"mode": "off"}})
    assert calls == []


def test_report_mode_logs_but_allows():
    seen = []
    robots.gate(_denying(), "https://x/jobs",
                cfg={"robots": {"mode": "report"}}, log=seen.append)
    assert seen and "WOULD BLOCK" in seen[0]


def test_enforce_mode_raises():
    with pytest.raises(robots.RobotsDenied):
        robots.gate(_denying(), "https://x/jobs", cfg={"robots": {"mode": "enforce"}})


def test_enforce_mode_fails_closed_on_unconfirmed():
    c = _client(lambda r: httpx.Response(503))
    with pytest.raises(robots.RobotsDenied):
        robots.gate(c, "https://x/jobs", cfg={"robots": {"mode": "enforce"}})


def test_allow_hosts_exempts_an_origin():
    robots.gate(_denying(), "https://x/jobs",
                cfg={"robots": {"mode": "enforce", "allow_hosts": ["x"]}})


def test_allow_hosts_matches_subdomains():
    c = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="User-agent: *\nDisallow: /\n")))
    robots.gate(c, "https://api.example.com/jobs",
                cfg={"robots": {"mode": "enforce", "allow_hosts": ["example.com"]}})


def test_configure_sets_the_default_policy():
    robots.configure({"robots": {"mode": "enforce"}})
    with pytest.raises(robots.RobotsDenied):
        robots.gate(_denying(), "https://x/jobs")
