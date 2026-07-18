"""复盘 → pipeline indexing: the parser against the real file conventions, matching rules,
and idempotency (a hand-set link must survive re-ingest)."""
import json

from app import interview_ingest as iv
from app import store
from tests.test_store import role_row

MD = """# Interview Action Items / 面试行动项

---

## 答案库 / Answer Scripts（跨公司反复出现的问题）

- [ ] this line must NOT be parsed (non-interview section)

## Amazon · 2026-07-14 · Onsite Round 1 — BQ + OOD
<!-- atoms-source id="2026-07-14-amazon" added="2026-07-14" status="new" -->

### 高优先级 / High Priority

- [ ] Practice conflict story out loud / 大声练冲突故事
- [x] Re-read OOD notes / 重读 OOD 笔记

## WOP.com · 2026-06 · Recruiter Screen (Payments Engineer)

- [ ] Send thank-you note
"""

TECH = """# Technical Interview Questions

---

## Amazon · 2026-07-14 · Onsite Round 1 — BQ + OOD
<!-- atoms-source id="2026-07-14-amazon" added="2026-07-14" status="new" -->

### Q: Design a parking lot (OOD)
### 问：设计停车场

**Key points for a strong answer:**
- ...

### Q: How do you handle concurrent updates?
"""


def test_sections_parse_real_conventions(tmp_path):
    s = iv._sections(MD, "Interview Action Items.md", "action")
    # 答案库 (no date field) filtered; both dated sections found
    assert [x["company"] for x in s] == ["Amazon", "WOP.com"]
    amazon, wop = s
    assert amazon["marker"] == "2026-07-14-amazon"          # explicit marker wins
    assert wop["marker"] == "2026-06-wop-com"               # derived when absent (old entries)
    assert [(i["text"], i["done"]) for i in amazon["items"]] == [
        ("Practice conflict story out loud / 大声练冲突故事", False),
        ("Re-read OOD notes / 重读 OOD 笔记", True)]
    t = iv._sections(TECH, "Technical_Interview_Questions.md", "tech_q")
    assert len(t) == 1 and len(t[0]["items"]) == 2          # 问： duplicate line not double-counted
    assert t[0]["items"][0]["text"] == "Design a parking lot (OOD)"


def _cfg_with_files(cfg, tmp_path):
    (tmp_path / "Interview Action Items.md").write_text(MD, encoding="utf-8")
    (tmp_path / "Technical_Interview_Questions.md").write_text(TECH, encoding="utf-8")
    c = dict(cfg)
    c["interviews"] = {"dir": str(tmp_path), "files": {
        "action": "Interview Action Items.md", "tech_q": "Technical_Interview_Questions.md"}}
    return c


def test_ingest_links_unambiguous_and_advances_stage(cfg, tmp_path):
    cfg = _cfg_with_files(cfg, tmp_path)
    conn = store.connect_view(cfg)
    conn.execute("INSERT INTO jobs.roles (" + ", ".join(store.ROLE_COLS) + ") VALUES ("
                 + ", ".join(f":{c}" for c in store.ROLE_COLS) + ")",
                 role_row(job_id="manual:amazon:sde", company="Amazon", title="SDE II"))
    store.upsert_state(conn, "manual:amazon:sde", status="applied", applied_date="2026-07-01")
    conn.commit()
    res = iv.ingest(cfg, conn)
    assert res == {"interviews": 2, "linked": 1, "ambiguous": 0, "standalone": 1,
                   "with_transcript": 0}
    row = conn.execute("SELECT * FROM interviews WHERE marker_id='2026-07-14-amazon'").fetchone()
    assert row["job_id"] == "manual:amazon:sde"
    st = conn.execute("SELECT status, stage FROM role_state WHERE job_id='manual:amazon:sde'").fetchone()
    assert st["stage"] == "interview" and st["status"] == "interviewing"   # 复盘存在 ⇒ 面试发生了
    assert json.loads(row["sources"]).keys() == {"Interview Action Items.md",
                                                 "Technical_Interview_Questions.md"}


def test_reingest_is_idempotent_and_keeps_manual_links(cfg, tmp_path):
    cfg = _cfg_with_files(cfg, tmp_path)
    conn = store.connect_view(cfg)
    iv.ingest(cfg, conn)
    # hand-link the standalone one to some role later added
    conn.execute("INSERT INTO jobs.roles (" + ", ".join(store.ROLE_COLS) + ") VALUES ("
                 + ", ".join(f":{c}" for c in store.ROLE_COLS) + ")",
                 role_row(job_id="cap:wop123", company="WOP.com", title="Payments Engineer"))
    conn.commit()
    store.interview_link(conn, "2026-06-wop-com", "cap:wop123")
    iv.ingest(cfg, conn)                                     # re-ingest must not clear it
    row = conn.execute("SELECT job_id FROM interviews WHERE marker_id='2026-06-wop-com'").fetchone()
    assert row["job_id"] == "cap:wop123"
    n = conn.execute("SELECT COUNT(*) n FROM interview_items WHERE marker_id='2026-07-14-amazon'").fetchone()["n"]
    assert n == 4                                            # replaced, not duplicated


README = """# 面试复盘 — 按公司归档

- 跨公司汇总: [Interview Action Items](<Interview Action Items.md>)

### Amazon

- [31. Amazon - Technical Phone Screen](Amazon/02_Technical_Phone_Screen_live_coding.md) (140 transcript rows) - [Otter](https://otter.ai/u/DgeB)
- [Amazon Onsite Round 1 — BQ + OOD Checkout Price Engine](Amazon/Onsite_2026-07/Round1_BQ_OOD.md)

### WOP.com

- [02. WOP.com - Recruiter Screen](WOP/02_Recruiter_Screen.md) - [Otter](https://otter.ai/u/WOPX)
"""


def test_readme_transcript_links_attach_to_the_right_round(cfg, tmp_path):
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    from pathlib import Path as P
    entries = iv._readme_entries(P(tmp_path))
    assert set(entries) == {"amazon", "wopcom"}
    # round-label token overlap picks the right line, and ≥2 tokens are required
    e = iv._best_entry(entries["amazon"], "Onsite Round 1 — BQ + OOD")
    assert e and e["path"].endswith("Round1_BQ_OOD.md") and e["otter"] is None
    e2 = iv._best_entry(entries["amazon"], "Technical Phone Screen")
    assert e2 and e2["otter"] == "https://otter.ai/u/DgeB"
    assert iv._best_entry(entries["amazon"], "HR chat") is None   # one weak shared token ≠ a match


def test_ingest_carries_transcripts_and_keeps_them_on_reingest(cfg, tmp_path):
    cfg = _cfg_with_files(cfg, tmp_path)
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    conn = store.connect_view(cfg)
    res = iv.ingest(cfg, conn)
    assert res["with_transcript"] == 2                       # amazon round-1 file + wop otter
    row = conn.execute("SELECT * FROM interviews WHERE marker_id='2026-07-14-amazon'").fetchone()
    assert row["review_path"].endswith("Round1_BQ_OOD.md")
    # README later edited to drop the line → re-ingest must KEEP the stored link (COALESCE)
    (tmp_path / "README.md").write_text("# empty\n", encoding="utf-8")
    iv.ingest(cfg, conn)
    row = conn.execute("SELECT review_path FROM interviews WHERE marker_id='2026-07-14-amazon'").fetchone()
    assert row["review_path"].endswith("Round1_BQ_OOD.md")
