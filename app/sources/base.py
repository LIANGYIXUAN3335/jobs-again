"""Shared fetcher plumbing: Posting model, polite HTTP, HTML→text."""
from __future__ import annotations

import html
import html.parser
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

ATS_CODES = {"greenhouse": "gh", "lever": "lv", "ashby": "ab",
             "workday": "wd", "smartrecruiters": "sr", "rippling": "rp",
             "avature": "av"}

USER_AGENT = "jobscan/0.1 (personal job-search tool; low volume)"


@dataclass
class Posting:
    ats: str          # greenhouse | lever | ashby
    company: str      # canonical display name from config
    slug: str
    raw_id: str
    title: str
    location: str
    jd_text: str      # plain text, HTML stripped
    apply_url: str
    department: str = ""
    is_remote: bool = False
    # Boards whose list endpoint has no JD (Workday, Rippling, SmartRecruiters) set
    # jd_text="" and provide this callback; the scanner calls it only for postings that
    # survive the title/location filters, so we never bulk-fetch hundreds of details.
    fetch_jd: Callable[[], str] | None = field(default=None, repr=False, compare=False)

    @property
    def job_id(self) -> str:
        # Raw ATS ids are only unique per board — namespace them.
        return f"{ATS_CODES[self.ats]}:{self.slug}:{self.raw_id}"


def make_client(http_cfg: dict) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(float(http_cfg.get("timeout_seconds", 30)), connect=10.0),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


def polite_get(client: httpx.Client, url: str, retries: int = 2) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = client.get(url)
            r.raise_for_status()
            return r
        except Exception as exc:  # noqa: BLE001 — retry any transport/HTTP error
            last_exc = exc
            if attempt < retries:
                time.sleep(2.0 * (2 ** attempt))
    raise last_exc  # type: ignore[misc]


class _TextExtractor(html.parser.HTMLParser):
    _SKIP = {"script", "style"}
    _BREAK = {"p", "br", "li", "ul", "ol", "div", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    # Greenhouse returns the whole document HTML-escaped (&lt;p&gt;...).
    if "<" not in raw and "&lt;" in raw:
        raw = html.unescape(raw)
    parser = _TextExtractor()
    parser.feed(raw)
    text = parser.text()
    text = re.sub(r"[ \t\r]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()
