"""Daily idea/project radar — Show HN, GitHub Trending, Product Hunt, HN front page.

Writes ONLY to data/ideas.db (third DB, single writer = this module; the web UI reads
it and triggers fetches through this module's functions only). Same no-LLM/no-keys
rule as the rest of JobScan: public JSON APIs + RSS + one HTML scrape, heuristic
ranking, nothing else.

Usage: python -m app.ideas          # fetch all sources, upsert, print summary
The web UI surfaces the result at /ideas (💡 in the topbar).
"""
from __future__ import annotations

import datetime
import html
import json
import re
import sqlite3
import urllib.request

from .config import load_config, resolve_path
from .store import LA

TIMEOUT = 25
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh) jobscan-ideas/0.1 (personal daily digest)"}
# max items shown/kept per source per day
LIMITS = {"show_hn": 10, "gh": 12, "ph": 10, "hn_front": 12}

SOURCE_LABELS = {  # slug -> (emoji+label, ordering)
    "show_hn": ("🛠 Show HN", 0),
    "gh": ("📈 GitHub Trending", 1),
    "ph": ("🚀 Product Hunt", 2),
    "hn_front": ("📰 HN front page", 3),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  key        TEXT PRIMARY KEY,   -- e.g. hn:41234567 / gh:owner/repo / ph:<url>
  source     TEXT NOT NULL,      -- show_hn | gh | ph | hn_front
  title      TEXT NOT NULL,
  url        TEXT NOT NULL,
  meta       TEXT NOT NULL DEFAULT '',
  score      INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT NOT NULL,      -- LA date, YYYY-MM-DD
  -- streak columns: meta/score/first_seen stay frozen at the first sighting so a day
  -- bucket keeps reading as "what was new that day"; these three accumulate instead.
  days_seen  INTEGER NOT NULL DEFAULT 1,   -- distinct days this key showed up
  last_seen  TEXT NOT NULL DEFAULT '',     -- LA date of the most recent sighting
  peak_score INTEGER NOT NULL DEFAULT 0    -- best score seen across all sightings
);
CREATE INDEX IF NOT EXISTS idx_items_day ON items(first_seen);
CREATE TABLE IF NOT EXISTS fetch_log(
  run_at TEXT NOT NULL,          -- ISO timestamp
  n_new  INTEGER NOT NULL,
  errors TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS digests(
  day        TEXT PRIMARY KEY,   -- LA date, YYYY-MM-DD
  content    TEXT NOT NULL,      -- JSON: {"tldr": str, "groups": [{"label", "items": [{"title","url","why"}]}]}
  created_at TEXT NOT NULL
);
"""


def ideas_db_path(cfg: dict):
    return resolve_path(cfg.get("ideas_db_path", "data/ideas.db"))


def connect_ideas(cfg: dict) -> sqlite3.Connection:
    path = ideas_db_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add the streak columns to a pre-existing items table and backfill them, treating
    every historical row as a single sighting (we never recorded repeats before)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    if "days_seen" in cols:
        return
    conn.execute("ALTER TABLE items ADD COLUMN days_seen INTEGER NOT NULL DEFAULT 1")
    conn.execute("ALTER TABLE items ADD COLUMN last_seen TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE items ADD COLUMN peak_score INTEGER NOT NULL DEFAULT 0")
    conn.execute("UPDATE items SET last_seen = first_seen, peak_score = score")
    conn.commit()


def _today() -> str:
    return datetime.datetime.now(LA).date().isoformat()


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


# ---------------- sources (each returns list of item dicts, best first) ----------------

def src_show_hn():
    """Show HN, last 48h, by points — densest 'ordinary person shipped a thing' feed."""
    since = int((datetime.datetime.now() - datetime.timedelta(hours=48)).timestamp())
    url = ("https://hn.algolia.com/api/v1/search_by_date?tags=show_hn"
           f"&hitsPerPage=60&numericFilters=created_at_i>{since}")
    items = []
    for h in json.loads(_fetch(url)).get("hits", []):
        pts = h.get("points") or 0
        if pts < 5:
            continue
        hn_link = f"https://news.ycombinator.com/item?id={h['objectID']}"
        items.append({
            "key": f"hn:{h['objectID']}", "source": "show_hn",
            "title": _strip_tags(h.get("title") or "(untitled)"),
            "url": h.get("url") or hn_link,
            "meta": f"{pts}pt · {h.get('num_comments') or 0} comments — {hn_link}",
            "score": pts,
        })
    items.sort(key=lambda x: -x["score"])
    return items[: LIMITS["show_hn"]]


def src_hn_front():
    url = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30"
    items = []
    for h in json.loads(_fetch(url)).get("hits", []):
        hn_link = f"https://news.ycombinator.com/item?id={h['objectID']}"
        items.append({
            "key": f"hn:{h['objectID']}", "source": "hn_front",
            "title": _strip_tags(h.get("title") or "(untitled)"),
            "url": h.get("url") or hn_link,
            "meta": f"{h.get('points') or 0}pt · {h.get('num_comments') or 0} comments — {hn_link}",
            "score": h.get("points") or 0,
        })
    items.sort(key=lambda x: -x["score"])
    return items[: LIMITS["hn_front"]]


def src_gh_trending():
    """GitHub Trending (daily) — developers voting with stars. HTML scrape, may break."""
    page = _fetch("https://github.com/trending?since=daily")
    items = []
    for art in re.findall(r'<article class="Box-row"[\s\S]*?</article>', page):
        m = re.search(r'<h2[^>]*>[\s\S]*?href="/([^"]+)"', art)
        if not m:
            continue
        repo = m.group(1).strip()
        desc_m = re.search(r'<p class="col-9[^"]*">([\s\S]*?)</p>', art)
        desc = _strip_tags(desc_m.group(1)) if desc_m else ""
        lang_m = re.search(r'itemprop="programmingLanguage">([^<]+)<', art)
        stars_m = re.search(r"([\d,]+)\s+stars?\s+today", art)
        today = int(stars_m.group(1).replace(",", "")) if stars_m else 0
        items.append({
            "key": f"gh:{repo}", "source": "gh",
            "title": repo + (f" · {lang_m.group(1)}" if lang_m else ""),
            "url": f"https://github.com/{repo}",
            "meta": f"+{today}★ today" + (f" — {desc[:140]}" if desc else ""),
            "score": today,
        })
    items.sort(key=lambda x: -x["score"])
    return items[: LIMITS["gh"]]


def src_product_hunt():
    xml = _fetch("https://www.producthunt.com/feed")
    items = []
    for entry in re.findall(r"<entry>([\s\S]*?)</entry>", xml):
        t = re.search(r"<title[^>]*>([\s\S]*?)</title>", entry)
        link = re.search(r'<link[^>]*href="([^"]+)"', entry)
        if not (t and link):
            continue
        items.append({
            "key": f"ph:{link.group(1)}", "source": "ph",
            "title": _strip_tags(t.group(1)), "url": link.group(1),
            "meta": "", "score": 0,
        })
    return items[: LIMITS["ph"]]


SOURCES = [src_show_hn, src_gh_trending, src_product_hunt, src_hn_front]


# ---------------- fetch + store ----------------

def fetch_all(conn: sqlite3.Connection) -> dict:
    """Pull every source, insert never-seen items stamped with today. An item seen on an
    earlier day is not re-inserted — it bumps days_seen/last_seen/peak_score instead, so a
    repo that keeps trending accumulates a visible streak (the cheap stand-in for a star
    slope: sustained > one-day spike). Idempotent within a day, since a second sighting
    today already has last_seen == today; that also keeps the cross-source dedup — an HN
    story on both front page and Show HN still keeps its first source."""
    today, n_new, n_repeat, errors = _today(), 0, 0, []
    for fn in SOURCES:
        try:
            fetched = fn()
        except Exception as e:  # one dead source must not kill the run
            errors.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            continue
        for it in fetched:
            row = conn.execute("SELECT last_seen FROM items WHERE key = ?", (it["key"],)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO items(key, source, title, url, meta, score, first_seen,"
                    " days_seen, last_seen, peak_score) VALUES(?,?,?,?,?,?,?,1,?,?)",
                    (it["key"], it["source"], it["title"], it["url"], it["meta"], it["score"],
                     today, today, it["score"]))
                n_new += 1
            elif row["last_seen"] != today:
                conn.execute(
                    "UPDATE items SET days_seen = days_seen + 1, last_seen = ?,"
                    " peak_score = MAX(peak_score, ?) WHERE key = ?",
                    (today, it["score"], it["key"]))
                n_repeat += 1
    conn.execute("INSERT INTO fetch_log(run_at, n_new, errors) VALUES(?,?,?)",
                 (datetime.datetime.now(LA).isoformat(timespec="seconds"), n_new, "; ".join(errors)))
    conn.commit()
    return {"n_new": n_new, "n_repeat": n_repeat, "errors": errors}


def recent_days(conn: sqlite3.Connection, days: int = 7) -> list[dict]:
    """[{day, sources: [{slug, label, items:[...]}, ...]}, ...] newest day first."""
    cutoff = (datetime.datetime.now(LA).date() - datetime.timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        "SELECT * FROM items WHERE first_seen >= ? ORDER BY first_seen DESC, score DESC",
        (cutoff,)).fetchall()
    by_day: dict[str, dict[str, list]] = {}
    for r in rows:
        by_day.setdefault(r["first_seen"], {}).setdefault(r["source"], []).append(dict(r))
    out = []
    for day in sorted(by_day, reverse=True):
        srcs = sorted(by_day[day].items(), key=lambda kv: SOURCE_LABELS.get(kv[0], (kv[0], 9))[1])
        out.append({"day": day, "sources": [
            {"slug": s, "label": SOURCE_LABELS.get(s, (s, 9))[0], "items": its} for s, its in srcs]})
    return out


def last_fetch(conn: sqlite3.Connection) -> dict | None:
    r = conn.execute("SELECT * FROM fetch_log ORDER BY run_at DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def save_digest(conn: sqlite3.Connection, day: str, content: str) -> None:
    conn.execute("INSERT INTO digests(day, content, created_at) VALUES(?,?,?) "
                 "ON CONFLICT(day) DO UPDATE SET content=excluded.content,"
                 " created_at=excluded.created_at",
                 (day, content, datetime.datetime.now(LA).isoformat(timespec="seconds")))
    conn.commit()


def latest_digest(conn: sqlite3.Connection) -> dict | None:
    r = conn.execute("SELECT * FROM digests ORDER BY day DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def main():
    cfg = load_config()
    conn = connect_ideas(cfg)
    res = fetch_all(conn)
    print(f"[ideas] {_today()}: {res['n_new']} new items, {res['n_repeat']} still trending"
          f" -> {ideas_db_path(cfg)}")
    for e in res["errors"]:
        print(f"[ideas] source failed: {e}")
    conn.close()


if __name__ == "__main__":
    main()
