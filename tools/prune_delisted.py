"""Delete roles that have VANISHED from a still-being-scanned board — the "click the link
and the job is gone" class. Signal: the daily scanner refreshes last_seen for every role
still listed, so a role whose last_seen date is older than its company's NEWEST last_seen
was delisted by the company. No network needed.

Scope guards:
  - scanner-sourced roles only (cap:/manual: have no scan coverage — verify those online);
  - a company must have been rescanned since the role's last_seen (companies whose whole
    board went stale — commented out of config, fetch errors — prove nothing);
  - role_state.status must be new/absent: applied/interviewing/skip rows keep their history.

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

    comax = {r["company_norm"]: r["m"] for r in conn.execute(
        "SELECT company_norm, max(substr(last_seen,1,10)) m FROM jobs.roles"
        " WHERE job_id NOT LIKE 'cap:%' AND job_id NOT LIKE 'manual:%'"
        " GROUP BY company_norm")}

    rows = conn.execute(
        "SELECT r.*, s.status AS own_status, s.notes AS s_notes,"
        "       s.tailored_resume_path, s.prompt_path, s.tailor_status"
        " FROM jobs.roles r LEFT JOIN role_state s ON s.job_id = r.job_id"
        " WHERE r.job_id NOT LIKE 'cap:%' AND r.job_id NOT LIKE 'manual:%'").fetchall()

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
    main("--dry" in sys.argv)
