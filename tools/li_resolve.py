#!/usr/bin/env python3
"""li_resolve.py — link LinkedIn shadow rows to the real posting on the company's own board.

Two passes, both owner-approved 2026-09-15 (direction, hide-behind-duplicate_of, and the
high/low-confidence split were each explicitly signed off):

  A. dedupe   — li: rows whose company is ALREADY scanned directly: link each to its ATS twin.
                Exact tier reuses store.reconcile_duplicates() (space-stripped title_norm
                equality, plus status mirroring / tailor-pointer copying). This file adds ONE
                fuzzy tier on top for the rows exact equality misses ("Software Engineer -
                Backend" vs "Software Engineer, Backend Infrastructure").
  B. resolve  — li: rows from companies we do NOT scan: guess the company's public board
                (probe_ats.slugs + the app/sources fetchers), match the posting by title, and
                swap the row's apply_url from the LinkedIn view page to the board's direct
                link — so the browser extension recognises the page and one-click fill works
                without the LinkedIn hop. Never touches LinkedIn (guest or otherwise).

Fuzzy matching is deliberately narrower than "looks similar" — find_twin()'s docstring
explains why containment matching is dangerous (a wrong link cascades application status
onto a job never applied to). Every fuzzy link therefore requires ALL of:
  * seniority signature identical (senior/staff/lead/II/III… — "Software Engineer" never
    links to "Senior Software Engineer"),
  * same discipline word-set, and the LinkedIn core tokens a SUBSET of the board title's
    (the board may extend the name, extra LinkedIn specifics never collapse onto a barer one),
  * compatible locations (remote/blank match anything, else a shared city/state token),
  * exactly ONE candidate passing (two candidates = ambiguous = report, not link),
  * both rows still at status new/skip (a row with a real-world status is a human statement
    — those only ever link through the exact tier that reconcile_duplicates already runs).
Anything that fails only the uniqueness or threshold check lands in the report as a
low-confidence proposal for the owner to eyeball — never auto-linked.

DB discipline: role_state writes (duplicate_of, tailor pointers) go through the view
connection like every UI-side writer; the ONE jobs-db write (roles.apply_url swap in pass B)
uses its own connect_jobs() connection — the attached jobs db on a view connection stays
read-only by convention (store.py docstring).

    jobscan/.venv/bin/python jobscan/tools/li_resolve.py [--limit-companies 40] [--delay 0.4]
        [--recheck-days 30] [--dedupe-only | --resolve-only] [--dry-run] [--out PATH]

State: data/li_resolve_state.json remembers which orphan companies were probed and what
board (if any) was found, so the nightly chips away at the 1200+ orphan companies instead
of re-probing the same dead ends every morning.
"""
from __future__ import annotations

import argparse
import datetime
import json
import random
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tools"))

import httpx  # noqa: E402

from app import config as cfgmod, store  # noqa: E402
from app.sources import ashby, greenhouse, lever  # noqa: E402
from app.sources.base import make_client  # noqa: E402
from probe_ats import slugs  # noqa: E402  — pure function, import-safe (main() is guarded)

STATE = "data/li_resolve_state.json"
FRESH_STATUSES = (None, "", "new", "skip")   # rows fuzzy linking is allowed to touch

# ---------------------------------------------------------------- title matcher (pure)

# Tokens that change WHICH job it is, not how it's worded. Roman/arabic levels normalise to
# the same signature ("Engineer II" vs "Engineer 2" is the same level; II vs III is not).
# An arabic digit only counts as a LEVEL when it ends the title or follows "level"/"l" —
# review caught "Payments 2.0" reading as level 2 and matching "Engineer II".
_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5"}
_DIGIT = {"1", "2", "3", "4", "5"}
# Synonyms collapse so wording can't split a real match ("Sr."/"Senior", "Jr."/"Junior").
_SENIOR_MAP = {"senior": "sr", "sr": "sr", "junior": "jr", "jr": "jr",
               "intern": "intern", "internship": "intern",
               "graduate": "grad", "grad": "grad"}
_SENIOR = {"staff", "principal", "lead", "entry", "associate", "manager", "director",
           "head", "vp", "distinguished", "fellow"} | set(_SENIOR_MAP)
_FILLER = {"a", "an", "the", "of", "and", "or", "for", "in", "on", "at", "to", "with"}
# Discipline words name WHAT KIND of engineer — the two titles must carry the SAME set, or
# it's a different profession no matter how much else they share ("Backend Engineer" vs
# "DevOps Engineer Backend", "Network Reliability Engineer" vs "Software Engineer, Network
# Performance & Reliability" — real mispairs from the first dry run). "software" IS in the
# set (SWE vs non-SWE is itself a discipline line); area words like "infrastructure" or
# "platform" are NOT — they specialise a discipline, they don't change it.
_DISCIPLINE = {"software", "backend", "frontend", "fullstack", "mobile", "ios", "android",
               "web", "devops", "sre", "security", "network", "networking", "controls",
               "hardware", "firmware", "embedded", "qa", "test", "data", "ml"}
# Spelled-out forms of the same disciplines, detected on the squashed title — review showed
# "Machine Learning" / "Site Reliability" / "Quality Assurance" walked straight past the
# token set that stops "ML"/"SRE"/"QA".
_DISCIPLINE_PHRASES = {"machinelearning": "ml", "sitereliability": "sre",
                       "qualityassurance": "qa", "fullstack": "fullstack",
                       "frontend": "frontend", "backend": "backend"}


def discipline_sig(title: str) -> frozenset[str]:
    sq = _squash(title)
    out = {t for t in _tokens(title) if t in _DISCIPLINE}
    for phrase, canon in _DISCIPLINE_PHRASES.items():
        if phrase in sq:
            out.add(canon)
    return frozenset(out)


def _tokens(title: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (title or "").lower())


def seniority_sig(title: str) -> frozenset[str]:
    toks = _tokens(title)
    out = set()
    for idx, t in enumerate(toks):
        if t in _SENIOR:
            out.add(_SENIOR_MAP.get(t, t))
        elif t in _ROMAN:
            out.add(_ROMAN[t])
        elif t in _DIGIT and (idx == len(toks) - 1 or toks[idx - 1] in ("level", "l")):
            out.add(t)
    return frozenset(out)


def core_tokens(title: str) -> set[str]:
    toks = _tokens(title)
    out = set()
    for idx, t in enumerate(toks):
        if t in _FILLER or t in _SENIOR or t in _ROMAN:
            continue
        if t in _DIGIT and (idx == len(toks) - 1 or toks[idx - 1] in ("level", "l")):
            continue                        # counted as a level, not as content
        if t in ("level", "l") and idx + 1 < len(toks) and toks[idx + 1] in _DIGIT:
            continue                        # the "level" word itself belongs to the level
        out.add(t)
    return out


def _squash(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


def _city(loc: str) -> str:
    """First comma-segment, squashed — the city-ish part of a location string."""
    return re.sub(r"[^a-z0-9]+", "", (loc or "").lower().split(",")[0])


def loc_compatible(a: str, b: str, strict: bool = False) -> bool:
    """Lenient (exact-title tier): remote/blank on either side matches anything.
    Strict (fuzzy tier): remote only matches remote-or-blank, otherwise the CITY segments
    must agree — token overlap let San Francisco≈San Jose, New York≈Newark and
    'Remote - US'≈'Bengaluru, India' through (all real review repros)."""
    a, b = (a or "").lower(), (b or "").lower()
    if not a or not b:
        return True
    ra, rb = "remote" in a, "remote" in b
    if not strict and (ra or rb):
        return True
    if ra or rb:
        return ra and rb
    ca, cb = _city(a), _city(b)
    if not ca or not cb:
        return True
    return ca == cb or ca.startswith(cb) or cb.startswith(ca)


def title_match(li_title: str, li_loc: str,
                cands: list[tuple[str, str, str]]) -> tuple[str | None, list[str]]:
    """cands = [(key, title, location)] → ("high", [key]) | ("ambiguous", keys) | (None, []).

    Exact tier first (squashed-title equality, mirroring find_twin's rule), then the guarded
    fuzzy tier documented in the module docstring. Ambiguity at either tier is reported, not
    guessed at.
    """
    # LinkedIn titles sometimes carry junk after a newline ("Software Engineer\nSan
    # Francisco" — real row); only the first line is the title.
    li_title = (li_title or "").splitlines()[0] if li_title else ""
    cands = [(k, (t or "").splitlines()[0] if t else "", loc) for k, t, loc in cands]

    sq = _squash(li_title)
    exact = [k for k, t, loc in cands if _squash(t) == sq and loc_compatible(li_loc, loc)]
    if len(exact) == 1:
        return "high", exact
    if len(exact) > 1:
        return "ambiguous", exact

    sig, core, disc = seniority_sig(li_title), core_tokens(li_title), discipline_sig(li_title)
    fuzzy = []
    for k, t, loc in cands:
        if seniority_sig(t) != sig or not loc_compatible(li_loc, loc, strict=True):
            continue
        other = core_tokens(t)
        # Three guards, each earned by a real mispair on the first dry runs:
        # 1. Same discipline set — "Backend Engineer" never links to "DevOps Engineer
        #    Backend"; a swapped profession is a different job.
        # 2. Asymmetric subset: the BOARD title may extend the LinkedIn title ("Backend" ⊂
        #    "Backend Infrastructure" — boards carry the fuller req name), but a LinkedIn
        #    title with EXTRA specifics must not collapse onto a barer board title —
        #    "SWE III, BigQuery" is not the same req as a plain "SWE III" (Google posts
        #    dozens of specialised SWE III reqs; the bare row is its own req).
        # 3. At least 2 shared core tokens, so one generic word can't carry a link.
        if discipline_sig(t) != disc:
            continue
        if len(core) >= 2 and core <= other:
            fuzzy.append(k)
    if len(fuzzy) == 1:
        return "high", fuzzy
    if len(fuzzy) > 1:
        return "ambiguous", fuzzy
    return None, []


# ---------------------------------------------------------------- state file (repo idiom)

def load_state(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}                       # a lost/corrupt state file just means re-probing


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)                   # atomic — a killed run never leaves half a file


# ---------------------------------------------------------------- pass A: dedupe

def dedupe(view, *, dry_run: bool = False) -> dict:
    """Exact tier via store.reconcile_duplicates, then the guarded fuzzy tier."""
    out = {"exact": {}, "fuzzy": [], "ambiguous": []}
    if not dry_run:
        out["exact"] = store.reconcile_duplicates(view)

    rows = [dict(r) for r in view.execute(
        "SELECT r.job_id, r.company_norm, r.company, r.title, r.location,"
        "       s.status AS own_status, s.duplicate_of,"
        "       s.tailored_resume_path, s.tailor_status, s.prompt_path"
        " FROM jobs.roles r LEFT JOIN role_state s ON s.job_id = r.job_id")]
    li = [r for r in rows if r["job_id"].startswith("li:")]
    by_company: dict[str, list[dict]] = {}
    for r in rows:
        if not r["job_id"].startswith("li:"):
            by_company.setdefault(r["company_norm"] or "", []).append(r)

    for r in li:
        if r["duplicate_of"] or (r["own_status"] or "new") not in ("new", "skip"):
            continue
        # Uniqueness is judged on the FULL candidate set — find_twin's rule. Filtering to
        # fresh rows first would let "one applied + one new copy of the same title" look
        # unambiguous and link (review M1); the status guard below only gates the WRITE.
        cands = by_company.get(r["company_norm"] or "", [])
        if not cands:
            continue
        verdict, keys = title_match(r["title"], r["location"],
                                    [(c["job_id"], c["title"], c["location"]) for c in cands])
        if verdict == "high" and (next(c for c in cands if c["job_id"] == keys[0])
                                  .get("own_status") or "new") not in ("new", "skip"):
            continue                       # twin carries a real-world status — hands off
        if verdict == "high":
            twin = next(c for c in cands if c["job_id"] == keys[0])
            out["fuzzy"].append((r["job_id"], r["title"], twin["job_id"], twin["title"],
                                 r["company"]))
            if not dry_run:
                store.upsert_state(view, r["job_id"], duplicate_of=twin["job_id"])
                # Mirror reconcile_duplicates' tailor-pointer copy for this pair: the
                # extension resolves the ATS row on the employer's page and needs the PDF
                # pointer there; the UI list often shows the li: row.
                for src, dst in ((r, twin), (twin, r)):
                    if src.get("tailored_resume_path") and not dst.get("tailored_resume_path"):
                        store.upsert_state(view, dst["job_id"],
                                           tailored_resume_path=src["tailored_resume_path"],
                                           prompt_path=src.get("prompt_path"),
                                           tailor_status=src.get("tailor_status") or "ready")
        elif verdict == "ambiguous":
            out["ambiguous"].append((r["job_id"], r["title"], r["company"], keys))
    return out


# ---------------------------------------------------------------- pass B: orphan resolve

_FETCHERS = {"greenhouse": greenhouse.fetch, "lever": lever.fetch, "ashby": ashby.fetch}
_SUFFIXES = re.compile(r"\b(inc|llc|labs|technologies|technology|corp|corporation|co|ai|io)\b\.?$",
                       re.I)


def strong_slugs(display: str) -> set[str]:
    """The guesses that ARE the company name (squashed / hyphenated, with and without a
    corporate suffix) — safe to trust on a bare 200. Everything else slugs() invents
    (go/get/try prefixes, aggressive stem-stripping) is a WEAK guess: 'Thrive' hits Thrive
    Market's board whether you meant Thrive Global or Thrive Capital, and probe_ats's own
    length floor still lets 'open' probe OpenAI (review H2). Weak hits are reported for the
    owner to confirm, never written."""
    base = re.sub(r"[(（|│].*$", "", display or "").strip()
    variants = {base, _SUFFIXES.sub("", base).strip()}
    out = set()
    for v in variants:
        if not v:
            continue
        out.add(re.sub(r"[^a-z0-9]+", "", v.lower()))
        out.add(re.sub(r"[^a-z0-9]+", "-", v.lower()).strip("-"))
    return {s for s in out if len(s) >= 4}


def swap_url(ats: str, posting) -> str:
    """The URL written onto the li: row. For greenhouse this must be the PUBLIC posting
    page (path-delimited id): the scanner's embed form carries the id only as `token=`,
    which the extension's id-channel refuses (store.lookup_role_by_ids needs `/<id>` for
    short ids), and every embed URL canonicalises to the SAME string — on a li: row that
    combination made the extension resolve a DIFFERENT company's role and offer its résumé
    (review H1, reproduced live). Lever/Ashby URLs carry a uuid and are unique as-is."""
    if ats == "greenhouse":
        return f"https://job-boards.greenhouse.io/{posting.slug}/jobs/{posting.raw_id}"
    return posting.apply_url


def find_board(client: httpx.Client, display: str, delay: float):
    """Try slug guesses across the three public board APIs; first hit wins.

    Returns (ats, slug, postings, definitive, trusted): `definitive` is False when every
    miss was a transport failure (offline ≠ no board — review M4); `trusted` is False when
    the hit came from a weak guess (report-only, see strong_slugs)."""
    strong = strong_slugs(display)
    definitive = False
    for slug in slugs(display):
        for ats, fetch in _FETCHERS.items():
            time.sleep(delay + random.uniform(0.0, 0.3))
            try:
                postings = fetch(client, display, slug, retries=0)
                definitive = True             # got an HTTP answer (2xx here, 404 raises…)
            except httpx.TransportError:
                continue                      # network-level — proves nothing
            except Exception:
                definitive = True             # 404/robots/parse — a real "not this board"
                continue
            if postings:
                return ats, slug, postings, True, slug in strong
    return None, None, [], definitive, False


def resolve_orphans(view, jobs_conn, client, *, limit_companies: int, delay: float,
                    recheck_days: int, state: dict, dry_run: bool = False) -> dict:
    out = {"probed": 0, "boards_found": 0, "swapped": [], "ambiguous": [],
           "no_board": 0, "unconfirmed": [], "network_skipped": 0}
    today = datetime.date.today()
    cutoff = (today - datetime.timedelta(days=recheck_days)).isoformat()
    companies = state.setdefault("companies", {})

    rows = [dict(r) for r in view.execute(
        "SELECT r.job_id, r.company_norm, r.company, r.title, r.location, r.apply_url,"
        "       r.weighted_score, r.verdict, r.first_seen, s.status AS own_status"
        " FROM jobs.roles r LEFT JOIN role_state s ON s.job_id = r.job_id"
        " WHERE r.job_id LIKE 'li:%'")]
    direct = {r["company_norm"] for r in view.execute(
        "SELECT DISTINCT company_norm FROM jobs.roles WHERE job_id NOT LIKE 'li:%'")}

    orphans: dict[str, list[dict]] = {}
    for r in rows:
        cn = r["company_norm"] or ""
        if cn and cn not in direct and "linkedin.com" in (r["apply_url"] or "") \
                and (r["own_status"] or "new") in ("new", "skip") \
                and (r["verdict"] or "") != "drop":
            orphans.setdefault(cn, []).append(r)

    ranked = sorted(orphans.items(),
                    key=lambda kv: max(x["weighted_score"] or 0 for x in kv[1]), reverse=True)
    pending_swaps: list[tuple[str, str]] = []       # (url, job_id) — written AFTER probing
    transport_streak = 0
    for cn, group in ranked:
        if out["probed"] >= limit_companies:
            break
        seen = companies.get(cn) or {}
        fresh_rows = any((r["first_seen"] or "") > (seen.get("checked") or "") for r in group)
        # Rest rule (review M3): anything probed inside the window that produced no swap
        # last time — board or no board — rests, UNLESS new li rows arrived since. Without
        # this, boards whose titles never match camp permanently in the nightly quota.
        if (seen.get("checked") or "") >= cutoff and not seen.get("last_swaps") \
                and not fresh_rows:
            continue
        display = group[0]["company"]
        out["probed"] += 1
        trusted = True
        if seen.get("board"):                 # known trusted board — one cheap re-fetch
            ats, slug = seen["board"].split(":", 1)
            try:
                postings = _FETCHERS[ats](client, display, slug, retries=0)
                definitive = True
            except httpx.TransportError:
                postings, definitive = [], False
            except Exception:
                postings, definitive = [], True
        else:
            ats, slug, postings, definitive, trusted = find_board(client, display, delay)
        if not postings:
            if not definitive:
                # Offline/unreachable is not "no board" — leave state untouched so the
                # company is re-tried, and bail out entirely if the network looks dead.
                out["network_skipped"] += 1
                transport_streak += 1
                if transport_streak >= 3:
                    print("!! network looks down — aborting resolve pass, state untouched")
                    break
                continue
            transport_streak = 0
            companies[cn] = {"checked": today.isoformat(), "board": seen.get("board"),
                             "display": display, "last_swaps": 0}
            out["no_board"] += 1
            continue
        transport_streak = 0
        cands = [(swap_url(ats, p), p.title, p.location) for p in postings if p.apply_url]
        if not trusted:
            # Weak slug guess (review H2): a board answered, but nothing proves it belongs
            # to THIS company. Report the would-be swaps; never write them.
            hits = []
            for r in group:
                verdict, keys = title_match(r["title"], r["location"], cands)
                if verdict == "high":
                    hits.append((r["job_id"], r["title"], keys[0]))
            out["unconfirmed"].append((display, f"{ats}:{slug}", hits))
            companies[cn] = {"checked": today.isoformat(), "board": None,
                             "board_unconfirmed": f"{ats}:{slug}",
                             "display": display, "last_swaps": 0}
            continue
        out["boards_found"] += 1
        n_swaps = 0
        for r in group:
            verdict, keys = title_match(r["title"], r["location"], cands)
            if verdict == "high":
                url = keys[0]
                out["swapped"].append((r["job_id"], r["title"], display, url))
                pending_swaps.append((url, r["job_id"]))
                n_swaps += 1
            elif verdict == "ambiguous":
                titles = [t for u, t, _l in cands if u in keys]
                out["ambiguous"].append((r["job_id"], r["title"], display, titles))
        companies[cn] = {"checked": today.isoformat(), "board": f"{ats}:{slug}",
                         "display": display, "last_swaps": n_swaps}
    # One short write transaction AFTER all network work — a transaction held open across
    # the probing loop kept jobs.db locked for the whole run and starved the 09:00 scanner
    # for its full busy_timeout (review H3, reproduced live).
    if not dry_run and pending_swaps:
        now = store.la_now_iso()
        for url, job_id in pending_swaps:
            jobs_conn.execute("UPDATE roles SET apply_url=?, updated_at=? WHERE job_id=?",
                              (url, now, job_id))
        jobs_conn.commit()
    return out


# ---------------------------------------------------------------- report

def write_report(path: Path, a: dict | None, b: dict | None) -> None:
    lines = [f"# li_resolve — {datetime.date.today().isoformat()}", ""]
    if a is not None:
        ex = a.get("exact") or {}
        lines += ["## A · 去重（li ↔ 已扫 ATS 行）",
                  f"- exact tier (reconcile_duplicates): linked {ex.get('linked', 0)}, "
                  f"status mirrored {ex.get('status_mirrored', 0)}, "
                  f"tailor copied {ex.get('tailor_copied', 0)}",
                  f"- fuzzy tier: linked {len(a['fuzzy'])}", ""]
        for li_id, li_t, tw_id, tw_t, comp in a["fuzzy"]:
            lines.append(f"  - {comp}: `{li_id}` “{li_t}” → `{tw_id}` “{tw_t}”")
        if a["ambiguous"]:
            lines += ["", "### 低置信（多候选，请过目 — 未动库）", ""]
            for li_id, li_t, comp, keys in a["ambiguous"]:
                lines.append(f"  - {comp}: `{li_id}` “{li_t}” ~ {len(keys)} candidates: "
                             + ", ".join(f"`{k}`" for k in keys))
    if b is not None:
        lines += ["", "## B · 孤儿公司解链（换 apply_url 直链）",
                  f"- companies probed {b['probed']}, boards found {b['boards_found']}, "
                  f"no board {b['no_board']}, links swapped {len(b['swapped'])}, "
                  f"network-skipped {b.get('network_skipped', 0)}", ""]
        for jid, title, comp, url in b["swapped"]:
            lines.append(f"  - {comp}: `{jid}` “{title}” → {url}")
        if b["ambiguous"]:
            lines += ["", "### 低置信（多候选，请过目 — 未动库）", ""]
            for jid, title, comp, titles in b["ambiguous"]:
                lines.append(f"  - {comp}: `{jid}` “{title}” ~ " + " | ".join(titles[:4]))
        if b.get("unconfirmed"):
            lines += ["", "### 板子归属存疑（slug 是弱猜测，请确认 — 未动库）",
                      "确认属于该公司后，把 state 里的 board_unconfirmed 改成 board 即可启用。", ""]
            for comp, board, hits in b["unconfirmed"]:
                lines.append(f"  - {comp} → `{board}`" + (f"，可换 {len(hits)} 条:" if hits else "（无标题命中）"))
                for jid, title, url in hits:
                    lines.append(f"      - `{jid}` “{title}” → {url}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit-companies", type=int, default=40)
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--recheck-days", type=int, default=30)
    ap.add_argument("--dedupe-only", action="store_true")
    ap.add_argument("--resolve-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=f"output/li_resolve_{datetime.date.today().isoformat()}.md")
    args = ap.parse_args()
    if args.dedupe_only and args.resolve_only:
        ap.error("--dedupe-only and --resolve-only are mutually exclusive")

    cfg = cfgmod.load_config()
    view = store.connect_view(cfg)
    a = b = None
    if not args.resolve_only:
        a = dedupe(view, dry_run=args.dry_run)
        if args.dry_run:
            print("note: --dry-run skips the exact tier (reconcile_duplicates writes); "
                  "its counts show 0 and the fuzzy list includes pairs the exact tier "
                  "would claim first")
    if not args.dedupe_only:
        jobs_conn = store.connect_jobs(cfg)
        state_path = cfgmod.resolve_path(STATE)
        state = load_state(state_path)
        client = make_client((cfg.get("http") or {}))
        b = resolve_orphans(view, jobs_conn, client,
                            limit_companies=args.limit_companies, delay=args.delay,
                            recheck_days=args.recheck_days, state=state,
                            dry_run=args.dry_run)
        if not args.dry_run:
            save_state(state_path, state)

    out_path = cfgmod.resolve_path(args.out)
    write_report(out_path, a, b)
    bits = []
    if a is not None:
        ex = a.get("exact") or {}
        bits.append(f"dedupe: exact+{ex.get('linked', 0)} fuzzy+{len(a['fuzzy'])} "
                    f"ambiguous {len(a['ambiguous'])}")
    if b is not None:
        bits.append(f"resolve: probed {b['probed']} boards {b['boards_found']} "
                    f"swapped {len(b['swapped'])} ambiguous {len(b['ambiguous'])}")
    print(("[dry-run] " if args.dry_run else "") + "; ".join(bits) + f" — report: {out_path}")


if __name__ == "__main__":
    main()
