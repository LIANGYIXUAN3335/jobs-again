"""Delete roles that have VANISHED from a still-being-scanned board — the "click the link
and the job is gone" class. Signal: the daily scanner refreshes last_seen for every role
still listed, so a role whose last_seen date is older than its company's NEWEST last_seen
was delisted by the company. No network needed.

Scope guards:
  - BOARD-scanned roles only. cap:/manual: have no scan coverage, and neither does li: —
    a LinkedIn role's last_seen is refreshed by a keyword SEARCH, not by an enumeration of
    an employer's board, so "did not come back in today's search" says nothing about whether
    the posting is still up. Including li: here was wrong and expensive: on 2026-08-10 it
    accounted for 344 of the 872 rows this tool wanted to delete (248 of the 425 inside the
    review queue). A live HTTP probe of a 40-role sample flagged 11 rows of which only 2 were
    actually dead (18% precision); 6 of the 9 survivors it wanted to delete were li: rows
    still serving a live posting. Those belong to check_dead_links.py, which fetches them.
  - a company must have been rescanned since the role's last_seen (companies whose whole
    board went stale — commented out of config, fetch errors — prove nothing);
  - role_state.status must be new/absent: applied/interviewing/skip rows keep their history.

Even so the signal is soft: a board scan that is keyword-filtered or paginated drops live
roles out of a day's results, and this tool cannot tell that from a delisting. Read the
--dry list before applying; it is a suggestion, not a verdict.

Backs up everything it deletes (roles row + role_state row) to data/purged_delisted_backup.json
(appends per run, keyed by run timestamp).

    python tools/prune_delisted.py          # prune
    python tools/prune_delisted.py --dry    # just show what would go
"""
import json
import sys

sys.path.insert(0, ".")
from app import config as cfgmod, store  # noqa: E402

BACKUP = "data/purged_delisted_backup.json"


def main(dry: bool):
    cfg = cfgmod.load_config()
    conn = store.connect_view(cfg)      # attaches jobs + user_state

    # Sources whose last_seen is NOT refreshed by enumerating an employer's board, so a
    # lagging last_seen carries no delisting signal for them. li: belongs here for the same
    # reason cap:/manual: do — see the module docstring.
    NOT_BOARD = " AND ".join(f"{{col}} NOT LIKE '{p}:%'" for p in ("cap", "manual", "li"))

    # The per-company baseline has to come from the SAME population being judged: mixing a
    # li: row's search-driven last_seen into the max makes every board-scanned role at that
    # company look one scan stale.
    comax = {r["company_norm"]: r["m"] for r in conn.execute(
        "SELECT company_norm, max(substr(last_seen,1,10)) m FROM jobs.roles"
        f" WHERE {NOT_BOARD.format(col='job_id')}"
        " GROUP BY company_norm")}

    rows = conn.execute(
        "SELECT r.*, s.status AS own_status, s.notes AS s_notes,"
        "       s.tailored_resume_path, s.prompt_path, s.tailor_status"
        " FROM jobs.roles r LEFT JOIN role_state s ON s.job_id = r.job_id"
        f" WHERE {NOT_BOARD.format(col='r.job_id')}").fetchall()

    gone = []
    for r in rows:
        seen = (r["last_seen"] or "")[:10]
        newest = comax.get(r["company_norm"]) or ""
        if seen and newest and seen < newest and (r["own_status"] or "new") == "new":
            gone.append(dict(r))

    print(f"{'would delete' if dry else 'deleting'} {len(gone)} delisted roles:")
    for g in gone:
        print(f"  {g['company']:16} {(g['title'] or '')[:48]:50} last_seen {(g['last_seen'] or '')[:10]}")
    if dry or not gone:
        return

    path = cfgmod.resolve_path(BACKUP)
    try:
        book = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        book = {}
    book[store.la_now_iso()] = gone
    path.write_text(json.dumps(book, indent=1, ensure_ascii=False, default=str),
                    encoding="utf-8")

    for g in gone:
        conn.execute("DELETE FROM jobs.roles WHERE job_id=?", (g["job_id"],))
        conn.execute("DELETE FROM role_state WHERE job_id=?", (g["job_id"],))
    conn.commit()
    print(f"deleted {len(gone)}; backup appended to {BACKUP}")


if __name__ == "__main__":
    # Deliberately strict: this deletes rows, and the old `main("--dry" in sys.argv)` meant
    # ANY unrecognised flag ran the real thing — `--help` deleted 293 roles on 2026-07-30.
    # Unknown args now abort, and the destructive path needs an explicit --apply.
    args = set(sys.argv[1:])
    known = {"--dry", "--apply", "-h", "--help"}
    if args - known:
        sys.exit(f"unknown option(s): {' '.join(sorted(args - known))}\n"
                 "usage: prune_delisted.py [--dry | --apply]")
    if not args or args & {"-h", "--help"}:
        sys.exit(__doc__)
    if "--dry" in args:
        main(True)
    elif "--apply" in args:
        main(False)
