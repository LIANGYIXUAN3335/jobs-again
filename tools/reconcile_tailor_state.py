#!/usr/bin/env python3
"""reconcile_tailor_state.py — make `role_state.tailor_status` tell the truth about what is
actually on disk in `output/resumes`, in BOTH directions.

Why this exists (2026-08-10): `queued` means "prompt written, résumé not produced yet". It is
supposed to last one autotailor run. But nothing expires it, and `tools/prep_autotailor.py`
only ever re-picks *today's* lead role per company from the review queue — so the moment a
role leaves that queue (company retired by the application budget, posting delisted, verdict
flipped to `drop`, or she skipped it) its `queued` row is orphaned and no run ever comes back
for it. 47 rows had been parked that way for up to 16 days, every one of them unreachable by
the pipeline that was meant to finish them. The counts on the review page said "47 in flight";
the truth was "47 abandoned".

The résumé on disk is the ground truth, the DB row is a cache of it, and either can drift:

  promote   not `ready` but a real PDF exists     -> mark ready + record the path
  relink    `ready` but the recorded path is gone -> repoint at the PDF that IS there
  inherit   a duplicate_of twin has the PDF       -> copy the pointer onto this row
  demote    `ready` with no file anywhere         -> back to `none` so the daily prep redoes it
  resolve   `queued` on a role the pipeline can never reach again -> `none`
  render    a validated tailored YAML but no PDF  -> re-ingest it (needs --render + typst)

`prompt_path` is never cleared and no file is ever deleted: `none` + a prompt on disk still
reads as "a prompt was written for this role once", and if the role ever becomes eligible
again `prep_autotailor` picks it up (it selects everything that is not `ready`) and rewrites
the prompt from the current JD anyway.

Dry run by DEFAULT — `--apply` is required to write, and it snapshots the user DB into
.backups/<date>/ first (same convention as data/user_state.db.bak-2026-07-24).

    python tools/reconcile_tailor_state.py                  # dry run (default)
    python tools/reconcile_tailor_state.py --verbose        # + every affected row
    python tools/reconcile_tailor_state.py --apply          # repair, after backing the DB up
    python tools/reconcile_tailor_state.py --apply --render # also re-render pending YAMLs
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
import time
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import budget, config as cfgmod, doctor, store, tailoring  # noqa: E402

BACKUP_DIR = ".backups"

# A status that says the decision about this role is already made. A `queued` prompt behind one
# of these is never coming back — she is not going to apply, or she already did.
TERMINAL_STATUSES = {"applied", "skip", "rejected"}

# Order matters: the first reason that fits is the one reported, hardest evidence first.
RESOLVE_REASONS = ("role-gone", "status-applied", "status-skip", "status-rejected",
                   "delisted", "verdict-drop", "company-retired", "company-blocked")


# ---------- evidence on disk ----------

def _pdf_names(cfg: dict) -> list[str]:
    d = cfgmod.resolve_path(cfg["output_dirs"]["resumes"])
    if not d.is_dir():
        return []
    return [p.name for p in d.iterdir() if p.suffix == ".pdf"]


def _file_ok(p: str | Path | None) -> bool:
    """A résumé pointer is only good if the file is there AND has bytes. A 0-byte PDF is what
    a killed typst run leaves behind, and it opens to nothing in the recruiter's viewer."""
    if not p:
        return False
    try:
        f = Path(p)
        return f.is_file() and f.stat().st_size > 0
    except OSError:
        return False


def find_pdf(cfg: dict, company: str | None, title: str | None, job_id: str,
             names: list[str]) -> str | None:
    """The rendered PDF for this role, found without trusting the DB.

    `friendly_pdf_prefix` embeds the raw ATS id, so a prefix match can only ever be THIS
    role's file — never a sibling posting at the same company. Matched with startswith, not
    glob: the "[ats-id]" makes the prefix a character class to glob (same trap documented in
    tailoring.ingest_file). Newest wins when re-scoring left more than one behind."""
    if not company:
        return None
    d = cfgmod.resolve_path(cfg["output_dirs"]["resumes"])
    prefix = tailoring.friendly_pdf_prefix(company, title or "", job_id)
    hits = [d / n for n in names if n.startswith(prefix)]
    hits = [p for p in hits if _file_ok(p)]
    if hits:
        return str(max(hits, key=lambda p: p.stat().st_mtime))
    legacy = tailoring.resume_pdf_path(cfg, company, job_id)   # pre-2026-07 naming
    return str(legacy) if _file_ok(legacy) else None


def yaml_state(cfg: dict, company: str | None, job_id: str) -> str:
    """'missing' | 'invalid: …' | 'valid' for the tailored YAML behind this role."""
    if not company:
        return "missing"
    p = tailoring.resume_yaml_path(cfg, company, job_id)
    if not _file_ok(p):
        return "missing"
    try:
        t = yaml.safe_load(p.read_text(encoding="utf-8"))
        tailoring.validate_tailored(t, tailoring.load_base(cfg))
    except Exception as exc:                                    # noqa: BLE001
        return f"invalid: {type(exc).__name__}: {str(exc)[:80]}"
    return "valid"


# ---------- why a queued row is stuck ----------

def _delisted_map(conn) -> dict[str, str]:
    """Newest last_seen per company, scanner-sourced only — the same signal prune_delisted
    uses. cap:/manual: rows have no scan coverage so they can never be called delisted."""
    return {r["company_norm"]: r["m"] for r in conn.execute(
        "SELECT company_norm, max(substr(last_seen,1,10)) m FROM jobs.roles"
        " WHERE job_id NOT LIKE 'cap:%' AND job_id NOT LIKE 'manual:%'"
        " GROUP BY company_norm")}


def _budget_state(cfg: dict, conn) -> tuple[dict, dict]:
    """{company_norm: stock}, {company_norm: blocked} from the application budget, or empty
    dicts when the budget is off / unavailable. A company whose lifetime stock is spent is
    `retired` and NEVER refills (app/budget.py: "Never resets"), so a role behind one can not
    re-enter the review queue — which is exactly why its prompt was orphaned."""
    if not budget.enabled(cfg):
        return {}, {}
    try:
        rows = store.fetch_view(conn, cfg, status="all")["rows"]
        today = store.la_today()
        return budget.stock_status(rows, cfg, today), budget.blocked_status(rows, cfg, today)
    except Exception:                                           # noqa: BLE001
        return {}, {}      # a diagnosis aid, never a reason to fail the repair


def stuck_reason(row: dict, *, delisted: bool, stock: dict, blocked: dict) -> str | None:
    """Why this `queued` row can never be finished by the daily prep — None if it still can."""
    if not row.get("company"):
        return "role-gone"                        # roles row pruned; nothing left to tailor
    if row.get("status") in TERMINAL_STATUSES:
        return f"status-{row['status']}"
    if delisted:
        return "delisted"
    if row.get("verdict") == "drop":
        # store.fetch_view: verdict 'drop' is "a dead-end — keep it out of the review queue
        # entirely … and never auto-tailored".
        return "verdict-drop"
    cn = row.get("company_norm") or ""
    if (stock.get(cn) or {}).get("retired"):
        return "company-retired"
    if (blocked.get(cn) or {}).get("blocked"):
        return "company-blocked"
    return None


# ---------- the scan ----------

def scan(cfg: dict, conn) -> dict:
    """Walk role_state × the filesystem. Pure read — decides, changes nothing."""
    names = _pdf_names(cfg)
    comax = _delisted_map(conn)
    stock, blocked = _budget_state(cfg, conn)
    now = time.time()

    rows = [dict(r) for r in conn.execute(
        "SELECT s.job_id, s.status, s.tailor_status, s.prompt_path, s.tailored_resume_path,"
        "       s.applied_with_resume, s.applied_date, s.status_updated_at, s.duplicate_of,"
        "       r.company, r.company_norm, r.title, r.last_seen, r.verdict"
        " FROM role_state s LEFT JOIN jobs.roles r ON r.job_id = s.job_id")]
    by_id = {r["job_id"]: r for r in rows}

    before: dict[str, int] = {}
    findings: list[dict] = []
    for r in rows:
        ts = r["tailor_status"] or "none"        # NULL is 'none' with a different spelling
        raw = r["tailor_status"] if r["tailor_status"] is not None else "(null)"
        before[raw] = before.get(raw, 0) + 1
        recorded = r["tailored_resume_path"]
        have = _file_ok(recorded)
        found = recorded if have else find_pdf(cfg, r["company"], r["title"], r["job_id"], names)
        src = "recorded" if have else ("on-disk" if found else "")
        if not found and r["duplicate_of"]:
            # A li: shadow and its ATS canonical are ONE physical posting sharing ONE PDF
            # (store.reconcile_duplicates). If the twin has it, this row's résumé is not
            # missing — only its pointer is.
            twin = by_id.get(r["duplicate_of"]) or {}
            if _file_ok(twin.get("tailored_resume_path")):
                found, src = twin["tailored_resume_path"], "duplicate_of"

        f = {"job_id": r["job_id"], "company": r["company"], "title": r["title"],
             "status": r["status"], "tailor_status": ts, "raw_tailor_status": raw,
             "recorded": recorded,
             "applied_with_resume": r["applied_with_resume"], "applied_date": r["applied_date"],
             "found": found, "found_via": src, "action": "ok", "reason": "",
             "new_tailor_status": None, "new_path": None,
             "age_days": doctor.queued_age_days(r["prompt_path"], r["status_updated_at"], now)}

        if found and ts != "ready":
            f.update(action="promote", reason=f"résumé on disk ({src})",
                     new_tailor_status="ready", new_path=found)
        elif found and not have:
            f.update(action="relink", reason=f"recorded path missing, résumé found ({src})",
                     new_tailor_status="ready", new_path=found)
        elif not found and ts == "ready":
            f.update(action="demote", reason="marked ready but no résumé file",
                     new_tailor_status="none", new_path=None)
        elif not found and ts == "queued":
            ys = yaml_state(cfg, r["company"], r["job_id"])
            # A YAML from before auto_tailor.stale_before came out of the pipeline that
            # keyword-stuffed and occasionally fabricated. Rendering one would put `ready`
            # on a résumé the owner already ruled untrustworthy (her call 2026-08-06: don't
            # batch-regenerate, refresh on demand). So a stale YAML counts as no YAML here.
            stale = ys == "valid" and tailoring.resume_is_stale(cfg, r["company"], r["job_id"])
            if ys == "valid" and not stale:
                f.update(action="render", reason="tailored YAML validates but was never rendered")
            else:
                note = " + stale YAML left for the on-demand refresh" if stale else ""
                why = stuck_reason(r, delisted=_is_delisted(r, comax), stock=stock,
                                   blocked=blocked)
                if why:
                    f.update(action="resolve", reason=why + note, new_tailor_status="none")
                else:
                    f.update(action="stuck-live",
                             reason=(ys if ys != "missing" else "never ran") + note)
        findings.append(f)

    return {"before": before, "findings": findings,
            "applied_no_resume": _applied_without_resume(findings),
            "orphan_yaml": orphan_yamls(cfg, {r["job_id"] for r in rows})}


def _is_delisted(r: dict, comax: dict[str, str]) -> bool:
    if r["job_id"].startswith(("cap:", "manual:")):
        return False
    seen = (r["last_seen"] or "")[:10]
    newest = comax.get(r["company_norm"] or "") or ""
    return bool(seen and newest and seen < newest)


def _applied_without_resume(findings: list[dict]) -> list[dict]:
    """Roles marked applied that will still have no résumé pointer after this run. If a
    recruiter calls about one, there is no way to reproduce what she sent."""
    out = []
    for f in findings:
        if f["status"] != "applied":
            continue
        if f["found"] or f["action"] in ("promote", "relink", "render"):
            continue
        out.append(f)
    return out


def orphan_yamls(cfg: dict, known_ids: set[str]) -> list[dict]:
    """Tailored YAMLs on disk whose job_id has no role_state row left (the role was pruned).
    Reported only — there is no row to repair and inventing one would resurrect a dead role."""
    d = cfgmod.resolve_path(cfg["output_dirs"]["resumes"])
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.yaml")):
        try:
            t = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            jid = t.get("job_id")
        except Exception:                                       # noqa: BLE001
            out.append({"file": p.name, "job_id": None, "why": "unparseable"})
            continue
        if not jid:
            out.append({"file": p.name, "job_id": None, "why": "no job_id"})
        elif jid not in known_ids:
            out.append({"file": p.name, "job_id": jid, "why": "no role_state row"})
    return out


# ---------- the repair ----------

def backup_user_db(cfg: dict) -> Path:
    """Snapshot the user DB before writing. sqlite's own backup API, not a file copy: the DB
    runs in WAL mode, so copying the .db alone can miss committed pages still in the -wal."""
    src = cfgmod.user_db_path(cfg)
    now = datetime.datetime.now()
    dest_dir = cfgmod.resolve_path(BACKUP_DIR) / now.strftime("%Y-%m-%d")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{src.name}.bak-{now.strftime('%H%M%S')}"
    s = sqlite3.connect(str(src))
    try:
        d = sqlite3.connect(str(dest))
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()
    return dest


def apply_findings(cfg: dict, conn, findings: list[dict], *, render: bool = False) -> dict:
    stats = {"promote": 0, "relink": 0, "demote": 0, "resolve": 0, "render": 0, "failed": 0}
    for f in findings:
        act = f["action"]
        if act in ("promote", "relink"):
            store.set_tailor(conn, f["job_id"], tailor_status="ready",
                             tailored_resume_path=f["new_path"])
            stats[act] += 1
        elif act in ("demote", "resolve"):
            # prompt_path is deliberately left alone — it is the record that a prompt was
            # written, and prep_autotailor rewrites it from the current JD when it re-picks
            # the role (it takes everything that is not `ready`).
            store.set_tailor(conn, f["job_id"], tailor_status="none", tailored_resume_path=None)
            stats[act] += 1
        elif act == "render" and render:
            try:
                yf = tailoring.resume_yaml_path(cfg, f["company"], f["job_id"])
                res = tailoring.ingest_file(cfg, conn, yf)       # validates, renders, marks ready
                f["new_path"] = res["pdf"]
                stats["render"] += 1
            except Exception as exc:                            # noqa: BLE001
                f["reason"] = f"render failed: {type(exc).__name__}: {exc}"
                stats["failed"] += 1
    return stats


# ---------- reporting ----------

def _counts(findings: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in findings:
        out[f["action"]] = out.get(f["action"], 0) + 1
    return out


def _after(before: dict[str, int], findings: list[dict], *, render: bool) -> dict[str, int]:
    after = dict(before)
    for f in findings:
        new = f["new_tailor_status"]
        if f["action"] == "render":
            new = "ready" if render else None
        old = f["raw_tailor_status"]
        if not new or new == old:
            continue
        after[old] = after.get(old, 0) - 1
        after[new] = after.get(new, 0) + 1
    return {k: v for k, v in after.items() if v}


def _fmt_counts(d: dict[str, int]) -> str:
    return "  ".join(f"{k}={v}" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))


def report(cfg: dict, res: dict, *, apply_: bool, render: bool, verbose: bool) -> None:
    findings = res["findings"]
    counts = _counts(findings)
    changed = [f for f in findings if f["action"] not in ("ok", "stuck-live")]
    mode = "APPLIED" if apply_ else "DRY RUN (nothing written — pass --apply to repair)"
    print(f"reconcile_tailor_state — {mode}")
    print(f"  rows scanned: {len(findings)}")
    print(f"  before: {_fmt_counts(res['before'])}")
    print(f"  after:  {_fmt_counts(_after(res['before'], findings, render=render))}")
    print()

    order = ["promote", "relink", "render", "demote", "resolve", "stuck-live"]
    labels = {
        "promote": "promote  -> ready   (résumé exists, DB said otherwise)",
        "relink":  "relink   -> ready   (recorded path dead, real file found)",
        "render":  "render   -> ready   (valid YAML, PDF never rendered)"
                   + ("" if render else "   [needs --render]"),
        "demote":  "demote   -> none    (marked ready, file missing/empty)",
        "resolve": "resolve  -> none    (queued on a role the pipeline can't reach)",
        "stuck-live": "still queued        (live + reachable: the daily prep should finish these)",
    }
    for act in order:
        n = counts.get(act, 0)
        if not n:
            continue
        print(f"  {n:>4}  {labels[act]}")
        reasons: dict[str, int] = {}
        for f in findings:
            if f["action"] == act:
                reasons[f["reason"]] = reasons.get(f["reason"], 0) + 1
        for why, k in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"          {k:>4}  {why}")
        if verbose:
            for f in findings:
                if f["action"] == act:
                    age = f"{f['age_days']}d" if f["age_days"] is not None else "?"
                    print(f"          - {f['job_id']:<48} {str(f['company'])[:20]:<20} "
                          f"{f['status']:<6} queued {age}")
        print()

    if not changed:
        print("  no drift to repair.\n")

    ap = res["applied_no_resume"]
    print(f"  applied without a tailored résumé on file: {len(ap)}")
    if ap:
        # `applied_with_resume` splits this cohort in two, and only one half is a problem:
        # 'base' means she deliberately sent resumes/resume_base.yaml and the record is
        # complete; 'tailored' means a tailored PDF was sent and NOTHING on disk reproduces it.
        variants: dict[str, list] = {}
        for f in ap:
            variants.setdefault(f["applied_with_resume"] or "(unrecorded)", []).append(f)
        for v, rows_ in sorted(variants.items(), key=lambda kv: -len(kv[1])):
            note = ("sent the base résumé — provenance is complete" if v == "base"
                    else "NO record of what was sent — unreproducible")
            print(f"      {len(rows_):>4}  applied_with_resume={v!r:<13} {note}")
            for f in (rows_ if (verbose or v != "base") else rows_[:3]):
                print(f"            {f['job_id']:<48} {str(f['company'])[:22]:<22} "
                      f"{f['applied_date'] or ''}")
            if not verbose and v == "base" and len(rows_) > 3:
                print(f"            … {len(rows_) - 3} more (--verbose for all)")
    print()

    orph = res["orphan_yaml"]
    if orph:
        print(f"  tailored YAMLs with no role_state row: {len(orph)} "
              f"(role pruned after the résumé was made — kept, not repairable)")
        for o in (orph if verbose else orph[:5]):
            print(f"      {o['file']}")
        if not verbose and len(orph) > 5:
            print(f"      … {len(orph) - 5} more (--verbose for all)")
        print()

    stale = doctor.stale_queued(cfg)
    days = doctor.queued_stale_days(cfg)
    if stale:
        print(f"  guardrail: {len(stale)} role(s) still parked in `queued` ≥{days}d "
              f"— `python -m app.doctor --tailor` reports this on its own.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Repair drift between role_state.tailor_status and output/resumes.",
        epilog="Dry run by default. --apply backs the user DB up into .backups/<date>/ first.")
    ap.add_argument("--apply", action="store_true",
                    help="write the repairs (default: dry run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit no-op; the default anyway")
    ap.add_argument("--render", action="store_true",
                    help="also re-render résumés whose tailored YAML validates but has no PDF "
                         "(runs typst; only does anything together with --apply)")
    ap.add_argument("--verbose", "-v", action="store_true", help="list every affected role")
    args = ap.parse_args(argv)
    if args.apply and args.dry_run:
        ap.error("--apply and --dry-run are contradictory")

    cfg = cfgmod.load_config()
    conn = store.connect_view(cfg)
    res = scan(cfg, conn)

    if args.apply:
        dest = backup_user_db(cfg)
        print(f"backup: {dest}")
        stats = apply_findings(cfg, conn, res["findings"], render=args.render)
        res["applied_no_resume"] = _applied_without_resume(res["findings"])
        print(f"wrote: {_fmt_counts({k: v for k, v in stats.items() if v})or 'nothing'}\n")

    report(cfg, res, apply_=args.apply, render=args.render, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
