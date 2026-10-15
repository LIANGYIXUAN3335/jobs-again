"""Record explicit employer closure evidence for one currently new posting only."""
from __future__ import annotations

import datetime
import re
from urllib.parse import parse_qsl, urlsplit

from . import config as cfgmod
from . import store


_CLOSED = re.compile(
    r"\b(?:this|the|that)\s+(?:job|position|role|posting|opportunity)(?:\s+(?:posting|opening))?"
    r"(?:\s+you(?:'re| are) looking for)?\s+(?:is(?: now)?|was|has been|has)\s+"
    r"(?:no longer (?:available|open|active|accepting applications)|not available|closed|filled|removed|expired|unavailable)\b|"
    r"\b(?:we(?:'re| are)?\s+)?(?:are\s+)?no longer accepting applications (?:for|to) (?:this|the) (?:job|position|role)\b|"
    r"^(?:(?:this|the)\s+)?(?:job(?: posting)?|position|role|vacancy)\s+(?:(?:is|has been|has)\s+)?"
    r"(?:not found|filled|closed|expired)[.!]?$|"
    r"^(?:(?:we are|we're)\s+)?no longer accepting applications[.!]?$|"
    r"^applications (?:for this (?:job|position|role) )?(?:are|have been) closed[.!]?$|"
    r"(?:该|此|这个)?(?:职位|岗位|招聘职位)(?:已|已经)(?:下架|关闭|过期|停止招聘|结束招聘)|招聘已结束", re.I)
_UNCERTAIN = re.compile(
    r"\b(?:unknown|uncertain|not sure|unable to determine|could not confirm|cannot confirm|unconfirmed)\b|"
    r"未知|无法确认|未确认|不能确认|不确定|可能.{0,15}(?:下架|关闭|过期)", re.I)
_ACCESS = re.compile(
    r"\b(?:log[ -]?in|sign[ -]?in|captcha|authentication|access denied|forbidden)\b|"
    r"\bverify\b.{0,25}\b(?:human|identity)\b|登录|验证码|验证身份|验证您是人类", re.I)
_TRACKING_QUERY_KEYS = {"source", "src", "ref", "referrer", "referral", "gh_src", "lever-source", "lever-origin"}


def _role_url(url):
    if not isinstance(url, str) or len(url) > 3000:
        raise ValueError("Invalid role URL")
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid role URL")
    origin = (parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80))
    path = parsed.path.rstrip("/")
    path = re.sub(r"/(?:application|apply)$", "", path)
    # Query-carried job IDs (e.g. gh_jid) are part of identity. Only known tracking
    # parameters may differ; matching a generic careers path alone is insufficient.
    query = tuple(sorted((key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                         if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS))
    return origin, path, query


REFUSAL_COOLDOWN_DAYS = 90
REFUSED_PREFIX = "雇主拒收："   # extension/form_agent.js writes this before the employer's own words


def record_refused(cfg: dict, job: dict, evidence: str) -> dict:
    """The employer refused this application in its own words: close it, never as applied."""
    if not isinstance(evidence, str) or not evidence.startswith(REFUSED_PREFIX) or len(evidence) > 3000:
        raise ValueError("Explicit refusal evidence is required")
    words = evidence[len(REFUSED_PREFIX):]
    result = _close_new_role(cfg, job, "skip", "雇主拒收", words)
    # This employer has said no, so its other roles would meet the same answer: freeze the company.
    today = datetime.date.fromisoformat(store.la_today())
    until = (today + datetime.timedelta(days=refusal_days(words))).isoformat()
    conn = store.connect_view(cfg)
    try:
        # A shorter refusal must not cut short a freeze already in place. Keeping the later date belongs
        # here: in the store's own write the later decision wins, which is what the manual UI means.
        held = store.manual_cooldowns(conn).get(cfgmod.norm_key(job["company"]))
        result["cooldown"] = held if held and held["until"] >= until else store.set_company_cooldown(
            conn, job["company"], until=until, reason=REFUSED_PREFIX + words[:300])
    finally:
        conn.close()
    return result


_SPELLED = {word: value for value, word in enumerate(
    "one two three four five six seven eight nine ten eleven twelve".split(), start=1)}
_SPELLED.update(a=1, an=1)
_VAGUE = {"few": 3, "several": 6}   # an estimate: the employer named no number, and it never shortens the 90 days
_DAYS_IN = {"day": 1, "week": 7, "month": 30, "year": 365}
_PERIOD = re.compile(r"\b(?:a\s+)?(\d{1,3}|few|several|" + "|".join(_SPELLED) + r")[\s-]*(day|week|month|year)s?\b(?!\s+(?:of\b|ago\b|old\b|a\s+week\b|per\s+week\b))", re.I)
# A sentence about staying away. "This role needs 5 years of experience" and "posted 2 days ago" name a period and say nothing about that.
_ABOUT_STAYING = re.compile(r"\b(?:wait|re-?appl\w*|apply\s+again|applying\s+again|try\s+again|come\s+back|cool(?:ing)?(?:[- ]?(?:down|off))?|frozen|freeze|limit\w*|too\s+many|"
                            r"per\s+\d|in\s+the\s+(?:last|past)|for\s+the\s+next)\b", re.I)
_MONTHS = {name: number for number, name in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_MONTH_DAY = re.compile(r"\b" + _MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d\d))?\b", re.I)
_MONTH_YEAR = re.compile(r"\b" + _MONTH + r"\s+(20\d\d)\b", re.I)
_ISO_DATE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b")
_YEAR = re.compile(r"\b(after|until|before|in)\s+(20\d\d)\b(?![-/])", re.I)
_LONGEST_DATE_DAYS = 730   # page text is not trusted to freeze a company for good


def _dates_named(sentence: str, today: datetime.date) -> list[int]:
    """Days until each date this sentence names. One already past says nothing about staying away."""
    dates = []

    def add(year, month, day):
        try:
            dates.append(datetime.date(year, month, day))
        except ValueError:
            pass
    for month, day, year in _MONTH_DAY.findall(sentence):
        number, day_ = _MONTHS[month.lower()[:3]], int(day)
        if year:
            add(int(year), number, day_)
        else:   # "on March 3": the next one
            add(today.year + (1 if (number, day_) < (today.month, today.day) else 0), number, day_)
    for month, year in _MONTH_YEAR.findall(sentence):
        number = _MONTHS[month.lower()[:3]]   # "after December 2027": all of that month is still too early
        add(int(year) + (number == 12), number % 12 + 1, 1)
    for year, month, day in _ISO_DATE.findall(sentence):
        add(int(year), int(month), int(day))
    for month, day, year in _US_DATE.findall(sentence):
        add(int(year), int(month), int(day))
    for word, year in _YEAR.findall(sentence):
        add(int(year) + (word.lower() == "after"), 1, 1)
    return [min((date - today).days, _LONGEST_DATE_DAYS) for date in dates if date > today]


def _stay_days(text: str, today: datetime.date) -> list[int]:
    days = []
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text):
        if not _ABOUT_STAYING.search(sentence):
            continue
        for number, unit in _PERIOD.findall(sentence):
            word = number.lower()
            length = (_VAGUE.get(word) or _SPELLED.get(word) or int(number)) * _DAYS_IN[unit.lower()]
            days.append(min(max(length, 1), 365) if word not in _VAGUE else max(min(length, 365), REFUSAL_COOLDOWN_DAYS))
        days += _dates_named(sentence, today)
    return days


def refusal_days(words: str, today: datetime.date | None = None) -> int:
    """How long to leave a refusing employer alone: the longest stay it names, else 90 days.

    A refusal can name a counting window as well ("2 applications per 30 days; reapply in 6 months"). The first line is the
    refusal itself. The extension adds a line after it only if that line says how long to stay away, and such a line
    can lengthen the 90 days but never shorten them: a short freeze puts the company back in the queue almost at once.
    """
    today = today or datetime.date.fromisoformat(store.la_today())
    first, _, rest = words.partition("\n")
    own, later = _stay_days(first, today), _stay_days(rest, today)
    if not own and not later:
        return REFUSAL_COOLDOWN_DAYS
    return max(max(own, default=0), max(max(later), REFUSAL_COOLDOWN_DAYS) if later else 0)


def record_expired(cfg: dict, job: dict, evidence: str) -> dict:
    """Append exact observed closure text and close this job without applying it.

    The worker must supply explicit employer closure wording, not an inference from
    login requirements, a missing form, a generic error, or an uncertain result.
    URL/status checks and the one-row write share an immediate SQLite transaction.
    """
    if (not isinstance(job, dict) or not isinstance(job.get("job_id"), str) or
            not job["job_id"] or len(job["job_id"]) > 200 or job.get("status") != "new"):
        raise ValueError("A claimed new job is required")
    if not isinstance(evidence, str) or not 1 <= len(evidence.strip()) <= 3000:
        raise ValueError("Explicit closure evidence is required")
    evidence = evidence.strip()
    if not _CLOSED.search(evidence) or _UNCERTAIN.search(evidence) or _ACCESS.search(evidence):
        raise ValueError("Evidence does not establish an expired posting")
    return _close_new_role(cfg, job, "expired", "已下架", evidence)


def _close_new_role(cfg: dict, job: dict, status: str, label: str, evidence: str) -> dict:
    claimed_url = _role_url(job.get("url"))
    conn = store.connect_view(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        role = store.get_role(conn, job["job_id"])
        if not role or role["status"] != "new":
            raise ValueError("The stored role is no longer new")
        if _role_url(role.get("apply_url")) != claimed_url:
            raise ValueError("Closure evidence belongs to a different role URL")
        at = store.la_now_iso()
        previous = role.get("notes") or ""
        notes = previous + ("\n\n" if previous else "") + f"{label}（{at}）：{evidence}"
        # Update only this row's status and notes. No set_status cascade, application
        # timestamp, identity rewrite, duplicate link, or file deletion is involved.
        changed = conn.execute(
            "INSERT INTO role_state(job_id,status,status_updated_at,notes) VALUES(?,?,?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, "
            "status_updated_at=excluded.status_updated_at, notes=excluded.notes "
            "WHERE role_state.status='new'", (job["job_id"], status, at, notes))
        if changed.rowcount != 1:
            raise ValueError("The stored role changed before the closure write")
        conn.commit()
        return {"job_id": job["job_id"], "status": status, "evidence": evidence,
                "status_updated_at": at, "notes": notes}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
