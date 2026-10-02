"""Nightly quality eval of the AI autofill answers (data/logs/answers.jsonl).

Why: the extension used to fill forms with answers nobody ever re-read — one real form got
"I can't call that API" typed into it. app/ai.py now logs every generated output; this tool
judges the NEW records each night with an LLM rubric, writes a human-readable report to
output/answer_eval/YYYY-MM-DD.md, appends machine verdicts to data/logs/answer_eval.jsonl,
and distills recurring failures into data/answer_lessons.md — which app/ai.py feeds back
into the generation prompt, closing the loop.

Run (from the jobscan root, launchd does this nightly via tools/answer_eval.sh):
    .venv/bin/python -m tools.answer_eval             # judge records since the last run
    .venv/bin/python -m tools.answer_eval --all       # re-judge everything in the log
    .venv/bin/python -m tools.answer_eval --dry-run   # print the report, write nothing
Flags: --since YYYY-MM-DD  --max N (default 80)  --no-lessons  --log PATH (eval a seed file)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, ".")
from app import ai as aimod          # noqa: E402
from app import config as cfgmod     # noqa: E402
from app import store                # noqa: E402

STATE_REL = "data/answer_eval_state.json"
VERDICTS_REL = "data/logs/answer_eval.jsonl"
LESSONS_REL = "data/answer_lessons.md"
REPORT_DIR_REL = "output/answer_eval"
JUDGED_KINDS = {"answer", "cover_letter", "pick_option"}
MAX_LESSONS = 10

_JUDGE_SYSTEM = """You audit answers that an AI drafted onto real job-application forms for a
candidate (Casey Example). For each item, judge the ANSWER against the QUESTION, the candidate
facts, and the job context. Be strict — these went (or will go) onto real forms under her name.

Score each item:
- relevance 1-5: does it answer the question actually asked? (5 = directly and completely;
  1 = answers a different question or is generic filler that fits any question)
- grounded true/false: is every factual claim (employer, project, number, skill, degree)
  present in the candidate facts? List any invented claim in ungrounded_claims.
- voice 1-5: does it read like a real person talking, not AI/corporate template? Penalize
  long multi-clause sentences, "leverage/passionate/aligns with"-style filler, and abstract
  "X maps to Y" equivalences.
- red_flags, any of:
  "ai_disclosure"   — the text reveals or implies it was written by an AI/assistant
  "refusal"         — says "I can't / I'm unable to ..." about a task the form asked to perform
  "should_be_blank" — the question demanded an external action (call an API and paste the
                      output, run code, solve a challenge, attach a file), asked for
                      salary/compensation or referral source, or asked a personal fact NOT in
                      the candidate facts (non-compete, prior application/interview at this
                      company, anything else the facts don't cover) that got answered anyway. NOTE (owner 2026-09-09): legal /
                      eligibility / EEO answers that MATCH the recorded facts are CORRECT
                      behavior — do not flag those; flag them only when they contradict or
                      exceed the facts
  "placeholder"     — contains template residue like [Company], TODO, lorem, unfilled blanks
  "off_target"      — confidently answers about a different company/role than the context
  "overclaim"       — inflates seniority/scope beyond the facts (e.g. claims to have led an
                      org, invented years of experience)
- ok true/false: would you let this go onto the form unedited? (any red flag → false;
  relevance <= 2 or grounded=false → false)
- note: one short sentence saying the main problem, or "" if ok. Write notes in English.

For pick_option items, judge only whether the chosen option is the correct one given the
facts (relevance = correctness, voice = 5, grounded covers factual fit).

Return ONLY JSON: {"verdicts": [{"i": <item index>, "relevance": n, "grounded": bool,
"ungrounded_claims": [..], "voice": n, "red_flags": [..], "ok": bool, "note": ".."}],
"patterns": ["cross-item recurring problem, if any", ...]}"""

_DISTILL_SYSTEM = """You maintain a short list of writing rules for an AI that drafts
job-application answers. You are given the current rules and a batch of judged failures.
Produce the UPDATED full list: keep still-relevant existing rules, add at most 3 new ones
that would have prevented the recurring failures, drop duplicates. Rules must:
- be imperative, one line, <= 25 words, in English, generalizable (never company-specific);
- never contradict: ground only in given facts; never answer EEO/legal/salary; return
  [NEEDS_HUMAN] for questions demanding an external action.
- NOT restate what a deterministic validator already hard-blocks after generation
  (AI-disclosure phrasing, refusing a task, bracketed placeholders/TODO). A prompt rule
  the model keeps ignoring is dead weight — spend the list on relevance, grounding,
  and voice, which only the prompt can shape.
Maximum {max_lessons} rules. Return ONLY the rules, one per line, each starting with "- "."""


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass  # one corrupt line must not kill the nightly run
    return out


def _client(cfg: dict):
    import anthropic
    key = aimod.load_key(cfg)
    if not key:
        raise SystemExit("no Anthropic API key (secrets.yaml / $ANTHROPIC_API_KEY)")
    return anthropic.Anthropic(api_key=key)


def _judge_job(cfg: dict, client, role: dict | None, meta: dict, items: list[dict]) -> list[dict]:
    """One judge call for all of one job's records. Returns items merged with verdicts."""
    ai = cfg.get("ai") or {}
    facts = aimod._facts(cfg, role) if role else "(role no longer in DB — judge grounding against the question only)"
    jd = (role or {}).get("jd_text") or ""
    lines = []
    for n, r in enumerate(items):
        if r["kind"] == "pick_option":
            lines.append(f"[{n}] DROPDOWN question: {r.get('question')}\n"
                         f"    options: {r.get('options')}\n    chosen: {r.get('answer')!r}")
        else:
            q = r.get("question") or "(cover letter / 'anything else' box)"
            lines.append(f"[{n}] QUESTION: {q}\n    ANSWER: {r.get('answer')}")
    user = (f"Candidate facts:\n{facts}\n\n"
            f"Job context: {meta.get('company')} — {meta.get('title')}\n"
            f"Job description (may be truncated):\n{jd[:4000]}\n\n"
            f"Items to judge:\n" + "\n\n".join(lines) +
            "\n\nJudge every item and return the JSON verdict object.")
    resp = client.messages.create(
        model=ai.get("model", "claude-opus-4-8"),
        max_tokens=3000,
        output_config={"effort": ai.get("effort", "high")},
        system=_JUDGE_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    data = json.loads(aimod._extract_json(text))
    verd = {int(v.get("i", -1)): v for v in (data.get("verdicts") or []) if isinstance(v, dict)}
    out = []
    for n, r in enumerate(items):
        v = verd.get(n) or {"relevance": None, "grounded": None, "voice": None,
                            "red_flags": ["judge_missing"], "ok": False,
                            "note": "judge returned no verdict for this item"}
        out.append({**r, "eval": {
            "relevance": v.get("relevance"), "grounded": v.get("grounded"),
            "ungrounded_claims": v.get("ungrounded_claims") or [],
            "voice": v.get("voice"), "red_flags": v.get("red_flags") or [],
            "ok": bool(v.get("ok")), "note": str(v.get("note") or "").strip(),
        }, "eval_patterns": data.get("patterns") or []})
    return out


def _update_lessons(cfg: dict, client, failures: list[dict], lessons_path: Path) -> tuple[str, str]:
    """Distill recurring failures into the lessons file. Returns (old_text, new_text)."""
    old = lessons_path.read_text(encoding="utf-8").strip() if lessons_path.exists() else ""
    fail_lines = []
    for r in failures:
        e = r["eval"]
        fail_lines.append(f"- company={r.get('company')} kind={r['kind']} "
                          f"question={str(r.get('question'))[:120]!r} "
                          f"flags={e['red_flags']} note={e['note']!r} "
                          f"ungrounded={e['ungrounded_claims']}")
    ai = cfg.get("ai") or {}
    user = (f"Current rules:\n{old or '(none yet)'}\n\n"
            f"Judged failures from the latest batch:\n" + "\n".join(fail_lines) +
            "\n\nReturn the updated rule list.")
    resp = client.messages.create(
        model=ai.get("model", "claude-opus-4-8"),
        max_tokens=1200,
        system=_DISTILL_SYSTEM.replace("{max_lessons}", str(MAX_LESSONS)),
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    rules = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("- ")]
    if not rules:
        return old, old
    new = "\n".join(rules[:MAX_LESSONS]) + "\n"
    lessons_path.parent.mkdir(parents=True, exist_ok=True)
    lessons_path.write_text(new, encoding="utf-8")
    return old, new.strip()


def _render_report(day: str, judged: list[dict], skipped: list[dict],
                   lessons_diff: tuple[str, str] | None) -> str:
    ok = [r for r in judged if r["eval"]["ok"]]
    bad = [r for r in judged if not r["eval"]["ok"]]
    jobs = sorted({f"{r.get('company')} — {r.get('title')}" for r in judged})
    L = [f"# Autofill answer eval — {day}", "",
         f"- judged: **{len(judged)}** outputs across **{len(jobs)}** roles "
         f"({sum(1 for r in judged if r['kind'] == 'answer')} answers, "
         f"{sum(1 for r in judged if r['kind'] == 'cover_letter')} cover letters, "
         f"{sum(1 for r in judged if r['kind'] == 'pick_option')} dropdown picks)",
         f"- pass: **{len(ok)}**   fail: **{len(bad)}**",
         f"- unanswered by design (never-filter / needs-human / declined): {len(skipped)}", ""]
    if bad:
        L.append("## ✗ Failures (fix or re-fill these fields by hand)\n")
        for r in bad:
            e = r["eval"]
            L += [f"### {r.get('company')} — {str(r.get('question') or '(cover letter)')[:100]}",
                  f"- kind: {r['kind']}  |  job_id: `{r.get('job_id')}`  |  ts: {r.get('ts')}",
                  f"- relevance {e['relevance']}/5 · voice {e['voice']}/5 · grounded: {e['grounded']}"
                  f" · flags: {', '.join(e['red_flags']) or '—'}",
                  f"- judge: {e['note']}"]
            if e["ungrounded_claims"]:
                L.append(f"- invented claims: {e['ungrounded_claims']}")
            L += ["", "> " + str(r.get("answer")).replace("\n", "\n> "), ""]
    pats = sorted({p for r in judged for p in r.get("eval_patterns", [])})
    if pats:
        L += ["## Recurring patterns the judge saw", ""] + [f"- {p}" for p in pats] + [""]
    if lessons_diff and lessons_diff[0] != lessons_diff[1]:
        L += ["## Lessons file updated (feeds back into generation prompt)", "",
              "**Before:**", "```", lessons_diff[0] or "(empty)", "```",
              "**After:**", "```", lessons_diff[1] or "(empty)", "```", ""]
    if not bad:
        L.append("All judged outputs passed. 🎉")
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="ignore state, judge the whole log")
    ap.add_argument("--since", help="judge records with ts >= this (YYYY-MM-DD)")
    ap.add_argument("--max", type=int, default=80, help="cap on records judged per run")
    ap.add_argument("--no-lessons", action="store_true", help="don't rewrite answer_lessons.md")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the report to stdout; write no state/verdicts/lessons/report")
    ap.add_argument("--log", default=None, help="alternate answers.jsonl to eval (for testing)")
    args = ap.parse_args()

    cfg = cfgmod.load_config()
    log_path = Path(args.log) if args.log else cfgmod.resolve_path("data/logs/answers.jsonl")
    state_path = cfgmod.resolve_path(STATE_REL)
    records = _read_jsonl(log_path)
    if not records:
        print(f"nothing to eval — {log_path} is empty or missing")
        return

    last_ts = ""
    if not args.all and not args.since and state_path.exists():
        try:
            last_ts = (json.loads(state_path.read_text()) or {}).get("last_ts", "")
        except Exception:
            last_ts = ""
    floor = args.since or last_ts
    fresh = [r for r in records if not floor or str(r.get("ts", "")) > floor]
    judged_in = [r for r in fresh if r.get("kind") in JUDGED_KINDS and r.get("answer")]
    skipped = [r for r in fresh if r.get("kind") == "unanswered"]
    if not judged_in:
        print(f"no new answers since {floor or 'the beginning'} — {len(fresh)} other records")
        return
    dropped = len(judged_in) - args.max
    judged_in = judged_in[-args.max:]
    if dropped > 0:
        print(f"capping at --max {args.max}: skipping {dropped} older records this run")

    client = _client(cfg)
    try:
        conn = store.connect_view(cfg)
    except Exception:
        conn = None

    by_job: dict[str, list[dict]] = defaultdict(list)
    for r in judged_in:
        by_job[str(r.get("job_id"))].append(r)

    judged: list[dict] = []
    judge_errors = 0
    for job_id, items in by_job.items():
        role = None
        if conn is not None and job_id and job_id != "None":
            try:
                role = store.get_role(conn, job_id)
            except Exception:
                role = None
        meta = {"company": (role or items[0]).get("company"),
                "title": (role or items[0]).get("title")}
        try:
            judged.extend(_judge_job(cfg, client, role, meta, items))
        except Exception as e:
            judge_errors += 1
            print(f"!! judge failed for job {job_id} ({meta['company']}): {type(e).__name__}: {e}")

    if not judged:
        raise SystemExit("every judge call failed — check the API key / network")

    failures = [r for r in judged if not r["eval"]["ok"]]
    lessons_diff = None
    if failures and not args.no_lessons and not args.dry_run:
        try:
            lessons_diff = _update_lessons(cfg, client, failures,
                                           cfgmod.resolve_path(LESSONS_REL))
        except Exception as e:
            print(f"!! lessons update failed: {type(e).__name__}: {e}")

    day = time.strftime("%Y-%m-%d")
    report = _render_report(day, judged, skipped, lessons_diff)
    if args.dry_run:
        print(report)
        return

    vp = cfgmod.resolve_path(VERDICTS_REL)
    vp.parent.mkdir(parents=True, exist_ok=True)
    with vp.open("a", encoding="utf-8") as f:
        for r in judged:
            f.write(json.dumps({**r, "eval_run": day}, ensure_ascii=False) + "\n")
    rp = cfgmod.resolve_path(REPORT_DIR_REL) / f"{day}.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(report, encoding="utf-8")
    if judge_errors:
        # don't advance the floor past unjudged records — they get retried next run
        # (re-judged jobs just append a second verdict row, which is harmless)
        print(f"!! {judge_errors} judge call(s) failed — state not advanced, will retry next run")
    elif not args.log:   # a seed/test log must never move the real pipeline's floor
        max_ts = max(str(r.get("ts", "")) for r in judged_in)
        state_path.write_text(json.dumps({"last_ts": max_ts}), encoding="utf-8")
    print(f"judged {len(judged)} outputs — {len(failures)} failed. report: {rp}")


if __name__ == "__main__":
    main()
