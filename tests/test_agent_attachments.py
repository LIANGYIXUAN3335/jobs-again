import hashlib
import json

import pytest

from app.agent_review import Snapshot, review


PDF_BYTES = b"%PDF-1.7 confirmed resume bytes"
PDF_HASH = hashlib.sha256(PDF_BYTES).hexdigest()
PDF_NAME = "Casey Example - Example - Backend.pdf"


def evidence():
    snapshot = Snapshot(
        url="https://jobs.ashbyhq.com/example/role/application", document_id="page-1",
        coverage="complete", fields=[dict(id="resume", label="Resume", kind="file",
                                          value=PDF_NAME, required=True)],
        attachments=[dict(field_id="resume", name=PDF_NAME, size=len(PDF_BYTES), sha256=PDF_HASH)],
    )
    context = {
        "requires_attachment_review": True,
        "expected_resume": {"sha256": PDF_HASH, "name": PDF_NAME,
                            "text": "Casey Example | sample-5fbfd918@example.com", "identity_verified": True},
        "facts": [{"id": "ready-resume-text", "labels": [],
                   "value": "Casey Example | sample-5fbfd918@example.com"}],
    }
    return snapshot, context


def verdict(snapshot, context):
    def model_call(_cfg, payload):
        assert payload["context"] == context
        return json.dumps({"decision": "pass", "issues": [], "summary": "Checked actual resume text.",
                           "checked_field_ids": [f.id for f in snapshot.fields]}), "test-reviewer"
    return review(snapshot, context, {}, model_call)


def test_matching_native_file_bytes_can_pass_independent_review():
    snapshot, context = evidence()
    result = verdict(snapshot, context)
    assert result["decision"] == "pass"
    assert result["model_status"] == "completed"
    assert result["issues"] == []


@pytest.mark.parametrize("change", [
    {"sha256": ""},
    {"sha256": hashlib.sha256(b"different bytes, same filename").hexdigest()},
    {"name": "another-resume.pdf"},
    {"size": 0},
    {"field_id": "missing-field"},
])
def test_missing_or_mismatched_byte_evidence_cannot_be_overruled_by_model(change):
    snapshot, context = evidence()
    for key, value in change.items():
        setattr(snapshot.attachments[0], key, value)
    assert verdict(snapshot, context)["decision"] == "needs_user"


@pytest.mark.parametrize("change", [
    {"identity_verified": False}, {"identity_verified": "true"},
    {"sha256": ""}, {"name": ""}, {"text": ""},
])
def test_incomplete_expected_resume_stays_blocked(change):
    snapshot, context = evidence()
    context["expected_resume"].update(change)
    assert verdict(snapshot, context)["decision"] == "needs_user"


def test_legacy_context_still_requires_attachment_review():
    snapshot, context = evidence()
    del context["expected_resume"]
    assert verdict(snapshot, context)["decision"] == "needs_user"


def test_filename_without_native_file_bytes_is_not_proof():
    snapshot, context = evidence()
    snapshot.attachments = []
    assert verdict(snapshot, context)["decision"] == "needs_user"


def test_unknown_extra_file_blocks_even_when_resume_matches():
    snapshot, context = evidence()
    snapshot.fields.append(snapshot.fields[0].model_copy(update={"id": "extra", "label": "Extra", "value": "unknown.pdf"}))
    snapshot.attachments.append(snapshot.attachments[0].model_copy(update={"field_id": "extra", "name": "unknown.pdf"}))
    assert verdict(snapshot, context)["decision"] == "needs_user"


def test_unreported_extra_file_blocks_even_when_resume_matches():
    snapshot, context = evidence()
    snapshot.fields.append(snapshot.fields[0].model_copy(update={"id": "extra", "label": "Extra", "value": "unknown.pdf"}))
    assert verdict(snapshot, context)["decision"] == "needs_user"


def test_optional_empty_file_control_does_not_invent_an_extra_attachment():
    snapshot, context = evidence()
    snapshot.fields.append(snapshot.fields[0].model_copy(update={"id": "extra", "label": "Cover letter", "value": "", "required": False}))
    assert verdict(snapshot, context)["decision"] == "pass"


@pytest.mark.parametrize("kind,readable,coverage", [("custom", False, "partial"), ("file", True, "partial")])
def test_matching_bytes_do_not_override_unreadable_controls(kind, readable, coverage):
    snapshot, context = evidence()
    snapshot.fields[0].kind = kind
    snapshot.fields[0].readable = readable
    snapshot.coverage = coverage
    assert verdict(snapshot, context)["decision"] == "needs_user"


def test_attachment_bytes_are_included_in_approval_fingerprint():
    snapshot, _ = evidence()
    before = snapshot.fingerprint()
    snapshot.attachments[0].sha256 = hashlib.sha256(b"different bytes").hexdigest()
    assert snapshot.fingerprint() != before
