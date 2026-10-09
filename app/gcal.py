"""Google Calendar (read-only) — proposes real interview availability slots for auto-replies.
Headless via a stored OAuth token: the WORKSPACE token from /settings (app/google_auth.py,
data/<name>/secrets/google_token.json) when it carries the calendar scope, else the legacy
owner token (token_gcal.json from `python tools/gcal_auth.py`). Everything degrades to
None/empty until one of them exists. Never writes to the calendar.
"""
from __future__ import annotations

import datetime
import zoneinfo

from . import config as cfgmod
from . import google_auth

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_PATH = "/path/to/.config/jobscan-secrets/token_gcal.json"      # gitignored — created by tools/gcal_auth.py
CLIENT_PATH = "/path/to/.config/jobscan-secrets/gcal_client.json"    # gitignored — the OAuth client the user downloads


def _service(cfg: dict):
    """Build a Calendar API client from the stored token, or None if unavailable."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        return None
    # 1) This workspace's own token (one click on /settings). It must win over the legacy file
    #    whenever it has the calendar scope: the legacy token is the OWNER's calendar, and a
    #    second candidate's scheduling replies must never offer her free/busy as his.
    try:
        creds = google_auth.credentials(cfg)
        if (creds is not None and SCOPES[0] in (getattr(creds, "scopes", None) or [])
                and getattr(creds, "valid", True)):
            return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception:  # noqa: BLE001 — fall through to the legacy token
        pass
    # 2) Legacy: the token tools/gcal_auth.py wrote.
    tp = cfgmod.resolve_path(TOKEN_PATH)
    if not tp.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(tp), SCOPES)
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
            tp.write_text(creds.to_json())
        if not creds.valid:
            return None
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception:
        return None


def available(cfg: dict) -> bool:
    return _service(cfg) is not None


def _tz(cfg: dict):
    return zoneinfo.ZoneInfo((cfg.get("calendar") or {}).get("timezone", "America/Los_Angeles"))


def free_slots(cfg: dict, days: int = 6, count: int = 3) -> list[str]:
    """Return up to `count` human-readable free slots ('Tue Jul 22, 2:00–2:45 PM PT') within
    business hours over the next `days`, skipping weekends and anything marked busy. Empty
    list if the calendar isn't connected."""
    svc = _service(cfg)
    if svc is None:
        return []
    cal = cfg.get("calendar") or {}
    tz = _tz(cfg)
    start_h = int(cal.get("hours_start", 10))
    end_h = int(cal.get("hours_end", 17))
    dur = int(cal.get("slot_minutes", 45))
    label = cal.get("tz_label", "PT")

    now = datetime.datetime.now(tz)
    win_start = now + datetime.timedelta(hours=int(cal.get("min_notice_hours", 18)))
    win_end = now + datetime.timedelta(days=days)
    try:
        busy = svc.freebusy().query(body={
            "timeMin": win_start.isoformat(), "timeMax": win_end.isoformat(),
            "timeZone": str(tz), "items": [{"id": "primary"}],
        }).execute()["calendars"]["primary"]["busy"]
    except Exception:
        return []
    busy = [(datetime.datetime.fromisoformat(b["start"]).astimezone(tz),
             datetime.datetime.fromisoformat(b["end"]).astimezone(tz)) for b in busy]

    def overlaps(s, e):
        return any(s < be and bs < e for bs, be in busy)

    slots: list[str] = []
    day = win_start.date()
    while day <= win_end.date() and len(slots) < count:
        if day.weekday() < 5:                       # Mon–Fri only
            t = datetime.datetime.combine(day, datetime.time(start_h), tzinfo=tz)
            end_of_day = datetime.datetime.combine(day, datetime.time(end_h), tzinfo=tz)
            while t + datetime.timedelta(minutes=dur) <= end_of_day:
                e = t + datetime.timedelta(minutes=dur)
                if t >= win_start and not overlaps(t, e):
                    slots.append(f"{t.strftime('%a %b %-d, %-I:%M')}–{e.strftime('%-I:%M %p')} {label}")
                    break                                    # one proposal per day → spread across days
                t += datetime.timedelta(minutes=30)
        day += datetime.timedelta(days=1)
    return slots


def _fmt_time(t) -> str:
    """'2 PM' / '2:30 PM' — drop ':00' so windows read naturally in a reply."""
    return t.strftime("%-I %p") if t.minute == 0 else t.strftime("%-I:%M %p")


def free_windows(cfg: dict, days: int | None = None, count: int | None = None) -> list[str]:
    """Broad availability *windows* to offer a recruiter — 'open all day', 'free in the
    morning', 'free after 2 PM' — instead of three narrow 45-min slots. Easier for the other
    side to arrange around. One line per business day over the next `days`; empty if the
    calendar isn't connected."""
    svc = _service(cfg)
    if svc is None:
        return []
    cal = cfg.get("calendar") or {}
    tz = _tz(cfg)
    start_h = int(cal.get("hours_start", 9))
    end_h = int(cal.get("hours_end", 18))
    mid_h = int(cal.get("midday_hour", 13))         # split morning / afternoon at 1 PM
    label = cal.get("tz_label", "PT")
    min_block = int(cal.get("min_window_minutes", 90))
    days = int(cal.get("window_days", 7)) if days is None else days
    count = int(cal.get("window_count", 5)) if count is None else count

    now = datetime.datetime.now(tz)
    win_start = now + datetime.timedelta(hours=int(cal.get("min_notice_hours", 18)))
    win_end = now + datetime.timedelta(days=days)
    try:
        busy_raw = svc.freebusy().query(body={
            "timeMin": win_start.isoformat(), "timeMax": win_end.isoformat(),
            "timeZone": str(tz), "items": [{"id": "primary"}],
        }).execute()["calendars"]["primary"]["busy"]
    except Exception:
        return []
    busy = [(datetime.datetime.fromisoformat(b["start"]).astimezone(tz),
             datetime.datetime.fromisoformat(b["end"]).astimezone(tz)) for b in busy_raw]

    def overlaps(s, e):
        return any(s < be and bs < e for bs, be in busy)

    def largest_free(day_s, day_e):
        segs = sorted((max(bs, day_s), min(be, day_e)) for bs, be in busy if bs < day_e and be > day_s)
        best, cur = None, day_s
        for bs, be in segs:
            if bs > cur and (best is None or (bs - cur) > (best[1] - best[0])):
                best = (cur, bs)
            cur = max(cur, be)
        if cur < day_e and (best is None or (day_e - cur) > (best[1] - best[0])):
            best = (cur, day_e)
        return best

    out: list[str] = []
    day = win_start.date()
    while day <= win_end.date() and len(out) < count:
        if day.weekday() < 5:                       # Mon–Fri only
            day_s = datetime.datetime.combine(day, datetime.time(start_h), tzinfo=tz)
            day_e = datetime.datetime.combine(day, datetime.time(end_h), tzinfo=tz)
            mid = datetime.datetime.combine(day, datetime.time(mid_h), tzinfo=tz)
            day_s = max(day_s, win_start)           # respect min-notice on the first day
            if day_s < day_e:
                pretty = day.strftime("%a %b %-d")
                am_free = day_s <= mid and not overlaps(day_s, mid)
                pm_free = not overlaps(mid, day_e)
                if am_free and pm_free:
                    out.append(f"{pretty}: open all day ({_fmt_time(day_s)}–{_fmt_time(day_e)} {label})")
                elif am_free:
                    out.append(f"{pretty}: free in the morning (before {_fmt_time(mid)} {label})")
                elif pm_free:
                    out.append(f"{pretty}: free in the afternoon (after {_fmt_time(mid)} {label})")
                else:
                    blk = largest_free(day_s, day_e)
                    if blk and (blk[1] - blk[0]).total_seconds() >= min_block * 60:
                        out.append(f"{pretty}: free {_fmt_time(blk[0])}–{_fmt_time(blk[1])} {label}")
        day += datetime.timedelta(days=1)
    return out
