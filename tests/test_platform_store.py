import os

import pytest

from app import platform_store as ps


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_CODE", "owner-secret")
    c = ps.connect(tmp_path / "platform.db")
    yield c
    c.close()


def test_invite_registration_and_tenant_config(conn):
    owner = ps.authenticate(conn, "owner@example.com", "owner-secret")
    assert owner["role"] == "owner"
    invite = ps.create_invite(conn, owner, "customer@example.com")
    customer = ps.register(conn, "customer@example.com", "Customer", "customer-secret", invite)
    assert customer["role"] == "customer"

    saved = ps.save_config(conn, customer, {
        "target_roles": ["software", "mle", "not-real"],
        "locations": ["Bay Area", "Remote"],
        "company_watchlist": ["NVIDIA", "NVIDIA"],
        "company_blacklist": [],
        "min_match_score": 78,
        "daily_tailor_limit": 12,
    })
    assert saved["target_roles"] == ["software", "mle"]
    assert saved["company_watchlist"] == ["NVIDIA"]
    assert saved["min_match_score"] == 78


def test_resume_requires_admin_review_and_customer_confirmation(conn):
    owner = ps.authenticate(conn, "owner@example.com", "owner-secret")
    invite = ps.create_invite(conn, owner)
    customer = ps.register(conn, "c@example.com", "C", "customer-secret", invite)
    resume = ps.upload_resume(conn, customer, "A" * 120)
    assert resume["status"] == "admin_review"
    assert ps.dashboard(conn, customer)["tailoring_enabled"] is False

    ps.review_resume(conn, owner, resume["id"], "approve", "Facts look consistent")
    assert ps.latest_resume(conn, customer["id"])["status"] == "customer_confirmation"
    ps.confirm_resume(conn, customer, resume["id"])
    assert ps.dashboard(conn, customer)["tailoring_enabled"] is True


def test_only_owner_can_promote_admin(conn):
    owner = ps.authenticate(conn, "owner@example.com", "owner-secret")
    first = ps.register(conn, "a@example.com", "A", "customer-secret", ps.create_invite(conn, owner))
    second = ps.register(conn, "b@example.com", "B", "customer-secret", ps.create_invite(conn, owner))
    ps.set_role(conn, owner, first["id"], "admin")
    admin = ps.get_user(conn, first["id"])
    with pytest.raises(PermissionError):
        ps.set_role(conn, admin, second["id"], "admin")
