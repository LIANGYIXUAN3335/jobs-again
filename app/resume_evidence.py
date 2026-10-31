"""Read a ready PDF once, binding extracted identity evidence to the exact bytes."""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path


def inspect_resume(path: Path, name: str, profile: dict) -> dict | None:
    if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        return None
    binary = shutil.which("pdftotext")
    if not binary and Path("/opt/homebrew/bin/pdftotext").is_file():
        binary = "/opt/homebrew/bin/pdftotext"
    if not binary:
        return None
    data = path.read_bytes()
    if not data.startswith(b"%PDF-"):
        return None
    try:
        result = subprocess.run([binary, "-layout", "-", "-"], input=data,
                                capture_output=True, timeout=10, check=True)
        text = result.stdout.decode("utf-8").strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if not text or len(text) > 40000:
        return None
    full_name = " ".join(str(profile.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
    email = str(profile.get("email") or "").strip().lower()
    phone = re.sub(r"\D", "", str(profile.get("phone") or ""))[-10:]
    header = re.sub(r"\s+", " ", text[:1000]).lower()
    phone_lines = [re.sub(r"\D", "", line) for line in text[:1000].splitlines()]
    name_match = re.search(r"(?<![\w])" + re.escape(full_name.lower()) + r"(?![\w])", header) if full_name else None
    email_match = re.search(r"(?<![\w.+\-@])" + re.escape(email) + r"(?![\w.\-@])", header) if email else None
    verified = bool(name_match and email_match and len(phone) == 10
                    and any(phone in line for line in phone_lines))
    return {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "size":len(data),
            "text": text, "identity_verified":verified}
