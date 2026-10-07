from app.mailcode import _extract_code


def test_greenhouse_mixed_alphanumeric_code():
    # Real Greenhouse body shape — the code is letters+digits, not pure digits.
    body = ("Hi Alex, Copy and paste this code into the security code field on your "
            "application: dW9hkRYX After you enter the code, resubmit your application. "
            "© 2026 Greenhouse 18 West 18th Street, 11th Floor, New York, NY 10011, USA")
    assert _extract_code(body) == "dW9hkRYX"


def test_digit_codes_still_work():
    assert _extract_code("Your verification code is 483920. Expires in 10 minutes.") == "483920"
    assert _extract_code("123456 is your one-time code") == "123456"
    assert _extract_code("Use code 8472 to continue") == "8472"


def test_mixed_code_with_leading_digits():
    assert _extract_code("Your Docker one-time verification code: 55A2B1") == "55A2B1"


def test_rejects_placeholders_and_years():
    assert _extract_code("Security code: 000000") is None
    assert _extract_code("Copyright 2026 Acme. Nothing to see here.") is None
    assert _extract_code("Your account was created in 2026. Welcome!") is None


def test_plain_prose_after_code_phrase_is_not_a_code():
    # Ordinary words must never be mistaken for a code — the mixed letters+digits
    # requirement is what enforces this.
    assert _extract_code("Enter the code shown on your application page to continue") is None


def test_meeting_passcodes_are_not_verification_codes():
    """2026-09-25: a Heron Power Teams cancel ("Meeting ID: 220313541110825 / Passcode: g7Fi9Fs7")
    came back as the newest 'verification code'. Conference passcodes are for the call."""
    teams = ("This is a Microsoft Teams meeting. Click this link to join: https://teams.microsoft.com/l/x\n"
             "Meeting ID: 220313541110825\nPasscode: g7Fi9Fs7\n\nWe look forward to speaking with you.")
    assert _extract_code(teams) is None
    zoom = "Join Zoom Meeting https://scale.zoom.us/j/93484214217?pwd=abc\nMeeting ID: 934 8421 4217\nPassword: 266896"
    assert _extract_code(zoom) is None
    # a real code in a mail that also mentions a meeting link still comes through
    assert _extract_code("Your verification code is 483920. Join later at https://zoom.us/j/1") == "483920"


def test_agenda_mail_with_dress_code_and_links_yields_nothing():
    body = ("Hi Casey, The team is excited to have you visit on Thu, Oct 1, 2026! Dress code is casual.\n"
            "(This link is for anyone who might dial in remotely)\n"
            "https://teams.microsoft.com/l/meetup-join/19%3ameeting_NDZiYzViMWMtNDM2Mi00M2QwLWJlYjYtODlkOTQzZDkzMmU1%40thread.v2/0\n"
            "Agenda: 11:30 AM - 12:00 PM (PDT) - Presentation. Our address is 4742 Scotts Valley Drive, Scotts Valley, CA 95066.")
    assert _extract_code(body) is None
