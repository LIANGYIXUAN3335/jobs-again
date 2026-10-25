"""Which companies is the owner already interviewing with?

A company with a technical interview in the last 90 days or still ahead gets no new automatic
application. A recruiter call does not count. Plain rules read the calendar, the recap table and
the JobScan stage; no model is involved. The board shows each verdict so the owner can overrule it.
"""
from __future__ import annotations

import datetime
import json
import re
import threading
import time
from pathlib import Path

from . import config as cfgmod
from . import gcal

_root = cfgmod.ROOT / "data" / "agent_lab"
_lock = threading.RLock()
WINDOW_DAYS = 90
AHEAD_DAYS = 60
STALE_DAYS = 7
SEED_COPY = "seed"  # a flat file's events, kept beside the fresh copies until every calendar has been read
LONG_MINUTES = 45   # "Interview with X": a 30-minute slot is a recruiter, 45 minutes or more is an engineer

# The owner's own study blocks and prep sessions mention interviews too.
_NOISE = re.compile(r"^\s*(?:📚|🎤|🧱|🧑|📝|📌\s*念|cancell?ed\b)|准备|复盘|冲刺|口练|热身|念稿|\bprep\b|\bmock\b|\bdrill\b", re.I)
_TECHNICAL = re.compile(r"technical|\btech (?:screen|interview)|coding|system design|on-?site|in[- ]person|virtual interview|"
                        r"hiring manager|\bhm\b|\bloop\b|\bpanel\b|assessment|\bt[123]\b|leads? chat|^\s*engineer interview|技术|"
                        r"\bpair(?:ing)?[- ]?(?:program|session|interview)|\bkarat\b|\bsuper\s?day\b", re.I)
_RECRUITER = re.compile(r"recruiter|\bintro\b|introduct|first chat|engagement call|candidate discussion|staffing", re.I)
_SCREENING_CALL = re.compile(r"初筛|动机筛选")   # what a recap calls a first conversation about her background and motives
_ROUND = re.compile(r"\bround\s*(?:#?\d|one|two|three|four)\b|\b(?:final|last|first|second|third|fourth|\d(?:st|nd|rd|th))[- ]round\b", re.I)
_INTERVIEW = re.compile(r"\binterview|\bscreen\b|面试", re.I)
# A friend's interview kept in the recap folder, or a practice. "Peer EM" and "peer conflict" are her own rounds.
_PEER = re.compile(r"\barya\b|\bpeer\s*[:：]|[(（]\s*peer\s*[)）]|同伴|\bmock\b|another candidate", re.I)
_NOT_EMPLOYER = re.compile(r"\barya\b|[(（]\s*peer\s*[)）]|教练|coach|^unknown|^mock\b", re.I)

# A company whose name is an everyday word in a meeting title never matches by name alone.
_NOT_A_NAME = {"interview", "technical", "onsite", "screen", "engineer", "software", "recruiter", "virtual", "call", "chat",
               "confirmed", "meeting", "person", "phone", "first", "coding", "system", "design", "assessment", "panel", "loop",
               "your", "with", "intro", "introduction", "google meet", "senior", "manager", "staff", "next", "steps", "follow",
               "round", "final", "last", "pair", "pairing", "programming", "session", "superday", "karat", "hiring", "engineering"}
_VENUE = re.compile(r"google meet|microsoft teams|\bzoom\b", re.I)
_OWNER = re.compile(r"\b(?:alex|casey)\b", re.I)   # scheduling tools write "Interview with <candidate>"
_SELF = {"sample-5fbfd918@example.com", "sample-1bb0aadb@example.com"}
# Scheduling tools and staffing firms send invites for someone else's opening.
_GO_BETWEEN_MAIL = (r"goodtime|calendly|cronofy|candidate|karat|hackerrank|codesignal|coderpad|hirevue|phenom|paradox|gem|modernloop|lu|"
                    r"insightglobal|toptal|erginc|haventalent|hugrecruitment|apera|weekday|paraform")
# Read against the registrable domain: mailboxes, hiring tools and schedulers, then staffing firms. None of them is the employer.
_NOT_EMPLOYER_MAIL = re.compile(r"(?:gmail|googlemail|google|outlook|hotmail|yahoo|icloud|"
                                r"ashbyhq|ashby|greenhouse|lever|workday|myworkday|icims|smartrecruiters|" + _GO_BETWEEN_MAIL + r")\.", re.I)
_GO_BETWEEN = re.compile(r"(?:" + _GO_BETWEEN_MAIL + r")\.", re.I)
_GO_BETWEEN_TITLE = re.compile(r"\b(?:insight\s?global|toptal|erg\s?inc|haven\s?talent|hug\s?recruitment|paraform)\b", re.I)
_TWO_PART_SUFFIX = {"co.uk", "org.uk", "ac.uk", "com.au", "net.au", "co.nz", "co.jp", "co.kr", "co.in", "co.il", "co.za",
                    "com.cn", "com.hk", "com.sg", "com.br", "com.mx", "com.tw", "com.tr"}
# What may follow a name and leave the same company: "Otter" and "Otter.ai" are one, but "Cohere" and "Cohere Health",
# "Weave" and "WeaveGrid", "Scale" and "Scaleway" are not. Any other pair goes in config interview_ledger.aliases.
_SAME_COMPANY_TAIL = re.compile(r"(?:ai|io|co|com|inc|incorporated|llc|ltd|limited|corp|corporation|company|labs|technologies|technology|"
                                r"systems|networks|industries|motors|webservices|aws)+")
_SHORTEST_PREFIX = 5   # below this a tail proves nothing: "Open" + "AI" is OpenAI


def _name(cfg: dict) -> str:
    return re.sub(r"\W+", "_", cfgmod.workspace_name(cfg)).strip("_").lower()


def calendar_path(cfg: dict) -> Path:
    return Path(_root) / f"calendar_events.{_name(cfg)}.json"


def overrides_path(cfg: dict) -> Path:
    return Path(_root) / f"interview_overrides.{_name(cfg)}.json"


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def _read(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def classify(title: str, minutes: int | None) -> str | None:
    """technical, recruiter, or None when the event is not an interview at all."""
    title = title or ""
    if _NOISE.search(title):
        return None
    if _TECHNICAL.search(title):
        return "technical"
    if _RECRUITER.search(title):
        return "recruiter"
    if _ROUND.search(title):
        return "technical"
    if _INTERVIEW.search(title):
        return "technical" if (minutes or 0) >= LONG_MINUTES else "recruiter"
    return None


def classify_round(round_type: str) -> str | None:
    """A recap exists because a real conversation happened, so only screening wording makes it light."""
    round_type = round_type or ""
    if _PEER.search(round_type):
        return None
    # 技术背景 is talk about her background and 非技术 says the opposite. 技术初筛 stays a technical screen.
    tested = re.sub(r"技术背景|非技术", " ", round_type)
    if (_RECRUITER.search(round_type) or _SCREENING_CALL.search(round_type)) and not re.search(r"technical|coding|on-?site|\bloop\b|system design|技术", tested, re.I):
        return "recruiter"
    return "technical"


def slim_event(event: dict) -> dict | None:
    """Keep what the rules read: a Google event becomes a title, two times and the other side's mail domains."""
    start, end = event.get("start") or {}, event.get("end") or {}
    start_at, end_at = start.get("dateTime") or start.get("date"), end.get("dateTime") or end.get("date")
    if event.get("status") == "cancelled" or not start_at or not end_at:
        return None
    mails = [p.get("email") or "" for p in event.get("attendees") or []] + [(event.get("organizer") or {}).get("email") or ""]
    domains = sorted({m.rsplit("@", 1)[1].lower() for m in mails if "@" in m and m.lower() not in _SELF})
    return {"id": event.get("id") or "", "title": event.get("summary") or "", "start": start_at, "end": end_at, "domains": domains}


def save_calendar(cfg: dict, events: list[dict], source: str, now: float | None = None) -> int:
    """One flat copy of unknown origin, the way the first seed was written. The next refresh takes it over."""
    slim = [e for e in map(slim_event, events) if e]
    with _lock:
        _write(calendar_path(cfg), {"fetched_at": now or time.time(), "source": source, "events": slim})
    return len(slim)


def _copies(saved: dict) -> dict[str, dict]:
    """The saved copies by calendar. A flat file counts as one copy, and it is read until every calendar has been."""
    if isinstance(saved.get("calendars"), dict):
        return saved["calendars"]
    return {SEED_COPY: {"fetched_at": saved.get("fetched_at"), "events": saved.get("events") or []}} if "events" in saved else {}


def _save_copies(cfg: dict, fresh: dict[str, list[dict]], now: float | None = None) -> dict:
    """Replace only the calendars that were read. The others keep their last copy, and the file says which those are."""
    stamp = now or time.time()
    wanted = sorted(_SELF)
    with _lock:
        saved = _read(calendar_path(cfg), {})
        copies = {} if all(c in fresh for c in wanted) else dict(_copies(saved))
        for calendar, events in fresh.items():
            copies[calendar] = {"fetched_at": stamp, "events": [e for e in map(slim_event, events) if e]}
        data = {"source": "google" if fresh else saved.get("source") or "", "wanted": wanted, "read": sorted(fresh), "calendars": copies}
        _write(calendar_path(cfg), data)
    return data


def _merged_events(copies: dict[str, dict]) -> list[dict]:
    """Each event once. An invite can sit on both calendars, and the fresher copy of it wins."""
    merged: dict = {}
    for copy in sorted(copies.values(), key=lambda c: c.get("fetched_at") or 0, reverse=True):
        for event in copy.get("events") or []:
            merged.setdefault(event.get("id") or (event.get("title"), event.get("start")), event)
    return list(merged.values())


def _unread_note(missing: list[str]) -> str:
    if not missing:
        return ""
    return f"{'、'.join(missing)} 这次没读到，新排的面试可能没算进来。别的日历和复盘照常算。到 /settings 检查 Google 授权后再刷新。"


def refresh_calendar(cfg: dict, now: datetime.datetime | None = None) -> dict:
    """Read both calendars with the stored token. The token dies every few days, and then the last copies stand."""
    service = gcal._service(cfg)
    if service is None:
        _save_copies(cfg, {})
        return {"ok": False, "error": "日历授权已失效：到 /settings 重新授权 Google 后再刷新。"}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    low = (now - datetime.timedelta(days=WINDOW_DAYS + 1)).isoformat()
    high = (now + datetime.timedelta(days=AHEAD_DAYS)).isoformat()
    fresh: dict[str, list[dict]] = {}
    for calendar in sorted(_SELF):
        try:
            events: list[dict] = []
            token = None
            while True:
                page = service.events().list(calendarId=calendar, timeMin=low, timeMax=high, singleEvents=True,
                                             orderBy="startTime", maxResults=250, pageToken=token).execute()
                events += page.get("items") or []
                token = page.get("nextPageToken")
                if not token:
                    break
            fresh[calendar] = events
        except Exception:  # noqa: BLE001 — one calendar without access must not hide the other
            continue
    saved = _save_copies(cfg, fresh)
    if not fresh:
        return {"ok": False, "error": "日历读取失败：到 /settings 重新授权 Google 后再刷新。"}
    return {"ok": True, "events": len(_merged_events(saved["calendars"])), "read": len(fresh), "wanted": len(saved["wanted"]),
            "error": _unread_note([c for c in saved["wanted"] if c not in fresh])}


def _all_day(event: dict) -> bool:
    return len(str(event.get("start") or "")) == 10   # a bare date, no time


def _minutes(event: dict) -> int | None:
    if _all_day(event):
        return None
    try:
        start = datetime.datetime.fromisoformat(event["start"])
        return int((datetime.datetime.fromisoformat(event["end"]) - start).total_seconds() // 60)
    except (KeyError, ValueError, TypeError):
        return None


def _not_a_name(name: str) -> bool:
    name = name.strip().lower()
    return name in _NOT_A_NAME or all(word in _NOT_A_NAME for word in name.split()) or bool(_OWNER.search(name))


def _without_title_words(guess: str) -> str:
    """"Nimbus Final" from "Nimbus Final Round" is Nimbus, and "Software Engineer" alone is nobody."""
    words = guess.split()
    while words and words[-1].lower() in _NOT_A_NAME:
        words.pop()
    return " ".join(words)


def _registrable(domain: str) -> str:
    """interview.adobe.com is adobe.com. A subdomain names a tool or a department, never the employer."""
    labels = domain.lower().strip(".").split(".")
    return ".".join(labels[-3 if ".".join(labels[-2:]) in _TWO_PART_SUFFIX else -2:])


def _names_pattern(names: set[str]) -> re.Pattern | None:
    usable = sorted((n for n in names if len(n.strip()) >= 3 and not _not_a_name(n)), key=len, reverse=True)
    if not usable:
        return None
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(n.strip()) for n in usable) + r")(?!\w)", re.I)


def _arranged_by_go_between(event: dict) -> bool:
    return bool(_GO_BETWEEN_TITLE.search(event.get("title") or "")) or any(_GO_BETWEEN.match(_registrable(d)) for d in event.get("domains") or [])


def _company_of(event: dict, pattern: re.Pattern | None, named: dict, known: set[str], technical: bool = True) -> tuple[str, bool]:
    """The company and whether it is more than a guess. A mail domain or a word in the title can be an agency or a person."""
    if named.get(event.get("id")):
        return named[event["id"]], True
    title = _VENUE.sub(" ", event.get("title") or "")
    match = pattern.search(title) if pattern else None
    if match:
        return match.group(1), True
    guesses = []
    for domain in event.get("domains") or []:
        base = _registrable(domain)
        if not _NOT_EMPLOYER_MAIL.match(base):
            guesses.append(base.split(".")[0].capitalize())
    match = re.search(r"(?:\bwith\b|\bat\b|@|\bw/)\s+([A-Z][\w.&-]*(?:\s+[A-Z][\w.&-]*){0,2})", title)
    # On such an invite "with Brian" is the person on the call. A technical round still takes the guess: it blocks, and that is the safe side.
    if match and not technical and match[0].lower().startswith(("with", "w/")) and _arranged_by_go_between(event):
        match = None
    match = match or re.search(r"^\s*\[([^\]]+)\]", title) or \
        re.search(r"^\s*([A-Z][\w.&-]+(?:\s+[A-Z][\w.&-]+)?)\s+(?:[-–—|]\s*)?(?:\d\s*(?:st|nd|rd|th)\s+)?"
                  r"(?:In[- ]Person|On-?site|Technical|Recruiter|Phone|Hiring|Virtual|Interview|interview|面试|"
                  r"Round|round|Final|Superday|Pair|Karat)", title)
    if match:
        guesses.append(match.group(1).strip(" -|"))
    guesses = [g for g in map(_without_title_words, guesses) if g and not _not_a_name(g)]
    for guess in guesses:
        if is_blocked(guess, known):
            return guess, True
    return (guesses[0], False) if guesses else ("", False)


def _day(value: str | None) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _alias_table(cfg: dict) -> dict[str, str]:
    return (cfg.get("interview_ledger") or {}).get("aliases") or {}


def _aliases(cfg: dict) -> dict[str, str]:
    return {cfgmod.norm_key(k): cfgmod.norm_key(v) for k, v in _alias_table(cfg).items()}


def view(cfg: dict, conn, today: datetime.date | None = None, now: float | None = None) -> dict:
    """Everything the board shows, and the blocked set the Gatekeeper reads."""
    today = today or datetime.date.today()
    now = now or time.time()
    floor = today - datetime.timedelta(days=WINDOW_DAYS)
    overrides = _read(overrides_path(cfg), {})
    calendar = _read(calendar_path(cfg), {})
    copies = _copies(calendar)
    aliases = _aliases(cfg)
    named = overrides.get("event_company") or {}
    recaps = conn.execute("SELECT company, happened_on, round_type FROM interviews").fetchall()
    roles = {r[0] for r in conn.execute("SELECT DISTINCT company FROM jobs.roles WHERE company IS NOT NULL").fetchall()}
    pattern = _names_pattern(roles | {str(name) for pair in _alias_table(cfg).items() for name in pair})
    # Names something already vouches for: a posting, the alias table, a recap, or the owner herself.
    known = {cfgmod.norm_key(n) for n in roles} | set(aliases) | set(aliases.values()) | set(overrides.get("block") or {}) | \
        {cfgmod.norm_key(n) for n in named.values()} | {cfgmod.norm_key(r[0]) for r in recaps if not _NOT_EMPLOYER.search(r[0] or "")}
    technical: dict[str, dict] = {}
    recruiter: dict[str, dict] = {}
    unknown: list[dict] = []

    def note(table, company, day, title, source):
        key = cfgmod.norm_key(company)
        if not key or day < floor:
            return
        key = aliases.get(key, key)
        current = table.get(key)
        # Keep the latest evidence, because that is what the owner checks first.
        if current is None or day.isoformat() > current["date"]:
            table[key] = {"key": key, "company": company, "date": day.isoformat(), "title": title[:160], "source": source}

    for event in _merged_events(copies):
        day = _day(event.get("start"))
        kind = classify(event.get("title") or "", _minutes(event))
        if day is None or kind is None or day < floor:
            continue
        if _all_day(event) and kind != "technical":   # mostly reminders, so only a title that says technical counts
            continue
        company, sure = _company_of(event, pattern, named, known, technical=kind == "technical")
        if kind == "technical" and not sure:
            # A guess blocks anyway, since that is the safe side, but the real company may be another: ask the owner.
            shown = (event.get("title") or "")[:160] + (f"（先按 {company} 挡着）" if company else "")
            unknown.append({"event_id": event.get("id") or "", "title": shown, "date": day.isoformat(), "minutes": _minutes(event), "guess": company})
        if company:
            note(technical if kind == "technical" else recruiter, company, day, event.get("title") or "", "日历")
    for row in recaps:
        day = _day(row[1])
        kind = classify_round(row[2])
        if day is not None and kind is not None and not _NOT_EMPLOYER.search(row[0] or ""):
            note(technical if kind == "technical" else recruiter, row[0], day, row[2] or "", "复盘")
    for row in conn.execute("SELECT r.company, s.stage, s.last_contact_at FROM role_state s JOIN jobs.roles r ON r.job_id=s.job_id "
                            "WHERE s.stage IN ('onsite','offer')").fetchall():
        day = _day(row[2])
        if day is not None:
            note(technical, row[0], day, f"JobScan 阶段：{row[1]}", "JobScan")
    for key, item in (overrides.get("block") or {}).items():
        technical[key] = {"key": key, "company": item.get("company") or key, "date": item.get("date") or "", "title": "你手动加的", "source": "手动"}
    allowed = overrides.get("allow") or {}
    blocked = [v for k, v in technical.items() if not is_blocked(k, set(allowed))]
    # The oldest copy is the honest age: a fresh calendar must not hide one that stopped being read.
    fetched = min((c["fetched_at"] for c in copies.values() if isinstance(c.get("fetched_at"), (int, float))), default=None)
    age = (now - fetched) / 86400 if fetched is not None else None
    missing = [c for c in calendar.get("wanted") or [] if c not in (calendar.get("read") or [])]
    # A seed is what the board leans on when Google cannot be read, so its age says nothing about what is missing from it.
    copy_note = "这份日历是副本，只有 candidate 这一个账号的。重新授权 Google 之前，新排的面试看不到。" if calendar.get("source") == SEED_COPY else ""
    return {"calendar": {"fetched_at": fetched, "source": calendar.get("source") or "", "age_days": None if age is None else round(age, 1),
                         "stale": age is None or age > STALE_DAYS or bool(missing), "missing": missing, "copy_note": copy_note},
            "refresh_error": _unread_note(missing),
            "blocked": sorted(blocked, key=lambda v: v["date"], reverse=True),
            "recruiter_only": sorted((v for k, v in recruiter.items() if not is_blocked(k, set(technical))), key=lambda v: v["date"], reverse=True),
            "allowed": [{"key": k, "company": technical[k]["company"] if k in technical else v.get("company", k)} for k, v in allowed.items()],
            "unknown": sorted(unknown, key=lambda v: v["date"], reverse=True)}


def _same_company(a: str, b: str) -> bool:
    short, long = sorted((a, b), key=len)
    return len(short) >= _SHORTEST_PREFIX and long.startswith(short) and bool(_SAME_COMPANY_TAIL.fullmatch(long[len(short):]))


def is_blocked(norm: str, blocked: set[str]) -> bool:
    """"Otter" and "Otter.ai" are one company, whichever is on the calendar. An alias needs blocked_keys, which knows the table."""
    norm = cfgmod.norm_key(norm)
    return norm in blocked or any(_same_company(norm, k) for k in blocked)


def blocked_keys(cfg: dict, conn) -> set[str]:
    keys = {item["key"] for item in view(cfg, conn)["blocked"]}
    # A posting can carry either side of an alias: the "Everpure" role when the row is under Pure Storage.
    return keys | {name for name, target in _aliases(cfg).items() if is_blocked(target, keys)}


def override(cfg: dict, company: str, action: str) -> None:
    key = cfgmod.norm_key(company)
    if not key or action not in {"block", "allow", "clear"}:
        raise ValueError("公司名或操作不对。")
    key = _aliases(cfg).get(key, key)   # the board sends the name on the row, and the row sits under the alias target
    with _lock:
        data = _read(overrides_path(cfg), {})
        block, allow = data.setdefault("block", {}), data.setdefault("allow", {})
        block.pop(key, None)
        allow.pop(key, None)
        if action != "clear":
            (block if action == "block" else allow)[key] = {"company": company.strip(), "date": datetime.date.today().isoformat()}
        _write(overrides_path(cfg), data)


def name_event(cfg: dict, event_id: str, company: str) -> None:
    if not event_id or not company.strip():
        raise ValueError("要写公司名。")
    with _lock:
        data = _read(overrides_path(cfg), {})
        data.setdefault("event_company", {})[event_id] = company.strip()
        _write(overrides_path(cfg), data)
