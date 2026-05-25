"""Remove roles already in jobscan.db that require a US security clearance / citizenship /
ITAR US-person status — dead-ends on AOS/EAD. The clearance filter blocks NEW ones at scan
time; this cleans out ones scanned before the filter existed. Backs up what it removes.
    python tools/purge_clearance.py          # purge
    python tools/purge_clearance.py --dry    # just show what would go
"""
import json
import sys
from collections import Counter

sys.path.insert(0, ".")
from app import config as cfgmod, store  # noqa: E402
from app.filters import _first_hit  # noqa: E402


def main(dry: bool):
    cfg = cfgmod.load_config()
    terms = (cfg.get("filters") or {}).get("clearance_exclude", [])
    conn = store.connect_jobs(cfg)
    rows = conn.execute("SELECT job_id, company, title, jd_text FROM roles").fetchall()
    hits = []
    for r in rows:
        hay = f"{(r['title'] or '').lower()} {(r['jd_text'] or '').lower()}"
        t = _first_hit(terms, hay)
        if t:
            hits.append((r["job_id"], r["company"], r["title"], t))

    print(f"{'would purge' if dry else 'purging'} {len(hits)} clearance/citizenship roles:")
    for co, n in Counter(c for _, c, _, _ in hits).most_common(20):
        print(f"  {co:18} {n}")

    if dry or not hits:
        if dry:
            print("\n(dry run — nothing deleted)")
        return
    backup = [{"job_id": j, "company": c, "title": ti, "term": t} for j, c, ti, t in hits]
    cfgmod.resolve_path("data/purged_clearance_backup.json").write_text(
        json.dumps(backup, indent=1, ensure_ascii=False), encoding="utf-8")
    for j, _, _, _ in hits:
        conn.execute("DELETE FROM roles WHERE job_id=?", (j,))
    conn.commit()
    print(f"\ndeleted {len(hits)} rows (backup → data/purged_clearance_backup.json)")


if __name__ == "__main__":
    main("--dry" in sys.argv)
