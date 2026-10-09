"""Outreach drafts (owner 2026-09-23): contact → matched roles → draft → Gmail Drafts, all
workspace-aware. The model and the mailbox are stubbed; what is pinned here is the plumbing —
matching, storage, error capture, signature, and that nothing ever calls send."""
import json
import sys
from types import SimpleNamespace

import yaml

from app import outreach, store
from tests.test_store import role_row


def _cfg_with_contacts(cfg, tmp_path, contacts):
    p = tmp_path / "contacts.yaml"
    p.write_text(yaml.safe_dump({"contacts": contacts}), encoding="utf-8")
    return {**cfg, "application_budget": {"enabled": False},
            "outreach": {"contacts_file": str(p), "tiers": ["A", "B"], "hook": "Saw your note.", "max_words": 120},
            "mailcode": {**(cfg.get("mailcode") or {}), "enabled": False}}


def test_match_company_by_norm_suffix_and_prefix(cfg, tmp_path):
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [role_row("gh:d:1", company="Disney", score=80), role_row("gh:d:2", company="Disney", score=70),
                              role_row("gh:r:1", company="Roblox", score=75), role_row("gh:z:1", company="Zum Services", score=60)])
    view = store.connect_view(cfg)
    by = outreach.roles_by_company(view, {**cfg, "application_budget": {"enabled": False}})
    assert outreach.match_company(by, "Roblox")[0] == "roblox"
    assert outreach.match_company(by, "The Walt Disney Company")[0] == "disney"       # suffixes stripped
    assert [r["job_id"] for r in outreach.match_company(by, "Disney")[1]] == ["gh:d:1", "gh:d:2"]   # best first
    assert outreach.match_company(by, "Zum Services Inc")[0] == "zumservices"          # suffix stripped
    assert outreach.match_company(by, "Zum")[1] == []                                   # 3 letters: too short to guess
    assert outreach.match_company(by, "Nowhere Inc")[1] == []


def test_generate_stores_draft_with_signature_and_captures_errors(cfg, tmp_path, monkeypatch):
    jobs = store.connect_jobs(cfg)
    store.upsert_roles(jobs, [role_row("gh:r:1", company="Roblox", title="Senior Software Engineer, Compute", score=75)])
    c = _cfg_with_contacts(cfg, tmp_path, [
        {"name": "Marie Romero", "email": "sample-3b028eda@example.com", "company": "Roblox", "size": "500+", "tier": "A", "note": "See open roles"},
        {"name": "Nobody", "email": "sample-e446c512@example.com", "company": "Nowhere Inc", "size": "500+", "tier": "A"}])
    view = store.connect_view(c)
    monkeypatch.setattr(outreach.aimod, "load_key", lambda cfg: "test-key")
    monkeypatch.setattr(outreach, "resume_for", lambda cfg, roles: str(tmp_path / "r.pdf"))
    sent = {}
    def create(**kw):
        sent["user"] = kw["messages"][0]["content"]; sent["system"] = kw["system"]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(
            {"subject": "Backend engineer — Roblox", "body": "You build the compute platform.\n\nI ship Go services.\n\nI attached my résumé for Senior Software Engineer, Compute. Could we talk for 15 minutes?"}))])
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda api_key=None: SimpleNamespace(messages=SimpleNamespace(create=create))))
    contacts = outreach.all_contacts(c)
    assert [x["email"] for x in contacts] == ["sample-3b028eda@example.com", "sample-e446c512@example.com"]   # mailbox off → list only
    rec = outreach.generate(view, c, contacts[0])
    assert rec["status"] == "draft" and rec["subject"].startswith("Backend engineer")
    assert rec["body"].rstrip().endswith("linkedin.com/in/candidate") or "Casey Example" in rec["body"]   # signature appended
    assert json.loads(rec["roles_json"])[0]["job_id"] == "gh:r:1"
    assert "Senior Software Engineer, Compute" in sent["user"] and "Saw your note." in sent["user"]
    assert "Casey Example" in sent["system"] and "__NAME__" not in sent["system"]
    # a contact with no roles still drafts (the prompt says so); a model failure lands on the row
    rec2 = outreach.generate(view, c, contacts[1])
    assert rec2["status"] == "draft" and "(none found in JobScan)" in sent["user"]
    def boom(**kw): raise RuntimeError("quota")
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda api_key=None: SimpleNamespace(messages=SimpleNamespace(create=boom))))
    rec3 = outreach.generate(view, c, contacts[1])
    assert rec3["status"] == "error" and "quota" in rec3["error"] and rec3["id"] == rec2["id"]   # same row, replaced
    # edits and status transitions
    upd = store.outreach_update(view, rec["id"], subject="New subject", status="saved", saved_at="2026-09-23T10:00:00")
    assert upd["subject"] == "New subject" and upd["status"] == "saved"
    assert len(store.outreach_list(view)) == 2


def test_save_gmail_draft_appends_to_drafts_and_never_sends(cfg, tmp_path, monkeypatch):
    calls = []
    class FakeImap:
        def login(self, a, p): calls.append(("login", a))
        def append(self, folder, flags, dt, raw): calls.append(("append", folder, flags, raw)); return ("OK", [b"[APPENDUID 1 2]"])
        def logout(self): calls.append(("logout",))
    monkeypatch.setattr(outreach.mailmod, "_creds", lambda cfg: ({"imap_host": "imap.gmail.com"}, "me@example.com", "pw"))
    monkeypatch.setattr(outreach.mailmod, "_imap_connect", lambda mc: FakeImap())
    pdf = tmp_path / "r.pdf"; pdf.write_bytes(b"%PDF-1.4 fake")
    mid = outreach.save_gmail_draft(cfg, {"contact_email": "sample-3b028eda@example.com", "contact_name": "Marie Romero",
                                          "subject": "Hi", "body": "Body text", "resume_path": str(pdf)})
    assert mid.startswith("<")
    kinds = [c[0] for c in calls]
    assert kinds == ["login", "append", "logout"]
    folder, flags, raw = calls[1][1], calls[1][2], calls[1][3]
    assert folder == '"[Gmail]/Drafts"' and "Draft" in flags
    assert b"To: Marie Romero <sample-3b028eda@example.com>" in raw and b"Subject: Hi" in raw and b"application/pdf" in raw
    assert not any(k == "send" for k in kinds)


def test_mailbox_contacts_ignore_another_mailboxs_board(cfg, monkeypatch):
    """A second workspace with its own connected mailbox must not inherit the owner's recruiter
    threads: the board cache is hers, so it counts only when its account is this mailbox."""
    monkeypatch.setattr(outreach.mailcode, "available", lambda c: True)
    board = {"account": "sample-5fbfd918@example.com", "rows": [
        {"company": "Base Power", "last_from": "Leo <sample-52ae0d53@example.com>", "dead": False, "cold": False}]}
    monkeypatch.setattr(outreach.boardmod, "load", lambda c, **kw: board)
    mine = {**cfg, "mailcode": {**(cfg.get("mailcode") or {}), "email": "sample-5fbfd918@example.com"}}
    his = {**cfg, "mailcode": {**(cfg.get("mailcode") or {}), "email": "sample-3a807109@example.com", "extra_accounts": []}}
    assert [c["email"] for c in outreach.mailbox_contacts(mine)] == ["sample-52ae0d53@example.com"]
    assert outreach.mailbox_contacts(his) == []
