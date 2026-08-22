"""Ingest tailored resumes: render each output/resumes/*.yaml to a PDF and link it.
Usage:
  python -m app.ingest            # one-shot: ingest new/changed tailored files
  python -m app.ingest --watch    # keep watching (5s polling)
  python -m app.ingest --all      # re-render everything, even unchanged
  python -m app.ingest --dry-run  # validate only: no PDFs, no DB writes, just the tally
"""
from __future__ import annotations

import argparse
import time

import yaml

from . import config as cfgmod
from . import store, tailoring


def _job_id_of(yf) -> str | None:
    """The job_id a failed file was claiming, so its role can be taken off `ready`."""
    try:
        t = yaml.safe_load(yf.read_text(encoding="utf-8"))
        return t.get("job_id") if isinstance(t, dict) else None
    except Exception:                                       # noqa: BLE001
        return None


def _demote(conn, yf, tally: dict) -> None:
    """A tailored file that fails validation must not leave the role claiming success.

    2026-08-10: the old behaviour printed `✗` and moved on, so the role kept
    tailor_status='ready' pointing at the PDF the OLD pipeline produced — the one the audit
    found fabricated tools and metrics in. /apply, the extension résumé lookup and the résumé
    chip in the UI all key off `ready`, so the stricter validator neither fixed nor flagged
    those résumés; it just declined to touch them while the UI kept offering them. A
    validation failure that leaves the system claiming success is worse than a crash.

    `invalid` is a status nothing serves: every consumer tests `== 'ready'`. The PDF and the
    YAML are left on disk untouched — this only stops the system from *recommending* them.
    prep_autotailor picks the role up again (it queues anything not `ready`), so the role
    heals on the next tailoring run instead of sitting there quietly wrong.
    """
    tally["failed"] = tally.get("failed", 0) + 1
    jid = _job_id_of(yf)
    if not jid:
        return
    role = store.get_role(conn, jid)
    if role and role.get("tailor_status") == "ready":
        store.set_tailor(conn, jid, tailor_status="invalid")
        tally["demoted"] = tally.get("demoted", 0) + 1
        print(f"   ↳ {jid}: tailor_status ready → invalid (was still offering the old PDF)")


def _scan_once(cfg: dict, seen: dict[str, float], force: bool = False,
               dry_run: bool = False, tally: dict | None = None) -> int:
    """Returns the number of files processed. `tally` (optional, filled in place) carries the
    valid/rejected/demoted counts — kept out of the return value because app/web.py's
    /api/ingest treats the return as a plain int."""
    resumes_dir = cfgmod.resolve_path(cfg["output_dirs"]["resumes"])
    n = 0
    tally = tally if tally is not None else {}
    conn = store.connect_view(cfg)
    base = tailoring.load_base(cfg) if dry_run else None
    try:
        for yf in sorted(resumes_dir.glob("*.yaml")):
            mtime = yf.stat().st_mtime
            if not force and seen.get(str(yf)) == mtime:
                continue
            try:
                if dry_run:
                    jid = _job_id_of(yf)
                    role = store.get_role(conn, jid) if jid else None
                    tailoring.validate_tailored(
                        yaml.safe_load(yf.read_text(encoding="utf-8")), base,
                        target_company=(role or {}).get("company"))
                    tally["ok"] = tally.get("ok", 0) + 1
                    n += 1
                    continue
                res = tailoring.ingest_file(cfg, conn, yf)
                seen[str(yf)] = mtime
                n += 1
                tally["ok"] = tally.get("ok", 0) + 1
                msg = f"✓ {res['company']:<16} → {res['pdf']}"
                if res["gaps"]:
                    msg += f"\n    gaps: {'; '.join(res['gaps'])}"
                print(msg)
            except tailoring.TailorError as exc:
                print(f"✗ {yf.name}: {exc}")
                if not dry_run:
                    _demote(conn, yf, tally)
                else:
                    tally["failed"] = tally.get("failed", 0) + 1
            except Exception as exc:
                # 2026-08-01 教训：第 941/2684 个文件抛了意外错误，整批停摆，后面
                # 1743 份全没做。单个文件的任何错误都只能损失它自己。
                try:
                    conn.rollback()
                except Exception:
                    pass
                print(f"✗ {yf.name}: 意外错误 {type(exc).__name__}: {exc} — 跳过，继续下一份")
                tally["crashed"] = tally.get("crashed", 0) + 1
    finally:
        conn.close()
    return n


def _report(tally: dict, dry_run: bool) -> None:
    """One summary line. Per-file `✗` output is ~1,500 lines on the current corpus and buries
    itself; the counts are what tells her whether a batch went wrong."""
    if not tally:
        return
    bits = [f"{tally.get('ok', 0)} valid", f"{tally.get('failed', 0)} rejected"]
    if tally.get("crashed"):
        bits.append(f"{tally['crashed']} errored")
    if tally.get("demoted"):
        bits.append(f"{tally['demoted']} demoted ready→invalid")
    print(f"=== {' · '.join(bits)}" + (" (dry run — nothing written)" if dry_run else ""))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.ingest", description=__doc__)
    ap.add_argument("--watch", action="store_true", help="keep polling every 5s")
    ap.add_argument("--all", action="store_true", help="re-render even unchanged files")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate only — no PDFs rendered, no DB writes, no demotions")
    args = ap.parse_args(argv)
    cfg = cfgmod.load_config()
    seen: dict[str, float] = {}

    if args.watch:
        print("watching output/resumes/ … (Ctrl+C to stop)")
        try:
            while True:
                _scan_once(cfg, seen, force=args.all and not seen, dry_run=args.dry_run)
                time.sleep(5)
        except KeyboardInterrupt:
            print("\nstopped.")
    else:
        tally: dict = {}
        n = _scan_once(cfg, seen, force=args.all, dry_run=args.dry_run, tally=tally)
        print(f"ingested {n} file(s)." if n else "nothing new to ingest.")
        _report(tally, args.dry_run)


if __name__ == "__main__":
    main()
