"""Google Calendar (read-only) — proposes real interview availability slots for auto-replies.
Headless via a stored OAuth token (secrets/token_gcal.json). Everything degrades to None/empty
until the user runs `python tools/gcal_auth.py` once. Never writes to the calendar.
"""
from __future__ import annotations

import datetime
import zoneinfo

from . import config as cfgmod

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_PATH = "secrets/token_gcal.json"      # gitignored — created by tools/gcal_auth.py
CLIENT_PATH = "secrets/gcal_client.json"    # gitignored — the OAuth client the user downloads


def _service(cfg: dict):
    """Build a Calendar API client from the stored token, or None if unavailable."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        return None
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
            while t + datetime.timedelta(minutes=dur) <= end_of_day and len(slots) < count:
                e = t + datetime.timedelta(minutes=dur)
                if t >= win_start and not overlaps(t, e):
                    s1 = t.strftime("%a %b %-d, %-I:%M")
                    s2 = e.strftime("%-I:%M %p")
                    slots.append(f"{s1}–{s2} {label}")
                    t = e + datetime.timedelta(minutes=30)   # spread proposals out
                else:
                    t += datetime.timedelta(minutes=30)
        day += datetime.timedelta(days=1)
    return slots
