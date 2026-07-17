"""Ingest tailored resumes: render each output/resumes/*.yaml to a PDF and link it.
Usage:
  python -m app.ingest            # one-shot: ingest new/changed tailored files
  python -m app.ingest --watch    # keep watching (5s polling)
  python -m app.ingest --all      # re-render everything, even unchanged
"""
from __future__ import annotations

import argparse
import time

from . import config as cfgmod
from . import store, tailoring


def _scan_once(cfg: dict, seen: dict[str, float], force: bool = False) -> int:
    resumes_dir = cfgmod.resolve_path(cfg["output_dirs"]["resumes"])
    n = 0
    conn = store.connect_view(cfg)
    try:
        for yf in sorted(resumes_dir.glob("*.yaml")):
            mtime = yf.stat().st_mtime
            if not force and seen.get(str(yf)) == mtime:
                continue
            try:
                res = tailoring.ingest_file(cfg, conn, yf)
                seen[str(yf)] = mtime
                n += 1
                msg = f"✓ {res['company']:<16} → {res['pdf']}"
                if res["gaps"]:
                    msg += f"\n    gaps: {'; '.join(res['gaps'])}"
                print(msg)
            except tailoring.TailorError as exc:
                print(f"✗ {yf.name}: {exc}")
    finally:
        conn.close()
    return n


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.ingest", description=__doc__)
    ap.add_argument("--watch", action="store_true", help="keep polling every 5s")
    ap.add_argument("--all", action="store_true", help="re-render even unchanged files")
    args = ap.parse_args(argv)
    cfg = cfgmod.load_config()
    seen: dict[str, float] = {}

    if args.watch:
        print("watching output/resumes/ … (Ctrl+C to stop)")
        try:
            while True:
                _scan_once(cfg, seen, force=args.all and not seen)
                time.sleep(5)
        except KeyboardInterrupt:
            print("\nstopped.")
    else:
        n = _scan_once(cfg, seen, force=args.all)
        print(f"ingested {n} file(s)." if n else "nothing new to ingest.")


if __name__ == "__main__":
    main()
