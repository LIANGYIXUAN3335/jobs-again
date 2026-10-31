import hashlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import resume_evidence


PROFILE = {"first_name": "Casey", "last_name": "Example", "email": "casey@example.test",
           "phone": "202-555-0142"}
HEADER = "Casey Example\n+1 202-555-0142 | casey@example.test\n"


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "ready.pdf"
    path.write_bytes(b"%PDF-1.7\nfixture bytes passed to the extractor")
    return path


def extractor(monkeypatch, text=HEADER + "Backend Engineer"):
    calls = []
    monkeypatch.setattr(resume_evidence.shutil, "which", lambda _name: "/fixture/pdftotext")
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=text.encode("utf-8") if isinstance(text, str) else text)
    monkeypatch.setattr(resume_evidence.subprocess, "run", run)
    return calls


def test_hash_and_text_are_bound_to_the_same_pdf_bytes(pdf, monkeypatch):
    calls = extractor(monkeypatch)
    result = resume_evidence.inspect_resume(pdf, "Casey - Role.pdf", PROFILE)
    assert result["identity_verified"] is True
    assert result["name"] == "Casey - Role.pdf"
    assert result["sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert result["size"] == pdf.stat().st_size
    assert result["text"] == HEADER + "Backend Engineer"
    assert calls[0][0] == ["/fixture/pdftotext", "-layout", "-", "-"]
    assert calls[0][1] == {"input": pdf.read_bytes(), "capture_output": True, "timeout": 10, "check": True}


@pytest.mark.parametrize("header", [
    HEADER.replace("Casey Example", "Alex Example"),
    HEADER.replace("casey@example.test", "other@example.test"),
    HEADER.replace("202-555-0142", "202-555-0142"),
    # Substring matches must not authorize a different person's name or mailbox.
    HEADER.replace("Casey Example", "Casey Liangston"),
    HEADER.replace("casey@example.test", "notcasey@example.test"),
    HEADER.replace("casey@example.test", "casey@example.test.invalid"),
])
def test_mismatched_identity_never_verifies(pdf, monkeypatch, header):
    extractor(monkeypatch, header + "Backend Engineer")
    result = resume_evidence.inspect_resume(pdf, "resume.pdf", PROFILE)
    assert result is not None
    assert result["identity_verified"] is False


def test_identity_must_be_in_header_not_only_in_later_experience(pdf, monkeypatch):
    extractor(monkeypatch, "Other Person\n" + "experience " * 110 + HEADER)
    assert resume_evidence.inspect_resume(pdf, "resume.pdf", PROFILE)["identity_verified"] is False


def test_missing_file_or_non_pdf_is_not_sent_to_extractor(tmp_path, monkeypatch):
    calls = extractor(monkeypatch)
    assert resume_evidence.inspect_resume(tmp_path / "absent.pdf", "resume.pdf", PROFILE) is None
    wrong = tmp_path / "not-a-pdf.pdf"
    wrong.write_text("Not PDF bytes")
    assert resume_evidence.inspect_resume(wrong, "resume.pdf", PROFILE) is None
    assert calls == []


def test_oversize_pdf_is_not_sent_to_extractor(pdf, monkeypatch):
    calls = extractor(monkeypatch)
    with pdf.open("r+b") as stream:
        stream.truncate(10 * 1024 * 1024 + 1)
    assert resume_evidence.inspect_resume(pdf, "resume.pdf", PROFILE) is None
    assert calls == []


@pytest.mark.parametrize("output", [b"\xff", "", " " * 10, "x" * 40001])
def test_invalid_empty_and_excessive_extracted_text_fails_closed(pdf, monkeypatch, output):
    extractor(monkeypatch, output)
    assert resume_evidence.inspect_resume(pdf, "resume.pdf", PROFILE) is None


@pytest.mark.parametrize("error", [
    subprocess.TimeoutExpired("pdftotext", 10),
    subprocess.CalledProcessError(1, "pdftotext"),
    OSError("extractor unavailable"),
])
def test_extractor_failure_returns_no_evidence(pdf, monkeypatch, error):
    extractor(monkeypatch)
    def fail(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(resume_evidence.subprocess, "run", fail)
    assert resume_evidence.inspect_resume(pdf, "resume.pdf", PROFILE) is None


def test_missing_extractor_returns_no_evidence(pdf, monkeypatch):
    monkeypatch.setattr(resume_evidence.shutil, "which", lambda _name: None)
    real_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda self: False if str(self) == "/opt/homebrew/bin/pdftotext" else real_is_file(self))
    assert resume_evidence.inspect_resume(pdf, "resume.pdf", PROFILE) is None


def test_generated_pdf_is_independently_extracted_and_hashed(tmp_path):
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    if not shutil.which("pdftotext") and not Path("/opt/homebrew/bin/pdftotext").is_file():
        pytest.skip("pdftotext is unavailable")
    path = tmp_path / "generated-resume.pdf"
    document = canvas.Canvas(str(path))
    for y, line in [(760, "Casey Example"), (740, "202-555-0142 | casey@example.test"),
                    (700, "Backend Engineer - API contracts and reliability")]:
        document.drawString(60, y, line)
    document.save()
    result = resume_evidence.inspect_resume(path, "Casey - Example.pdf", PROFILE)
    assert result["identity_verified"] is True
    assert "API contracts and reliability" in result["text"]
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
