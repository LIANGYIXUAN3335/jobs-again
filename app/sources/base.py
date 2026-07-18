"""Shared fetcher plumbing: Posting model, polite HTTP, HTML→text, posting-date parsing."""
from __future__ import annotations

import datetime
import html
import html.parser
import re
import time
from dataclasses import dataclass, field
from typing import Callable
from zoneinfo import ZoneInfo

import httpx

ATS_CODES = {"greenhouse": "gh", "lever": "lv", "ashby": "ab",
             "workday": "wd", "smartrecruiters": "sr", "rippling": "rp",
             "avature": "av"}

USER_AGENT = "jobscan/0.1 (personal job-search tool; low volume)"

_LA = ZoneInfo("America/Los_Angeles")


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
    # When the board says the role went live (YYYY-MM-DD), or None on boards that don't say.
    # Feeds the freshness bonus in scoring.freshness(); first_seen is only a fallback proxy.
    posted_at: str | None = None
    # Boards whose list endpoint has no JD (Workday, Rippling, SmartRecruiters) set
    # jd_text="" and provide this callback; the scanner calls it only for postings that
    # survive the title/location filters, so we never bulk-fetch hundreds of details.
    fetch_jd: Callable[[], str] | None = field(default=None, repr=False, compare=False)

    @property
    def job_id(self) -> str:
        # Raw ATS ids are only unique per board — namespace them.
        return f"{ATS_CODES[self.ats]}:{self.slug}:{self.raw_id}"


def today_la() -> datetime.date:
    return datetime.datetime.now(_LA).date()


def posted_day(value) -> str | None:
    """Normalize whatever a board calls its publish date into YYYY-MM-DD.

    Handles the three shapes the supported ATSs actually return: ISO timestamps
    (Greenhouse `first_published`, Ashby `publishedAt`, SmartRecruiters `releasedDate`),
    epoch milliseconds (Lever `createdAt`), and epoch seconds. Anything unrecognised
    returns None — an absent date is a fine outcome, a wrong one silently mis-ranks.
    """
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = float(value)
        if n > 1e11:            # milliseconds
            n /= 1000.0
        if n < 1e8:             # not a plausible epoch second (before 1973)
            return None
        try:
            return datetime.datetime.fromtimestamp(n, tz=_LA).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    m = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", str(value))
    if not m:
        return None
    try:
        return datetime.date(*(int(g) for g in m.groups())).isoformat()
    except ValueError:
        return None


# Workday has no date field — only the human string it renders on the card.
_REL_POSTED = re.compile(
    r"posted\s+(?:(today)|(yesterday)|(\d+)\+?\s*(day|week|month|year)s?\s*ago)", re.I)
_REL_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


def posted_day_relative(text: str, today: datetime.date | None = None) -> str | None:
    """Parse Workday's 'Posted Today' / 'Posted 5 Days Ago' / 'Posted 30+ Days Ago'.

    '30+' is a floor, not a measurement — reading it as exactly 30 is safe because
    anything that old is far past the freshness window and scores 0 either way.
    """
    m = _REL_POSTED.search(text or "")
    if not m:
        return None
    today = today or today_la()
    if m.group(1):
        days = 0
    elif m.group(2):
        days = 1
    else:
        days = int(m.group(3)) * _REL_UNIT_DAYS[m.group(4).lower()]
    return (today - datetime.timedelta(days=days)).isoformat()


# Hand-captured postings (Apple, Google, …): their pages print the date in the body.
_JD_POSTED = re.compile(
    r"posted[:\s]+(?:on\s+)?"
    r"(?:(\d{4}-\d{2}-\d{2})"
    r"|([A-Z][a-z]{2,8})\.?\s+(\d{1,2}),?\s+(\d{4})"
    r"|(\d{1,2})\s+([A-Z][a-z]{2,8})\.?,?\s+(\d{4}))", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def posted_day_from_jd(jd_text: str) -> str | None:
    """Sniff 'Posted: Jul 24, 2026' out of a JD body, for boards we can't call an API on."""
    m = _JD_POSTED.search(jd_text or "")
    if not m:
        return None
    if m.group(1):
        return posted_day(m.group(1))
    if m.group(2):
        mon, day, year = m.group(2), m.group(3), m.group(4)
    else:
        day, mon, year = m.group(5), m.group(6), m.group(7)
    idx = _MONTHS.get(mon[:3].lower())
    if not idx:
        return None
    try:
        return datetime.date(int(year), idx, int(day)).isoformat()
    except ValueError:
        return None


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
