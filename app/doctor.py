"""Config health check:
  - hits every supported board and prints job count + sample titles (identity check —
    a slug can exist yet belong to someone else, e.g. gh:linkedin is a junk test board)
  - auto-appends a stub to company_notes.yaml for any configured company missing there
  - warns about missing Phase-2 resume angle files
Usage: python -m app.doctor"""
from __future__ import annotations

import sys
import time

from . import config as cfgmod
from . import store
from .sources import FETCHERS
from .sources.base import make_client


def main() -> None:
    cfg = cfgmod.load_config()
    notes = cfgmod.load_company_notes(cfg)
    client = make_client(cfg.get("http") or {})
    failures = 0

    for c in cfg.get("companies", []):
        name, ats, slug = c["name"], c["ats"], str(c.get("slug", ""))
        if ats not in FETCHERS:
            print(f"SKIP  {name:<16} {ats} ({c.get('note', '')})")
            continue
        try:
            postings = FETCHERS[ats](client, name, slug, retries=1)
            sample = " / ".join(p.title[:40] for p in postings[:3])
            print(f"OK    {name:<16} {ats}:{slug:<14} {len(postings):>4} jobs | {sample}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {name:<16} {ats}:{slug:<14} {type(exc).__name__}: {exc}")
        time.sleep(1.0)

    # --- auto-stub missing company notes ---
    missing = [c["name"] for c in cfg.get("companies", [])
               if cfgmod.norm_key(c["name"]) not in notes]
    if missing:
        path = cfgmod.resolve_path(cfg.get("company_notes", "company_notes.yaml"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n# --- auto-added by doctor {store.la_today()} — fill in the priors: ---\n")
            for name in missing:
                f.write(f'{cfgmod.norm_key(name)}: {{ size: TBD, notes: "TODO: fill priors" }}\n')
        print(f"\nnotes: added {len(missing)} stub(s) to {path.name}: {', '.join(missing)}")
    else:
        print("\nnotes: every configured company has a company_notes entry")

    for angle, rel in (cfg.get("resume_angles") or {}).items():
        if not cfgmod.resolve_path(rel).exists():
            print(f"note: resume angle '{angle}' file missing ({rel}) — needed for Phase 2")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
