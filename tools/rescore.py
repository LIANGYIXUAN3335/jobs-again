"""Rescore every stored role in place using the current weights + company_notes,
WITHOUT re-fetching from any ATS (uses the jd_text already in the DB). Run after
editing config weights / scoring keywords / company_notes.yaml:
    python tools/rescore.py
"""
import sys
from types import SimpleNamespace

sys.path.insert(0, ".")
from app import config as cfgmod, scoring, store  # noqa: E402


def main():
    cfg = cfgmod.load_config()
    notes_all = cfgmod.load_company_notes(cfg)
    conn = store.connect_jobs(cfg)
    rows = conn.execute(
        "SELECT job_id, company, title, jd_text, is_remote, location FROM roles").fetchall()
    n = 0
    changed = 0
    for r in rows:
        p = SimpleNamespace(
            title=r["title"] or "", jd_text=r["jd_text"] or "",
            is_remote=bool(r["is_remote"]), company=r["company"] or "",
            location=r["location"] or "")
        notes = scoring.notes_for(r["company"], notes_all)
        s = scoring.score(p, notes, cfg)
        old = conn.execute("SELECT weighted_score FROM roles WHERE job_id=?", (r["job_id"],)).fetchone()
        if old and old["weighted_score"] != s.weighted:
            changed += 1
        conn.execute(
            "UPDATE roles SET s_stability=?, s_culture=?, s_depth=?, s_wlb=?, "
            "weighted_score=?, verdict=?, why=?, caution=? WHERE job_id=?",
            (s.s_stability, s.s_culture, s.s_depth, s.s_wlb, s.weighted,
             s.verdict, s.why, s.caution, r["job_id"]))
        n += 1
    store.meta_set(conn, "scoring_config_hash", scoring.config_hash(cfg))
    conn.commit()
    print(f"rescored {n} roles ({changed} changed score)")


if __name__ == "__main__":
    main()
