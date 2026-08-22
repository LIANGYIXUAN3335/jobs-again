"""Config health check:
  - hits every supported board and prints job count + sample titles (identity check —
    a slug can exist yet belong to someone else, e.g. gh:linkedin is a junk test board)
  - auto-appends a stub to company_notes.yaml for any configured company missing there
  - warns about missing Phase-2 resume angle files
  - flags roles parked in tailor_status='queued' (see stale_queued below)
Usage: python -m app.doctor            (full check — hits the network)
       python -m app.doctor --tailor   (only the tailor-queue check — offline, instant)"""
from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path

from . import config as cfgmod
from . import store
from .sources import FETCHERS
from .sources.base import make_client

# --- Phase-2 tailoring queue ------------------------------------------------------------
# `queued` = "prompt written, résumé not produced yet". It is meant to last one autotailor
# run, but nothing expires it and tools/prep_autotailor.py only re-picks today's lead role
# per company — so a role that drops out of the review queue (company retired by the
# application budget, posting delisted, verdict flipped to drop, she skipped it) keeps its
# prompt forever and no run ever comes back for it. 47 rows had silently accumulated that
# way by 2026-08-10, the oldest 16 days old, and nothing in the system said so.
# The fix for whatever this reports is `tools/reconcile_tailor_state.py`.
QUEUED_STALE_DAYS = 3


def queued_stale_days(cfg: dict) -> int:
    return int((cfg.get("auto_tailor") or {}).get("queued_stale_days", QUEUED_STALE_DAYS))


def queued_age_days(prompt_path: str | None, status_updated_at: str | None,
                    now: float | None = None) -> int | None:
    """How long this row has been queued. role_state has no queued_at, so the age comes from
    the prompt file's mtime — the moment tailoring.write_prompt ran — and falls back to
    status_updated_at. None when neither exists, which is itself drift (a queued row whose
    prompt is gone), so callers treat None as stale rather than as fresh."""
    now = now if now is not None else time.time()
    if prompt_path:
        try:
            return max(0, int((now - Path(prompt_path).stat().st_mtime) // 86400))
        except OSError:
            pass
    if status_updated_at:
        try:
            d = datetime.date.fromisoformat(str(status_updated_at)[:10])
            return max(0, (datetime.date.today() - d).days)
        except ValueError:
            pass
    return None


def stale_queued(cfg: dict, conn=None, days: int | None = None) -> list[dict]:
    """Roles parked in `queued` for at least `days`. Oldest (and age-unknown) first."""
    days = queued_stale_days(cfg) if days is None else int(days)
    own = conn is None
    conn = conn or store.connect_user(cfg)
    try:
        rows = conn.execute(
            "SELECT job_id, prompt_path, status_updated_at FROM role_state"
            " WHERE tailor_status='queued'").fetchall()
    finally:
        if own:
            conn.close()
    now = time.time()
    out = [{"job_id": r["job_id"],
            "age_days": queued_age_days(r["prompt_path"], r["status_updated_at"], now)}
           for r in rows]
    out = [r for r in out if r["age_days"] is None or r["age_days"] >= days]
    out.sort(key=lambda r: -(r["age_days"] if r["age_days"] is not None else 10 ** 6))
    return out


def check_tailor_queue(cfg: dict, conn=None) -> int:
    """Print the tailor-queue verdict; returns the number of parked roles (0 = healthy)."""
    days = queued_stale_days(cfg)
    stale = stale_queued(cfg, conn)
    if not stale:
        print(f"\ntailor queue: OK — nothing parked in `queued` for {days}+ days")
        return 0
    oldest = next((s["age_days"] for s in stale if s["age_days"] is not None), None)
    print(f"\nFAIL  tailor queue: {len(stale)} role(s) stuck in tailor_status='queued' for "
          f"{days}+ days"
          + (f" (oldest {oldest}d)" if oldest is not None else "")
          + " — a prompt was written but no résumé ever came back.")
    for s in stale[:5]:
        age = f"{s['age_days']}d" if s["age_days"] is not None else "age unknown (prompt gone)"
        print(f"        {s['job_id']:<52} {age}")
    if len(stale) > 5:
        print(f"        … {len(stale) - 5} more")
    print("      fix: .venv/bin/python tools/reconcile_tailor_state.py         # dry run\n"
          "           .venv/bin/python tools/reconcile_tailor_state.py --apply")
    return len(stale)


def main() -> None:
    cfg = cfgmod.load_config()
    # --tailor skips the board sweep (one HTTP call + a 1s sleep per company): the tailor-queue
    # check is pure SQLite + stat(), so it can be run any time without touching the network.
    if "--tailor" in sys.argv[1:]:
        sys.exit(1 if check_tailor_queue(cfg) else 0)
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

    parked = check_tailor_queue(cfg)

    sys.exit(1 if (failures or parked) else 0)


if __name__ == "__main__":
    main()
