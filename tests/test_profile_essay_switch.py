"""A batch fill reads the board's essay switch from the profile the page already receives."""
from app import form_preflight, web


def test_profile_carries_the_board_essay_switch(monkeypatch):
    monkeypatch.setattr(web, "cfg", {"profile": {"first_name": "Avery", "email": "avery@example.test"}})
    role = {"job_id": "j", "company": "Acme"}
    assert web._profile_for_role(role)["allow_unread_essays"] is False
    form_preflight.set_allow_essays(web.cfg, True)
    assert web._profile_for_role(role)["allow_unread_essays"] is True
    form_preflight.set_allow_essays(web.cfg, False)
    assert web._profile_for_role(role)["allow_unread_essays"] is False
