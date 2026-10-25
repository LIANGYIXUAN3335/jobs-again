"""Which companies get no automatic application: plain rules over the calendar, recaps and stages."""
import datetime
import sqlite3

import pytest

from app import interview_ledger as L

TODAY = datetime.date(2026, 11, 5)


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.execute("ATTACH ':memory:' AS jobs")
    db.execute("CREATE TABLE jobs.roles(job_id TEXT, company TEXT)")
    db.execute("CREATE TABLE interviews(company TEXT, happened_on TEXT, round_type TEXT)")
    db.execute("CREATE TABLE role_state(job_id TEXT, stage TEXT, last_contact_at TEXT)")
    for name in ("Retell AI", "Cohere", "Google", "Close", "Pure Storage", "DocuSign", "Software"):
        db.execute("INSERT INTO jobs.roles VALUES(?,?)", (name, name))
    yield db
    db.close()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "_root", tmp_path)
    monkeypatch.setattr(L.cfgmod, "workspace_name", lambda _cfg: "test")
    return {"interview_ledger": {"aliases": {"Everpure": "Pure Storage"}}}


def event(title, day="2026-11-01", minutes=60, event_id=None, attendees=()):
    start = datetime.datetime.fromisoformat(f"{day}T10:00:00-08:00")
    return {"id": event_id or title, "summary": title, "status": "confirmed", "attendees": [{"email": a} for a in attendees],
            "start": {"dateTime": start.isoformat()}, "end": {"dateTime": (start + datetime.timedelta(minutes=minutes)).isoformat()}}


@pytest.mark.parametrize("title,minutes,kind", [
    ("Technical Screen with Robinhood", 60, "technical"),
    ("Everpure In Person Interview - Alex Example", 135, "technical"),
    ("[Applied Intuition] Onsite Interview - Casey Example", 180, "technical"),
    ("Recruiter Screen with Robinhood", 30, "recruiter"),
    ("Microsoft Engagement Call - Alex Example", 20, "recruiter"),
    ("MongoDB Recruiter Phone Interview – Software Engineer 3", 30, "recruiter"),
    ("Interview with Retell AI", 45, "technical"),
    ("Interview with Retell AI", 15, "recruiter"),
    ("NVIDIA 面试 · HM Ganesh", 45, "technical"),
    ("📚 Lucid SD 冲刺：技术面准备", 60, None),
    ("Krissa & Casey - Prep for Applied Intuition Technical Interview", 30, None),
    ("[Applied Intuition] Onsite Prep Call", 30, None),
    ("Canceled: Technical Screen with Robinhood", 60, None),
    ("Alex Example and Terra Vo", 30, None),
    ("Waymo", 60, None),
    ("Software Engineer Phone Screen - Stripe", 60, "technical"),
    ("Software Engineer Phone Screen - Stripe", 30, "recruiter"),
    ("Recruiter Phone Screen - Stripe", 60, "recruiter"),
    ("Your Phone Screen at Workday", 30, "recruiter"),
    ("Interview with Retell AI", 30, "recruiter"),
    ("Stripe - Round 2", 60, "technical"),
    ("Round 3", 30, "technical"),
    ("Final Round", 60, "technical"),
    ("Stripe 2nd round", 45, "technical"),
    ("Round 1 Recruiter Chat", 30, "recruiter"),
    ("Pair Programming", 60, "technical"),
    ("Stripe pairing session", 60, "technical"),
    ("Karat session", 60, "technical"),
    ("Superday", 240, "technical"),
    ("Stripe Super Day", 240, "technical"),
    ("Superday prep", 60, None),
    ("Round table with friends", 60, None),
])
def test_an_event_title_and_length_say_what_kind_it_is(title, minutes, kind):
    assert L.classify(title, minutes) == kind


def test_technical_blocks_recruiter_does_not_and_old_ones_expire(cfg, conn):
    L.save_calendar(cfg, [event("Interview with Retell AI", minutes=45), event("Interview with Cohere", minutes=30),
                          event("Technical Screen with Close", day="2026-07-01"), event("Google In-Person Interview", day="2026-11-11")], "seed")
    view = L.view(cfg, conn, today=TODAY)
    assert {item["company"] for item in view["blocked"]} == {"Retell AI", "Google"}
    assert {item["company"] for item in view["recruiter_only"]} == {"Cohere"}


def all_day(title, event_id=None):
    return {"id": event_id or title, "summary": title, "start": {"date": "2026-11-01"}, "end": {"date": "2026-11-02"}}


def test_cancelled_events_do_not_count(cfg, conn):
    L.save_calendar(cfg, [{**event("Technical Screen with Close"), "status": "cancelled"},
                          {**all_day("Technical Screen with Close"), "status": "cancelled"}], "seed")
    assert L.view(cfg, conn, today=TODAY)["blocked"] == []


def test_an_all_day_event_counts_only_when_its_title_says_technical(cfg, conn):
    L.save_calendar(cfg, [all_day("Technical Screen with Close"), all_day("Interview with Cohere"), all_day("Recruiter Screen with Google"),
                          all_day("Final Round", event_id="d1"), all_day("Interview season")], "seed")
    view = L.view(cfg, conn, today=TODAY)
    assert {item["company"] for item in view["blocked"]} == {"Close"}
    assert view["recruiter_only"] == []
    assert [(item["event_id"], item["minutes"]) for item in view["unknown"]] == [("d1", None)]


def test_a_recap_and_an_onsite_stage_also_block_but_a_friends_recap_does_not(cfg, conn):
    conn.execute("INSERT INTO interviews VALUES('Snorkel AI','2026-10-23','Technical Deep Dive')")
    conn.execute("INSERT INTO interviews VALUES('Maincode','2026-10-23','Recruiter 20分钟 intro call')")
    conn.execute("INSERT INTO interviews VALUES('Arya（peer）','2026-10-23','Onsite R1（同伴复盘）')")
    conn.execute("INSERT INTO interviews VALUES('Meta','unknown','Virtual Onsite')")
    conn.execute("INSERT INTO jobs.roles VALUES('j','Ocho')")
    conn.execute("INSERT INTO role_state VALUES('j','onsite','2026-10-27')")
    view = L.view(cfg, conn, today=TODAY)
    assert {item["company"] for item in view["blocked"]} == {"Snorkel AI", "Ocho"}
    assert {item["company"] for item in view["recruiter_only"]} == {"Maincode"}


def test_a_title_with_no_company_uses_the_other_sides_mail_or_asks_the_owner(cfg, conn):
    L.save_calendar(cfg, [event("Confirmed Interview: Alex Example / Andrey", attendees=["sample-5fbfd918@example.com", "sample-20bbacf0@example.com"]),
                          event("60 min screen (alex example)", event_id="e1", attendees=["sample-2f0cadd2@example.com", "sample-971bce57@example.com"])], "seed")
    view = L.view(cfg, conn, today=TODAY)
    assert cfg and {item["key"] for item in view["blocked"]} == {"docusign"}
    assert [item["event_id"] for item in view["unknown"]] == ["e1"]
    L.name_event(cfg, "e1", "Axiom Cloud")
    view = L.view(cfg, conn, today=TODAY)
    assert {item["key"] for item in view["blocked"]} == {"docusign", "axiomcloud"} and view["unknown"] == []


def test_a_company_named_after_a_common_word_never_matches_a_title(cfg, conn):
    L.save_calendar(cfg, [event("Software Interview with somebody")], "seed")
    assert "software" not in {item["key"] for item in L.view(cfg, conn, today=TODAY)["blocked"]}


def test_the_owner_can_overrule_either_way_and_undo(cfg, conn):
    L.save_calendar(cfg, [event("Interview with Retell AI", minutes=45)], "seed")
    L.override(cfg, "Retell AI", "allow")
    view = L.view(cfg, conn, today=TODAY)
    assert view["blocked"] == [] and view["allowed"][0]["company"] == "Retell AI"
    L.override(cfg, "Retell AI", "clear")
    L.override(cfg, "Cohere", "block")
    assert {item["company"] for item in L.view(cfg, conn, today=TODAY)["blocked"]} == {"Retell AI", "Cohere"}


def test_an_alias_and_a_longer_posting_name_are_covered(cfg, conn):
    L.save_calendar(cfg, [event("Everpure In Person Interview")], "seed")
    conn.execute("INSERT INTO interviews VALUES('Amazon','2026-10-23','OpenSearch Virtual Onsite')")
    keys = {item["key"] for item in L.view(cfg, conn, today=TODAY)["blocked"]}
    assert "purestorage" in keys
    assert L.is_blocked("amazonwebservicesaws", keys) and L.is_blocked("Pure Storage", keys) and not L.is_blocked("baseten", keys)
    assert not L.is_blocked("amazing", {"ama"})


def test_the_board_says_when_the_calendar_is_too_old(cfg, conn):
    assert L.view(cfg, conn, today=TODAY)["calendar"]["stale"] is True
    L.save_calendar(cfg, [], "seed", now=1000.0)
    assert L.view(cfg, conn, today=TODAY, now=1000.0 + 86400)["calendar"]["stale"] is False
    assert L.view(cfg, conn, today=TODAY, now=1000.0 + 9 * 86400)["calendar"]["stale"] is True


def test_without_a_calendar_token_the_last_copy_stands(cfg, conn, monkeypatch):
    monkeypatch.setattr(L.gcal, "_service", lambda _cfg: None)
    L.save_calendar(cfg, [event("Interview with Retell AI", minutes=45)], "seed")
    assert L.refresh_calendar(cfg)["ok"] is False
    assert {item["company"] for item in L.view(cfg, conn, today=TODAY)["blocked"]} == {"Retell AI"}


def test_a_stored_event_keeps_only_what_the_rules_read(cfg):
    slim = L.slim_event({**event("Interview", attendees=["sample-5fbfd918@example.com", "sample-fb98d44a@example.com"]), "description": "private", "location": "home"})
    assert set(slim) == {"id", "title", "start", "end", "domains"} and slim["domains"] == ["b.com"]


# Real names from the jobs database. The rule was read by hand against every prefix pair in it.
@pytest.mark.parametrize("posting,key", [
    ("Otter", "otterai"), ("Otter.ai", "otter"),
    ("Lucid", "lucidmotors"), ("Lucid Motors", "lucid"),
    ("Snorkel", "snorkelai"), ("Snorkel AI", "snorkel"),
    ("Amazon", "amazonwebservicesaws"), ("Amazon Web Services (AWS)", "amazon"),
    ("Reddit", "redditinc"), ("Reddit, Inc.", "reddit"),
    ("Microsoft AI", "microsoft"), ("NVIDIA", "nvidiaai"),
    ("Temporal", "temporaltechnologies"), ("Anduril Industries", "anduril"),
])
def test_two_names_of_one_company_match_in_both_directions(posting, key):
    assert L.is_blocked(posting, {key})


@pytest.mark.parametrize("posting,key", [
    ("Metabase", "meta"), ("Meta", "metabase"), ("Scaleway", "scale"), ("Scale", "scaleway"), ("OpenAI", "open"), ("Open", "openai"),
    ("Cohere Health", "cohere"), ("Cohere", "coherehealth"), ("Hinge Health", "hinge"), ("Clover Health", "clover"),
    ("WeaveGrid", "weave"), ("Weave Robotics", "weave"), ("Weave", "weavegrid"),
    ("Intuitive", "intuit"), ("Compass", "compa"), ("Medallion", "medal"), ("Provenir", "prove"), ("Wonderschool", "wonder"),
    ("Harvey Nash", "harvey"), ("Mercury Insurance", "mercury"), ("Motion Recruitment", "motion"), ("Clear Street", "clear"),
    ("Lucid Software", "lucidmotors"), ("Applied Materials", "appliedintuition"), ("Millennium Advisors, LLC", "millennium"),
])
def test_a_different_company_with_the_same_start_does_not_match(posting, key):
    assert not L.is_blocked(posting, {key})


def test_an_alias_covers_the_posting_side_too(cfg, conn):
    L.save_calendar(cfg, [event("Pure Storage Onsite Interview", day=datetime.date.today().isoformat())], "seed")
    keys = L.blocked_keys(cfg, conn)
    assert L.is_blocked("Everpure", keys) and L.is_blocked("Pure Storage", keys)
    assert not L.is_blocked("Purely", keys)


def test_an_alias_covers_a_row_under_a_longer_form_of_its_name(cfg, conn):
    L.override(cfg, "Pure Storage, Inc.", "block")
    assert L.is_blocked("Everpure", L.blocked_keys(cfg, conn))


def test_the_real_alias_table_joins_the_real_rows():
    aliases = L._aliases(L.cfgmod.load_config())
    assert aliases["everpure"] == "purestorage"
    assert aliases["amazonwebservicesaws"] == aliases["aws"] == "amazon"
    assert aliases["lucid"] == "lucidmotors"
    # Pairs in the jobs database that the tail rule rightly will not join on its own.
    assert aliases["walmartglobaltech"] == aliases["walmartconnect"] == "walmart"
    assert aliases["billcom"] == "bill" and aliases["kreaai"] == "krea" and aliases["uberfreight"] == "uber"


def test_the_owners_allow_works_under_an_alias_or_the_other_name(cfg, conn):
    today = datetime.date.today()
    L.save_calendar(cfg, [event("Everpure In Person Interview", day=today.isoformat())], "seed")
    # The board sends the name on the row, and the row sits under the alias target.
    row = L.view(cfg, conn)["blocked"][0]
    assert (row["company"], row["key"]) == ("Everpure", "purestorage")
    L.override(cfg, row["company"], "allow")
    assert not L.is_blocked("Everpure", L.blocked_keys(cfg, conn)) and L.view(cfg, conn)["blocked"] == []
    L.override(cfg, row["company"], "clear")
    assert L.is_blocked("Everpure", L.blocked_keys(cfg, conn))
    L.override(cfg, row["company"], "allow")
    conn.execute("INSERT INTO interviews VALUES('Otter.ai',?,'Karat')", (today.isoformat(),))
    conn.execute("INSERT INTO interviews VALUES('Otter',?,'Coding')", (today.isoformat(),))
    assert L.is_blocked("Otter", L.blocked_keys(cfg, conn))
    L.override(cfg, "Otter", "allow")
    assert not L.is_blocked("Otter.ai", L.blocked_keys(cfg, conn))


def test_a_recruiter_row_under_the_other_name_is_not_listed_as_recruiter_only(cfg, conn):
    conn.execute("INSERT INTO interviews VALUES('Otter.ai','2026-10-23','Coding')")
    conn.execute("INSERT INTO interviews VALUES('Otter','2026-10-22','Recruiter Screen')")
    assert L.view(cfg, conn, today=TODAY)["recruiter_only"] == []


@pytest.mark.parametrize("domain,key", [
    ("interview.adobe.com", "adobe"), ("bookings.microsoft.com", "microsoft"), ("microsoft.com", "microsoft"),
    ("ctr.freshworks.com", "freshworks"), ("careers.acme.co.uk", "acme"), ("clever.com", "clever"),
])
def test_a_mail_domain_gives_the_registrable_name(cfg, conn, domain, key):
    L.save_calendar(cfg, [event("Confirmed Interview: Alex Example / Andrey", attendees=[f"a@{domain}"])], "seed")
    assert {item["key"] for item in L.view(cfg, conn, today=TODAY)["blocked"]} == {key}


@pytest.mark.parametrize("domain", [
    "scheduling.candidate.fyi", "candidate.fyi", "goodtime.io", "invite.cronofy.com", "calendly.com", "paradox.ai", "fern.greenhouse.io",
    "group.calendar.google.com", "lu.ma", "erginc.com", "haventalent.io", "hugrecruitment.com", "toptal.com",
])
def test_a_scheduler_or_agency_domain_never_names_the_employer(cfg, conn, domain):
    L.save_calendar(cfg, [event("Confirmed Interview: Alex Example / Andrey", event_id="e1", attendees=[f"a@{domain}"])], "seed")
    view = L.view(cfg, conn, today=TODAY)
    assert view["blocked"] == [] and [item["event_id"] for item in view["unknown"]] == ["e1"]


def test_a_guessed_name_still_blocks_and_the_owner_is_asked(cfg, conn):
    L.save_calendar(cfg, [event("Confirmed Interview: Alex Example / Andrey", event_id="e1", attendees=["sample-d7b2428a@example.com"]),
                          event("Technical Interview with Priya", event_id="e2")], "seed")
    view = L.view(cfg, conn, today=TODAY)
    assert {item["key"] for item in view["blocked"]} == {"adobe", "priya"}
    assert {item["event_id"]: item["guess"] for item in view["unknown"]} == {"e1": "Adobe", "e2": "Priya"}
    assert all(item["guess"] in item["title"] for item in view["unknown"])
    L.name_event(cfg, "e2", "Stripe")
    view = L.view(cfg, conn, today=TODAY)
    assert {item["key"] for item in view["blocked"]} == {"adobe", "stripe"}
    assert [item["event_id"] for item in view["unknown"]] == ["e1"]


def test_a_name_something_else_vouches_for_is_not_asked_about(cfg, conn):
    conn.execute("INSERT INTO jobs.roles VALUES('a','Adobe')")
    conn.execute("INSERT INTO interviews VALUES('Apodex','2026-06-01','Coding')")
    L.save_calendar(cfg, [event("Confirmed Interview: Alex Example / Andrey", attendees=["sample-d7b2428a@example.com"]),
                          event("Apodex 1 st interview"), event("Everpure Interview"), event("Acme 1 st interview", event_id="a1"),
                          event("Acme 2 nd interview", event_id="a2")], "seed")
    assert {item["event_id"] for item in L.view(cfg, conn, today=TODAY)["unknown"]} == {"a1", "a2"}
    L.name_event(cfg, "a1", "Acme")
    assert L.view(cfg, conn, today=TODAY)["unknown"] == []


def test_a_friends_recap_does_not_vouch_for_a_name(cfg, conn):
    conn.execute("INSERT INTO interviews VALUES('Arya','2026-06-01','Coding')")
    L.save_calendar(cfg, [event("Arya 1 st interview", event_id="e1")], "seed")
    assert [item["event_id"] for item in L.view(cfg, conn, today=TODAY)["unknown"]] == ["e1"]


def test_words_of_a_round_are_not_a_company(cfg, conn):
    L.save_calendar(cfg, [event("Final Round", event_id="e1"), event("Karat session", event_id="e2"), event("Superday", event_id="e3"),
                          event("Pair Programming", event_id="e4"), event("Final Round Interview", event_id="e5"),
                          event("Software Engineer Phone Screen", event_id="e6")], "seed")
    view = L.view(cfg, conn, today=TODAY)
    assert view["blocked"] == []
    assert {item["event_id"] for item in view["unknown"]} == {"e1", "e2", "e3", "e4", "e5", "e6"}


def test_the_candidates_own_name_is_not_a_company(cfg, conn):
    L.save_calendar(cfg, [event("Technical Interview with Alex Example", event_id="e1"), event("Coding Interview with Casey", event_id="e2")], "seed")
    view = L.view(cfg, conn, today=TODAY)
    assert view["blocked"] == [] and {item["event_id"] for item in view["unknown"]} == {"e1", "e2"}


def test_a_title_naming_a_round_finds_the_company_in_front_of_it(cfg, conn):
    conn.execute("INSERT INTO jobs.roles VALUES('s','Stripe')")
    L.save_calendar(cfg, [event("Software Engineer Phone Screen - Stripe"), event("Acme - Round 2"), event("Nimbus Final Round")], "seed")
    assert {item["key"] for item in L.view(cfg, conn, today=TODAY)["blocked"]} == {"stripe", "acme", "nimbus"}


@pytest.mark.parametrize("round_type,kind", [
    ("第 4 轮 · Peer EM · BQ 复核 + Coding：Agent Heartbeat Monitor", "technical"),
    ("Onsite Round 2 · Sid · BQ（peer conflict + push back on PM）+ Coding", "technical"),
    ("Virtual Onsite · Behavioral (BQ 4) · Alex, VR Home peer engineer", "technical"),
    ("R4 hiring-manager round (peer: Arya) · Azure Storage", None),
    ("R1 technical（peer：Bob）", None),
    ("Bob （peer） · 技术轮", None),
    ("同伴复盘 · Microsoft Azure Storage onsite R4", None),
    ("Peer Mock · system design", None),
    ("system-design-mock · Webhook", None),
    ("Screen 1 (reference: another candidate) · Accounts team", None),
    ("Recruiter Screen", "recruiter"),
])
def test_only_a_friends_recap_is_set_aside_not_every_mention_of_peer(round_type, kind):
    assert L.classify_round(round_type) == kind


def test_a_real_recap_with_a_peer_interviewer_blocks_and_a_mock_row_does_not(cfg, conn):
    conn.execute("INSERT INTO interviews VALUES('Microsoft','2026-09-17','第 4 轮 · Peer EM · BQ 复核')")
    conn.execute("INSERT INTO interviews VALUES('Mock','2026-10-23','System Design 模拟 · 撮合引擎')")
    conn.execute("INSERT INTO interviews VALUES('Bob（peer）','2026-10-23','Onsite R1')")
    assert {item["key"] for item in L.view(cfg, conn, today=TODAY)["blocked"]} == {"microsoft"}


class FakeCalendar:
    """Google's events().list(): `pages` is {calendar: [page, page]}; `broken` holds a calendar, or (calendar, page number)."""

    def __init__(self, pages, broken=()):
        self.pages, self.broken, self.request = pages, set(broken), None

    def events(self):
        return self

    def list(self, calendarId, pageToken=None, **_):
        self.request = (calendarId, int(pageToken or 0))
        return self

    def execute(self):
        calendar, page = self.request
        if calendar in self.broken or self.request in self.broken:
            raise RuntimeError("no access")
        pages = self.pages.get(calendar) or [[]]
        return {"items": pages[page], **({"nextPageToken": str(page + 1)} if page + 1 < len(pages) else {})}


def blocked_names(cfg, conn):
    return {item["company"] for item in L.view(cfg, conn, today=TODAY)["blocked"]}


def test_a_calendar_that_fails_keeps_its_last_copy_and_the_board_says_so(cfg, conn, monkeypatch):
    first, second = sorted(L._SELF)
    service = FakeCalendar({first: [[event("Interview with Retell AI", minutes=45)]], second: [[event("Google In-Person Interview")]]})
    monkeypatch.setattr(L.gcal, "_service", lambda _cfg: service)
    assert L.refresh_calendar(cfg)["ok"] is True
    view = L.view(cfg, conn, today=TODAY)
    assert blocked_names(cfg, conn) == {"Retell AI", "Google"}
    assert view["calendar"]["stale"] is False and view["calendar"]["missing"] == [] and not view.get("refresh_error")

    service.pages[first] = [[event("Technical Screen with Close")]]
    service.broken = {second}
    result = L.refresh_calendar(cfg)
    assert result["ok"] is True and second in result["error"]
    view = L.view(cfg, conn, today=TODAY)
    assert blocked_names(cfg, conn) == {"Close", "Google"}   # the read one is new, the failed one is its old copy
    assert view["calendar"]["stale"] is True and view["calendar"]["missing"] == [second] and second in view["refresh_error"]
    # Decided by the owner: a stale calendar is shown, never a reason to refuse a claim.
    assert L.is_blocked("Google", {item["key"] for item in view["blocked"]})

    service.broken = set()
    result = L.refresh_calendar(cfg)
    view = L.view(cfg, conn, today=TODAY)
    assert result["ok"] is True and not result.get("error")
    assert view["calendar"]["stale"] is False and not view.get("refresh_error")


def test_a_calendar_that_fails_on_a_later_page_keeps_its_whole_old_copy(cfg, conn, monkeypatch):
    first, second = sorted(L._SELF)
    service = FakeCalendar({first: [[event("Technical Screen with Close")]], second: [[]]})
    monkeypatch.setattr(L.gcal, "_service", lambda _cfg: service)
    L.refresh_calendar(cfg)
    service.pages[first] = [[event("Google In-Person Interview")], [event("Interview with Retell AI", minutes=45)]]
    service.broken = {(first, 1)}
    L.refresh_calendar(cfg)
    assert blocked_names(cfg, conn) == {"Close"}
    assert L.view(cfg, conn, today=TODAY)["calendar"]["missing"] == [first]


def test_the_board_reports_the_oldest_copy_not_the_freshest(cfg, conn):
    first, second = sorted(L._SELF)
    L._save_copies(cfg, {first: [], second: []}, now=1000.0)
    L._save_copies(cfg, {first: []}, now=1000.0 + 5 * 86400)
    calendar = L.view(cfg, conn, today=TODAY, now=1000.0 + 5 * 86400)["calendar"]
    assert calendar["age_days"] == 5.0 and calendar["fetched_at"] == 1000.0 and calendar["stale"] is True
    # Both read, and both old enough to be stale on age alone.
    L._save_copies(cfg, {first: [], second: []}, now=1000.0 + 5 * 86400)
    assert L.view(cfg, conn, today=TODAY, now=1000.0 + 6 * 86400)["calendar"]["stale"] is False
    assert L.view(cfg, conn, today=TODAY, now=1000.0 + 13 * 86400)["calendar"]["stale"] is True


def test_the_seed_copy_is_read_until_every_calendar_has_been_read(cfg, conn, monkeypatch):
    first, second = sorted(L._SELF)
    L.save_calendar(cfg, [event("Interview with Retell AI", minutes=45), event("60 min screen", event_id="e1")], "seed", now=1000.0)
    assert blocked_names(cfg, conn) == {"Retell AI"}
    service = FakeCalendar({first: [[event("Technical Screen with Close"), event("60 min screen", event_id="e1")]],
                            second: [[event("Google In-Person Interview")]]}, broken={second})
    monkeypatch.setattr(L.gcal, "_service", lambda _cfg: service)
    assert L.refresh_calendar(cfg)["ok"] is True
    view = L.view(cfg, conn, today=TODAY)
    assert blocked_names(cfg, conn) == {"Retell AI", "Close"}
    assert view["calendar"]["fetched_at"] == 1000.0 and view["calendar"]["stale"] is True
    assert [item["event_id"] for item in view["unknown"]] == ["e1"]   # the same event in two copies is one event
    service.broken = set()
    L.refresh_calendar(cfg)
    assert blocked_names(cfg, conn) == {"Close", "Google"}
    assert L.view(cfg, conn, today=TODAY)["calendar"]["source"] == "google"


def test_a_dead_token_or_no_readable_calendar_marks_the_copy_stale_at_once(cfg, conn, monkeypatch):
    L.save_calendar(cfg, [event("Interview with Retell AI", minutes=45)], "seed")
    assert L.view(cfg, conn, today=TODAY)["calendar"]["stale"] is False
    monkeypatch.setattr(L.gcal, "_service", lambda _cfg: None)
    assert L.refresh_calendar(cfg)["ok"] is False
    view = L.view(cfg, conn, today=TODAY)
    assert view["calendar"]["stale"] is True and view["calendar"]["age_days"] < 1 and view["refresh_error"]
    assert view["calendar"]["source"] == "seed" and blocked_names(cfg, conn) == {"Retell AI"}
    service = FakeCalendar({}, broken=L._SELF)
    monkeypatch.setattr(L.gcal, "_service", lambda _cfg: service)
    assert L.refresh_calendar(cfg)["ok"] is False
    assert L.view(cfg, conn, today=TODAY)["calendar"]["stale"] is True and blocked_names(cfg, conn) == {"Retell AI"}


def test_an_all_day_event_survives_slimming_with_its_dates(cfg):
    slim = L.slim_event(all_day("Technical Screen with Close"))
    assert set(slim) == {"id", "title", "start", "end", "domains"} and slim["start"] == "2026-11-01" and L._minutes(slim) is None


# ---- a first conversation about her background is not a technical round ----
@pytest.mark.parametrize("round_type,kind", [
    ("Platform Engineer（Forward-Deployed）· 技术背景 + 动机初筛", "recruiter"),
    ("Round 1 · CTO & Co-founder 动机筛选", "recruiter"),
    ("Round 0 · 第三方招聘平台 Paraform 初筛（早于 01 号 CTO 动机筛选轮）", "recruiter"),
    ("Founder 电话初筛（非技术，无 rubric）", "recruiter"),
    ("Recruiter/背景初筛 · Full Stack Engineer（技术背景问答，无编码）", "recruiter"),
    ("技术初筛 · CoderPad", "technical"),
    ("Recruiter Screen · 电话初筛（团队结构/技术栈/流程说明）", "technical"),
    ("技术+行为综合筛选轮", "technical"),
    ("初筛 + coding", "technical"),
    ("Technical Screen", "technical"),
])
def test_a_screening_recap_is_light_unless_it_tested_anything(round_type, kind):
    assert L.classify_round(round_type) == kind


def test_a_screening_recap_no_longer_blocks_the_company(cfg, conn):
    conn.execute("INSERT INTO interviews VALUES('Lightning AI','2026-10-20','Platform Engineer · 技术背景 + 动机初筛')")
    conn.execute("INSERT INTO interviews VALUES('Tryalma','2026-10-19','Round 1 · CTO & Co-founder 动机筛选')")
    view = L.view(cfg, conn, today=TODAY)
    assert view["blocked"] == [] and {item["company"] for item in view["recruiter_only"]} == {"Lightning AI", "Tryalma"}


# ---- a person's name on a go-between's invite ----
def test_a_name_after_with_is_not_a_company_on_a_staffing_or_scheduling_invite(cfg, conn):
    L.save_calendar(cfg, [event("Traceforce – Interview Next Steps with Kylie (Insight Global)", minutes=15, event_id="a"),
                          event("Recruiter Screen (Google Meet - Audio only) with Brian | Senior Platform Security Engineer", minutes=30, event_id="b",
                                attendees=["sample-6ead1d65@example.com", "sample-c0934e53@example.com"])], "seed")
    assert {item["company"] for item in L.view(cfg, conn, today=TODAY)["recruiter_only"]} == {"Traceforce"}


def test_a_company_after_with_stays_when_no_go_between_is_involved(cfg, conn):
    L.save_calendar(cfg, [event("Interview with AfterQuery", minutes=20, attendees=["sample-69480049@example.com"]),
                          event("Interview with Verkada", minutes=30, attendees=["sample-c0934e53@example.com"])], "seed")
    assert {item["key"] for item in L.view(cfg, conn, today=TODAY)["recruiter_only"]} == {"afterquery", "verkada"}


def test_a_technical_round_on_such_an_invite_still_blocks_on_the_guess(cfg, conn):
    L.save_calendar(cfg, [event("Technical Screen with Brian", minutes=60, event_id="t", attendees=["sample-6ead1d65@example.com"])], "seed")
    view = L.view(cfg, conn, today=TODAY)
    assert {item["company"] for item in view["blocked"]} == {"Brian"} and [item["event_id"] for item in view["unknown"]] == ["t"]


# ---- a seed copy says what it is ----
def test_a_seed_copy_says_it_is_a_copy_and_stays_saying_so_when_a_read_fails(cfg, conn, monkeypatch):
    L.save_calendar(cfg, [event("Interview with Retell AI", minutes=45)], "seed")
    note = L.view(cfg, conn, today=TODAY)["calendar"]["copy_note"]
    assert "副本" in note and "candidate" in note and "看不到" in note
    monkeypatch.setattr(L.gcal, "_service", lambda _cfg: None)
    L.refresh_calendar(cfg)
    assert L.view(cfg, conn, today=TODAY)["calendar"]["copy_note"] == note


def test_a_calendar_read_from_google_is_not_called_a_copy(cfg, conn, monkeypatch):
    service = FakeCalendar({calendar: [[event("Technical Screen with Close")]] for calendar in L._SELF})
    monkeypatch.setattr(L.gcal, "_service", lambda _cfg: service)
    L.refresh_calendar(cfg)
    assert L.view(cfg, conn, today=TODAY)["calendar"]["copy_note"] == ""
