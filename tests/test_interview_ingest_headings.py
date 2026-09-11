"""The heading grammar and the anti-silence channel.

Every heading string below is copied verbatim out of 面试复盘/ (scanned 2026-08-10) — the two
malformed ones are the actual sections that had been dropped whole for six weeks, and the
"must not be flagged" ones are the real non-interview headings that a sloppier warning would
have buried the real ones under.
"""
from app import interview_ingest as iv
from app import store

# ---------------------------------------------------------------- the three shapes she writes

def test_canonical_shape_still_wins():
    assert iv.parse_heading("## Amazon · 2026-07-15 · Onsite Round 3 (HM) — BQ + System Design") \
        == ("Amazon", "2026-07-15", "Onsite Round 3 (HM) — BQ + System Design")
    assert iv.parse_heading("## WOP.com · 2026-06 · Recruiter Screen (Payments Engineer)") \
        == ("WOP.com", "2026-06", "Recruiter Screen (Payments Engineer)")
    assert iv.parse_heading("## Amazon + Convoke · 2026-06") == ("Amazon + Convoke", "2026-06", "")


def test_round_glued_onto_the_date_with_a_paren():
    """「## Amazon · 2026-06-23 (Technical Interview: …)」 — 6 action items lived here."""
    assert iv.parse_heading("## Amazon · 2026-06-23 (Technical Interview: Coding + Design + BQ)") \
        == ("Amazon", "2026-06-23", "Technical Interview: Coding + Design + BQ")


def test_date_written_last():
    """「## Anytime AI（疑似）· System Design Round … · 2026-06」 — 8 action items lived here."""
    assert iv.parse_heading("## Anytime AI（疑似）· System Design Round (RAG over 100K pages) · 2026-06") \
        == ("Anytime AI（疑似）", "2026-06", "System Design Round (RAG over 100K pages)")


def test_non_interview_headings_are_not_interviews():
    for line in ("## 答案库 / Answer Scripts（跨公司反复出现的问题，按公司类型给出可练版本）",
                 "## 第一部分 · Coding：二分查找（Binary Search）",      # · but no year
                 "## 二、教练复盘（coaching session，2026-07-20 整理）",   # year but no ·
                 "### Amazon · 2026-06-23 · Round 1",                    # ### is not a section
                 "## Amazon"):
        assert iv.parse_heading(line) is None, line


# ---------------------------------------------------------------- the anti-silence channel

def test_unparseable_interview_heading_is_reported_not_swallowed():
    md = ("## Amazon · 2026-06-23 · Real Round\n\n- [ ] kept\n\n"
          "## Amazon · 2026 06 23 · Broken Round\n\n- [ ] this one is invisible to the parser\n")
    skipped: list[dict] = []
    secs = iv._sections(md, "Interview Action Items.md", "action", skipped)
    assert [s["company"] for s in secs] == ["Amazon"]
    assert len(skipped) == 1
    assert skipped[0]["file"] == "Interview Action Items.md" and skipped[0]["line"] == 5
    assert "Broken Round" in skipped[0]["heading"]


def test_a_section_with_no_readable_items_is_reported():
    """The Lucid/CipherData failure mode: heading fine, content real, but it's a 行动清单
    table — checkbox-only parsing would have dropped 34 items in silence."""
    md = ("## Lucid Motors · 2026-07-30 · HM 行为轮\n\n"
          "| # | 动作 | 类型 | 优先级 |\n|---|---|---|---|\n| 1 | 建 Story→题型映射表 | 素材工程 | 最高 |\n")
    skipped: list[dict] = []
    secs = iv._sections(md, "Interview Action Items.md", "action", skipped)
    assert len(secs) == 1 and secs[0]["items"] == []
    assert len(skipped) == 1 and skipped[0]["line"] == 1 and "0 个" not in skipped[0]["why"]
    assert "action" in skipped[0]["why"]


def test_sections_stay_quiet_when_no_sink_is_passed():
    md = "## Amazon · 2026 06 23 · Broken\n\n- [ ] x\n"
    assert iv._sections(md, "f.md", "action") == []          # 3-arg call site still works


# ---------------------------------------------------------------- end to end

MD = """# Interview Action Items

## 答案库 / Answer Scripts（跨公司反复出现的问题）

- [ ] must not be parsed

## Amazon · 2026-06-23 (Technical Interview: Coding + Design + BQ)

- [ ] 补 Kahn 算法 = BFS 的口径
- [x] 已做过的一条

## Anytime AI（疑似）· System Design Round (RAG over 100K pages) · 2026-06

- [ ] RAG 高负载与容错补一遍
"""


def test_previously_dropped_sections_reach_the_db(cfg, tmp_path):
    (tmp_path / "Interview Action Items.md").write_text(MD, encoding="utf-8")
    cfg = dict(cfg)
    cfg["interviews"] = {"dir": str(tmp_path),
                         "files": {"action": "Interview Action Items.md"}}
    conn = store.connect_view(cfg)
    res = iv.ingest(cfg, conn)
    assert res["interviews"] == 2 and res["skipped"] == []
    got = {r["marker_id"]: r["n"] for r in conn.execute(
        "SELECT marker_id, COUNT(*) n FROM interview_items GROUP BY marker_id")}
    assert got == {"2026-06-23-amazon": 2, "2026-06-anytime-ai": 1}
    row = conn.execute("SELECT round_type FROM interviews WHERE marker_id='2026-06-23-amazon'").fetchone()
    assert row["round_type"] == "Technical Interview: Coding + Design + BQ"
    assert conn.execute("SELECT done FROM interview_items WHERE text='已做过的一条'").fetchone()["done"] == 1


def test_ingest_surfaces_skips_and_main_exits_nonzero(cfg, tmp_path, capsys):
    (tmp_path / "Interview Action Items.md").write_text(
        "## Amazon · 2026 06 23 · Broken Round\n\n- [ ] invisible\n", encoding="utf-8")
    cfg = dict(cfg)
    cfg["interviews"] = {"dir": str(tmp_path),
                         "files": {"action": "Interview Action Items.md"}}
    conn = store.connect_view(cfg)
    res = iv.ingest(cfg, conn)
    assert res["interviews"] == 0 and len(res["skipped"]) == 1
    report = iv._skipped_report(res["skipped"])
    assert "Broken Round" in report and "Interview Action Items.md:1" in report
