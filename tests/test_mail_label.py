"""Company resolution for Gmail filing: the three tiers, and the four ways a thread lies about
who it is from.

Every case here is a real thread out of the mailbox that the first version got wrong. They are
the reason `auto` needs corroboration rather than just a domain.
"""
import datetime

import pytest

from app import company_board
from tools import mail_label as ml

GAZ = [
    (ml.re.compile(r"(?<![\w-])" + ml.re.escape(n) + r"(?![\w-])", ml.re.I), n)
    for n in sorted(["Ford", "Lucid", "Snowflake", "Google", "Pinterest", "CipherData",
                     "Cockroach Labs", "LinkedIn", "OpenAI"], key=len, reverse=True)
]


def resolve(text, domains, subjects=None):
    return ml._resolve(text, domains, GAZ, subjects=subjects or [text])


# --- tier 1: the employer's own domain ------------------------------------------------------

def test_known_company_domain_files_itself():
    co, tier, _ = resolve("1 Hour Schedule (Alex Example)", ["cipherdata.ai"])
    assert (co, tier) == ("CipherData", "auto")


def test_domain_beats_a_company_merely_mentioned_in_the_subject():
    """Mail from Snowflake that happens to mention Google is Snowflake's thread."""
    co, _, _ = resolve("Next Steps: interview, we use Google Cloud", ["snowflake.com"])
    assert co == "Snowflake"


def test_squashed_domain_name_still_matches_a_spaced_gazetteer_entry():
    co, tier, _ = resolve("Update on your Cockroach Labs application", ["cockroachlabs.com"])
    assert (co, tier) == ("Cockroach Labs", "auto")


# --- tier 2: the messenger problem ----------------------------------------------------------

def test_staffing_agency_files_under_its_client():
    """erginc.com is the agency; Lucid is the company you are interviewing with."""
    co, tier, why = resolve("Interview w/ Rachel - Software Engineer role - Lucid Motors",
                            ["erginc.com"])
    assert (co, tier) == ("Lucid", "auto")
    assert "erginc.com" in why


def test_outsourced_hr_files_under_its_client():
    co, _, _ = resolve("Complete WOTC screening for your application with Ford Motor Company",
                       ["adp.com"])
    assert co == "Ford"


def test_ats_vendor_domain_reads_the_display_name():
    co, tier, _ = resolve("Snowflake Hiring Team || Next Steps: Software Engineer Application",
                          ["ashbyhq.com"])
    assert (co, tier) == ("Snowflake", "auto")


def test_a_vendor_domain_is_never_itself_the_company():
    """A Lucid recruiter's InMail was filed under `LinkedIn` because the address said so.
    Addresses are excluded from the match text; without a company it must not guess."""
    co, tier, _ = resolve("Message replied: Interested in a Backend Software Engineering role",
                          ["linkedin.com"])
    assert co != "LinkedIn"
    assert tier == "review"


def test_subject_spells_out_what_the_domain_squashes():
    co, _, _ = resolve("Interested in Engineering Opportunities at Embedding VC",
                       ["embedding.vc"])
    assert co == "Embedding VC"


def test_public_suffix_is_not_the_company():
    co, _, _ = resolve("Interested in Engineering Opportunities at Open Government Products",
                       ["open.gov.sg"])
    assert co == "Open Government Products"


# --- tier 3: refusing to guess --------------------------------------------------------------

def test_two_companies_named_is_ambiguous_not_a_coin_flip():
    _, tier, why = resolve("Alex <> Pinterest || forwarded from Google recruiting",
                           ["goodtime.io"])
    assert tier == "review"
    assert "ambiguous" in why


def test_linkedin_job_alert_is_a_broadcast_even_though_it_names_a_company():
    _, tier, _ = resolve("LinkedIn Job Alerts || Software Engineer, Fleet Management at OpenAI",
                         ["linkedin.com"])
    assert tier == "skip"


def test_job_alerts_are_broadcasts_not_processes():
    """Names a company, reads as hiring — and is still a mass mail."""
    _, tier, _ = resolve("Your job alert for: Machine Learning Engineer, San Diego",
                         ["notifications.monster.com"])
    assert tier == "skip"


def test_newsletter_from_a_real_company_domain_is_not_a_process():
    _, tier, _ = resolve("This might be our best System Design roundup yet", ["educative.io"])
    assert tier == "skip"


def test_unknown_domain_with_nothing_hiring_shaped_is_skipped():
    _, tier, _ = resolve("California CCPA", ["yoh.com"])
    assert tier == "skip"


def test_a_description_is_not_a_company_name():
    """'a growing EV firm' parses as a trailing `at <X>` but is not a label."""
    assert ml._from_subject(["Backend Software Engineering role with a growing EV firm"]) == ""


@pytest.mark.parametrize("sender,expected", [
    ('"Snowflake Hiring Team" <sample-bbdb2b94@example.com>', "Snowflake Hiring Team"),
    ("Cwon Terrell <sample-e3f5fc30@example.com>", "Cwon Terrell"),
    ("bare@example.com", ""),
])
def test_display_name_strips_addresses(sender, expected):
    assert ml._display(sender) == expected


def test_label_name_cannot_nest_or_break_quoting():
    assert ml._label_name('Acme/Sub "Co"') == "Acme Sub Co"


def test_a_domain_suffix_is_not_part_of_the_company_name():
    """`Alterion.ai` and `Alterion` were becoming two separate labels."""
    assert ml._label_name("Alterion.ai") == ml._label_name("Alterion") == "Alterion"


# --- mailbox names on the wire --------------------------------------------------------------

@pytest.mark.parametrize("plain,encoded", [
    # RFC 3501 §5.1.3's own worked examples — the authority, not my arithmetic.
    ("~peter/mail/台北/日本語", "~peter/mail/&U,BTFw-/&ZeVnLIqe-"),
    ("Interviews/Google", "Interviews/Google"),
    ("R&D", "R&-D"),
])
def test_mailbox_names_are_modified_utf7(plain, encoded):
    """imaplib encodes its arguments as ASCII, so a Chinese label name raises before it ever
    reaches Gmail. RFC 3501 §5.1.3 is the wire format."""
    assert ml._utf7(plain) == encoded
    assert ml._un_utf7(encoded) == plain


def test_utf7_round_trips_every_label_this_tool_can_produce():
    for bucket in ml.BUCKETS.values():
        name = f"Interviews/{bucket}/Fin (formerly Intercom)"
        assert ml._un_utf7(ml._utf7(name)) == name
        ml._utf7(name).encode("ascii")             # must be wire-safe


def test_destructive_calls_refuse_anything_outside_the_root():
    for bad in ["[Gmail]/All Mail", "INBOX", "Interviews", "Other/Google"]:
        with pytest.raises(ValueError):
            ml._guard(bad, "Interviews")
    ok = ml._guard("Interviews/2-已冷/Google", "Interviews")
    assert ok.startswith('"Interviews/2-&') and ok.endswith('-/Google"')
    ok.encode("ascii")                             # what actually goes on the wire


# --- the board ------------------------------------------------------------------------------

def _ago(days: int) -> str:
    """Board rows go cold after 45 days, so anything testing the live path needs a real date."""
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def _thread(company, msgs, thrid="1"):
    return {"thrid": thrid, "company": company, "tier": "auto",
            "messages": [{"subject": s, "from": f, "date": d, "from_me": me}
                         for s, f, d, me in msgs]}


def test_board_says_you_owe_a_reply_when_they_spoke_last():
    rows = company_board.build([_thread("Google", [
        ("Next steps for your Google interviews", "sample-e3f5fc30@example.com", _ago(9), False)])])
    assert rows[0]["turn"] == "you"
    assert rows[0]["stage"] == "interview"


def test_board_waits_on_them_when_you_spoke_last():
    rows = company_board.build([_thread("Ford", [
        ("Interview invitation", "sample-01ef9d78@example.com", _ago(9), False),
        ("Re: Interview invitation", "sample-81d1a713@example.com", _ago(6), True)])])
    assert rows[0]["turn"] == "them"


def test_a_rejection_closes_the_row_only_if_it_had_the_last_word():
    """Companies reject one req and interview you for another; the later message revives it."""
    dead = company_board.build([_thread("Pinterest", [
        ("Invitation to interview", "sample-0e243595@example.com", _ago(9), False),
        ("we are not moving forward", "sample-0e243595@example.com", _ago(6), False)])])
    assert dead[0]["stage"] == "rejected"

    alive = company_board.build([_thread("Pinterest", [
        ("we are not moving forward", "sample-0e243595@example.com", _ago(9), False),
        ("Invitation to interview for a different role", "sample-0e243595@example.com", _ago(3), False)])])
    assert alive[0]["stage"] == "interview"


@pytest.mark.parametrize("subject", [
    "Invitation: Technical Interview w/ Joe @ Tue Jul 28",
    "Invitation from an unknown sender: Alex Example / Elle @ Mon Aug 3",
    "Docusign | Your interview has been scheduled for Tuesday, August 11",
    "You're All Set for Your Phone Call with Hadrian!",
])
def test_a_calendar_invite_is_not_a_reply_you_owe(subject):
    """The one incoming message that means the opposite of 'your turn': the time is booked."""
    rows = company_board.build([_thread("X", [(subject, "sample-478abec7@example.com", _ago(9), False)])])
    assert rows[0]["turn"] == "booked"
    assert rows[0]["urgency"] == 0


def test_a_human_reply_still_needs_an_answer():
    rows = company_board.build([_thread("X", [
        ("Re: your interview — can you do Thursday?", "sample-478abec7@example.com", _ago(9), False)])])
    assert rows[0]["turn"] == "you"


def test_furthest_stage_survives_a_later_scheduling_email():
    rows = company_board.build([_thread("Google", [
        ("Onsite interview loop", "sample-6cb3e36d@example.com", _ago(9), False),
        ("We need more information to schedule your interviews", "sample-6cb3e36d@example.com", _ago(6), False)])])
    assert rows[0]["stage"] == "onsite"
