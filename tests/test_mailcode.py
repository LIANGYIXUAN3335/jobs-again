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
