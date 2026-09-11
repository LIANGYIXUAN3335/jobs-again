"""Index the 面试复盘 markdown into the pipeline — the merge point between the interview-review
skill and JobScan.

The review files are already structured: the interview-review skill appends one
`## Company · Date · Round` section per interview to three card-source files, each tagged with
an `<!-- atoms-source id="..." -->` marker for the atoms card cron. This module reuses that
EXACT convention — zero new markers, zero risk to the cron — and mirrors the sections into
`interviews` / `interview_items` so the pipeline can show, per role: which rounds happened,
what was asked, and which action items are still open.

Boundaries, deliberately:
  - The markdown stays the system of record. This is read-only on every file; re-ingest is
    idempotent (items replaced per marker), and a job link set by hand always survives.
  - Matching is company-name based against applied/interviewing/rejected roles: exactly one
    candidate → auto-link (and the role advances to stage=interview — a 复盘 existing IS the
    evidence an interview happened); several → left unlinked for the UI to resolve. Most
    reviews are startups outside the scanned 99 — those stay standalone, which is fine: the
    questions and action items still surface in one place.

    python -m app.interview_ingest            # scan, ingest, print the table
    python -m app.interview_ingest --dry-run  # parse + match, write nothing

  Exit 3 = it ran, but at least one 复盘 section was NOT indexed (bad heading, or no items in a
  shape the parser reads). Silence was the actual bug here — see _sections. — 2026-08-10
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import config as cfgmod
from . import store

# The heading grammar, as she ACTUALLY writes it. Scanned every md under 面试复盘/ on
# 2026-08-10: 55 「##」 headings, three shapes, all with the company first —
#   公司 · 日期 · 轮次        ← the README contract, 53 of 55
#   公司 · 日期 (轮次)        ← 「## Amazon · 2026-06-23 (Technical Interview: …)」 round glued
#                               onto the date with a paren instead of a ·
#   公司 · 轮次 · 日期        ← 「## Anytime AI（疑似）· System Design Round … · 2026-06」
# The old regex only knew shape 1, so the other two sections were dropped whole — 14 action
# items that never reached the DB and nobody noticed for six weeks. Fix: split on ·, field 0
# is the company, whichever LATER field starts with a date is the date, everything else is the
# round. Anchoring on the date instead of on position turns three regexes into one rule and
# absorbs the next variant she invents. — 2026-08-10
_DATE_AT = re.compile(r"^(?P<date>20\d{2}(?:-\d{2}){0,2})(?![\d])")
# Tolerant, not credulous: a date field may carry the round with it, but only when it was
# GLUED ON with punctuation — 「2026-06-23 (Technical Interview: …)」. Without this, 「2026 06 23」
# would read as the year 2026 with a round of "06 23", i.e. a wrong parse where the old regex
# at least gave an honest refusal. Silent wrongness is worse than a loud skip. — 2026-08-10
_ROUND_GLUE = re.compile(r"^\s*[(\[（【—–:：,，-]")
# A 「##」 heading carrying BOTH a · and a year was MEANT to be an interview section. That pair
# is the entire false-positive filter for the warning below: on the real tree it clears every
# 「第一部分 · Coding」/「附录 · 高频术语」/「二、教练复盘（… 2026-07-20 整理）」 heading (16 of
# them) while still flagging the two malformed interview headings. Verified 2026-08-10.
_YEAR = re.compile(r"20\d{2}")
_EDGE = re.compile(r"^[\s\-—–:,;.、]+|[\s\-—–:,;.、]+$")
_WRAPS = (("(", ")"), ("（", "）"), ("[", "]"), ("【", "】"))

_MARKER = re.compile(r'<!--\s*atoms-source\s+id="(?P<id>[^"]+)"')
_CHECKBOX = re.compile(r"^\s*-\s*\[(?P<done>[ xX])\]\s*(?P<text>.+?)\s*$")
_QUESTION = re.compile(r"^###\s*Q:\s*(?P<text>.+?)\s*$")


def _tidy_round(s: str) -> str:
    """Un-glue a round from the date it was written against: 「2026-06-23 (Technical Interview:
    Coding + Design + BQ)」 → 「Technical Interview: Coding + Design + BQ」. Punctuation only —
    her wording is never rewritten, the notes are the system of record."""
    s = _EDGE.sub("", s.strip())
    for op, cl in _WRAPS:
        if s.startswith(op) and s.endswith(cl) and s.count(op) == 1:
            return _EDGE.sub("", s[1:-1].strip())
    return s


def parse_heading(line: str) -> tuple[str, str, str] | None:
    """(company, date, round) for an interview 「##」 heading; None when it isn't one."""
    if not line.startswith("## "):
        return None
    fields = [f.strip() for f in line[3:].strip().split("·")]
    if len(fields) < 2 or not fields[0]:
        return None
    for i in range(1, len(fields)):
        m = _DATE_AT.match(fields[i])
        if not m:
            continue
        tail = fields[i][m.end():]
        if tail.strip() and not _ROUND_GLUE.match(tail):
            continue
        rest = [_tidy_round(tail)]
        rest += [f for j, f in enumerate(fields) if j and j != i]
        return fields[0], m["date"], " · ".join(f for f in rest if f)
    return None


def _looks_like_interview(line: str) -> bool:
    return "·" in line and bool(_YEAR.search(line))


def _derived_id(company: str, date: str) -> str:
    kebab = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    return f"{date}-{kebab}"


def _sections(text: str, src_file: str, kind: str, skipped: list | None = None) -> list[dict]:
    """Split one card-source file into per-interview sections with their items.

    `skipped` is the anti-silence channel: every heading that reads like an interview but
    couldn't be parsed, and every section that parsed but yielded nothing, lands there so
    main() can shout about it. A parser that drops input without complaining is the reason
    28 action items sat outside the DB for six weeks. — 2026-08-10
    """
    out: list[dict] = []
    cur: dict | None = None
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            cur = None                                   # any ## ends the previous section
            h = parse_heading(line)
            if h:
                cur = {"company": h[0], "date": h[1], "round": h[2],
                       "heading": line.strip()[3:], "line": lineno,
                       "marker": None, "items": [], "src_file": src_file, "kind": kind}
                out.append(cur)
            elif skipped is not None and _looks_like_interview(line):
                skipped.append({"file": src_file, "line": lineno, "heading": line.strip(),
                                "why": "标题里有「·」和年份，但读不出 公司 · 日期 —— 整节被跳过"})
            continue
        if cur is None:
            continue
        m = _MARKER.search(line)
        if m:
            cur["marker"] = m["id"].strip()
            continue
        if kind == "action":
            c = _CHECKBOX.match(line)
            if c:
                cur["items"].append({"kind": "action", "text": c["text"],
                                     "done": c["done"].lower() == "x", "src_file": src_file})
        else:
            q = _QUESTION.match(line)
            if q:
                cur["items"].append({"kind": kind, "text": q["text"],
                                     "done": 0, "src_file": src_file})
    want = "- [ ] checkbox" if kind == "action" else "### Q:"
    for s in out:
        s["marker"] = s["marker"] or _derived_id(s["company"], s["date"])
        # The other silent drop, and the one that hid the Lucid/CipherData 行动清单 tables:
        # the heading is fine, the content is there, but it's in a shape this parser can't
        # read (a table, a plain bullet). Zero items out of a real 复盘 section is never right.
        if skipped is not None and not s["items"]:
            skipped.append({"file": src_file, "line": s["line"], "heading": s["heading"],
                            "why": f"标题解析成功，但一条 {kind} 都没读到 —— 内容不是 `{want}` 格式？"})
    return out


def _files(cfg: dict) -> list[tuple[Path, str]]:
    """(path, item-kind) for each card-source file that exists."""
    iv = cfg.get("interviews") or {}
    base = Path(iv.get("dir") or (cfgmod.ROOT.parent / "面试复盘"))
    names = iv.get("files") or {
        "action": "Interview Action Items.md",
        "tech_q": "Technical_Interview_Questions.md",
        "ai_q": "AI_Agent_Interview_Questions.md",
    }
    return [(base / fname, kind) for kind, fname in names.items() if (base / fname).exists()]


# README.md is her hand-maintained master index: one line per interview with the local
# review/transcript md (the Otter transcript's processed form) and, when one exists, the
# Otter share link. "- [31. Amazon - Technical Phone Screen](Amazon/02_....md) (140 transcript
# rows) - [Otter](https://otter.ai/u/...)" under a "### Company" heading.
_README_CO = re.compile(r"^###\s+(.+?)\s*$")
_README_ENTRY = re.compile(r"^\s*-\s*\[(?P<label>[^\]]+)\]\(<?(?P<path>[^)>]+\.md)>?\)(?P<rest>.*)$")
_README_OTTER = re.compile(r"\[Otter\]\((?P<url>https?://[^)]+)\)")
_TOKEN = re.compile(r"[a-z0-9]{1,}")


def _readme_entries(base: Path) -> dict[str, list[dict]]:
    """{company_norm: [{label, path, otter}]} from 面试复盘/README.md (missing file → {})."""
    p = base / "README.md"
    if not p.exists():
        return {}
    out: dict[str, list[dict]] = {}
    company = None
    for line in p.read_text(encoding="utf-8").splitlines():
        h = _README_CO.match(line)
        if h:
            company = cfgmod.norm_key(h.group(1))
            continue
        e = _README_ENTRY.match(line)
        if e and company:
            m = _README_OTTER.search(e["rest"] or "")
            out.setdefault(company, []).append({
                "label": e["label"], "path": e["path"].strip(),
                "otter": m["url"] if m else None})
    return out


def _best_entry(entries: list[dict], round_type: str) -> dict | None:
    """The README line for THIS round: highest token overlap between the interview's round
    label and the entry label; needs ≥2 shared tokens (so "Round 1" matches "Round 1 — BQ +
    OOD Checkout Price Engine" but a lone shared word like "interview" links nothing)."""
    want = set(_TOKEN.findall((round_type or "").lower()))
    best, score = None, 1
    for e in entries:
        have = set(_TOKEN.findall(e["label"].lower()))
        s = len(want & have)
        if s > score:
            best, score = e, s
    return best


def _match_role(conn, company: str) -> tuple[str | None, int]:
    """(job_id, n_candidates). Auto-link only when the company resolves to EXACTLY one
    applied/interviewing/rejected role — the same propose-don't-guess rule as outcomes."""
    cn = cfgmod.norm_key(company)
    if not cn:
        return None, 0
    rows = conn.execute(
        "SELECT r.job_id FROM jobs.roles r JOIN role_state s ON s.job_id=r.job_id"
        " WHERE s.status IN ('applied','interviewing','rejected')"
        "   AND (r.company_norm = ? OR r.company_norm LIKE ? || '%')",
        (cn, cn)).fetchall()
    ids = [r["job_id"] for r in rows]
    return (ids[0], 1) if len(ids) == 1 else (None, len(ids))


def ingest(cfg: dict, conn, *, dry_run: bool = False) -> dict:
    merged: dict[str, dict] = {}
    skipped: list[dict] = []
    for path, kind in _files(cfg):
        text = path.read_text(encoding="utf-8")
        for s in _sections(text, path.name, kind, skipped):
            g = merged.setdefault(s["marker"], {
                "company": s["company"], "date": s["date"], "round": s["round"],
                "sources": {}, "items": []})
            g["sources"][path.name] = s["heading"]
            g["round"] = g["round"] or s["round"]
            g["items"].extend(s["items"])

    iv = cfg.get("interviews") or {}
    base = Path(iv.get("dir") or (cfgmod.ROOT.parent / "面试复盘"))
    readme = _readme_entries(base)

    linked = ambiguous = standalone = with_transcript = 0
    for marker, g in sorted(merged.items()):
        job_id, n_cand = _match_role(conn, g["company"])
        if job_id:
            linked += 1
        elif n_cand > 1:
            ambiguous += 1
        else:
            standalone += 1
        entry = _best_entry(readme.get(cfgmod.norm_key(g["company"]), []), g["round"])
        if entry:
            with_transcript += 1
        if dry_run:
            continue
        store.interview_upsert(
            conn, marker_id=marker, company=g["company"], happened_on=g["date"],
            round_type=g["round"], sources=json.dumps(g["sources"], ensure_ascii=False),
            job_id=job_id,
            transcript_url=(entry or {}).get("otter"),
            review_path=(entry or {}).get("path"))
        store.interview_replace_items(conn, marker, g["items"])
        # A review existing IS evidence the interview happened — advance the pipeline.
        row = conn.execute("SELECT job_id FROM interviews WHERE marker_id=?", (marker,)).fetchone()
        eff_job = row["job_id"] if row else job_id
        if eff_job:
            st = conn.execute("SELECT status FROM role_state WHERE job_id=?", (eff_job,)).fetchone()
            if st and st["status"] in ("applied", "interviewing"):
                store.record_signal(conn, eff_job, stage="interview", when=_day(g["date"]))
    return {"interviews": len(merged), "linked": linked, "ambiguous": ambiguous,
            "standalone": standalone, "with_transcript": with_transcript,
            "skipped": skipped}


def _day(d: str) -> str:
    return d if len(d) == 10 else f"{d}-01" if len(d) == 7 else f"{d}-01-01"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    cfg = cfgmod.load_config()
    conn = store.connect_view(cfg)
    try:
        res = ingest(cfg, conn, dry_run=args.dry_run)
        print(f"{res['interviews']} interviews indexed · {res['linked']} linked to roles ·"
              f" {res['ambiguous']} ambiguous · {res['standalone']} standalone ·"
              f" {res['with_transcript']} with transcript"
              + (" (dry run — nothing written)" if args.dry_run else ""))
        if not args.dry_run:
            for r in store.interviews_overview(conn)[:12]:
                tag = r["role_title"] or ""
                print(f"  {r['happened_on']:10}  {r['company'][:18]:18} {r['round_type'][:26]:26}"
                      f" q{r['n_questions']:>2} · act {r['n_done']}/{r['n_actions']}  {tag[:36]}")
        # Loud, last, and on both streams — the silence is what cost six weeks, so a drop now
        # ends the run with a nonzero status instead of scrolling past in a happy summary.
        if res["skipped"]:
            print(_skipped_report(res["skipped"]))
            print(_skipped_report(res["skipped"]), file=sys.stderr)
            return 3
    finally:
        conn.close()
    return 0


def _skipped_report(skipped: list[dict]) -> str:
    lines = [f"\n{'!' * 72}",
             f"⚠️  {len(skipped)} 节没有进 DB —— 这些行动项/题目一条都没被索引：", ""]
    for s in skipped:
        lines += [f"    {s['file']}:{s['line']}", f"      ## {s['heading'].lstrip('# ')}",
                  f"      ↳ {s['why']}"]
    lines += ["",
              "    修法：标题写成 `## 公司 · YYYY-MM-DD · 轮次`（日期第二段、`·` 不能少），",
              "          行动项用 `- [ ] `、题目用 `### Q: `，然后重跑本命令。",
              f"{'!' * 72}"]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
