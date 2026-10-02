"""Every HTML page's inline <script> must parse as JavaScript (node --check).

2026-09-23: the /outreach template had a `\\'` inside a plain Python string; the browser got `''`,
the script died with "Unexpected string" and the page sat on "loading…" forever on both
workspaces. No Python test could see it — only a JS parser can.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import web

PAGES = ["/outreach", "/companies", "/settings", "/"]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("path", PAGES)
def test_inline_scripts_parse(path):
    html = TestClient(web.app).get(path).text
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert scripts, f"{path}: no inline script found"
    for i, js in enumerate(scripts):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(js); name = f.name
        try:
            r = subprocess.run(["node", "--check", name], capture_output=True, text=True)
        finally:
            Path(name).unlink(missing_ok=True)
        assert r.returncode == 0, f"{path} script #{i}: {r.stderr.strip()[:400]}"
