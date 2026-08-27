"""跟进提醒：名单里该有谁（和不该有谁），以及什么时候值得打断她。

2026-08-10 补的这一层，起因是一个数字：followups 表 0 行。算名单的代码一直是对的，
但没有任何东西把结果送到她面前，所以功能等于不存在。这里测两件事 ——
名单本身的正确性（重复投递、已经有回音的、同一家刷屏），和通知的克制（同一份名单
不许天天弹）。加上一条合同测试：这条路径永远不许发信。
"""
import datetime
import json

from app import config as cfgmod, followup, store
from tests.test_store import role_row
from tools import followup_nudge as fn


def _seed(cfg, *, job_id="gh:x:1", company="X", score=80.0, days_ago=30, conn=None, **state):
    conn = conn or store.connect_view(cfg)
    conn.execute("INSERT OR REPLACE INTO jobs.roles (" + ", ".join(store.ROLE_COLS) + ") VALUES ("
                 + ", ".join(f":{c}" for c in store.ROLE_COLS) + ")",
                 role_row(job_id=job_id, company=company, score=score))
    conn.commit()
    when = (datetime.date.fromisoformat(store.la_today())
            - datetime.timedelta(days=days_ago)).isoformat()
    store.upsert_state(conn, job_id, status="applied", applied_date=when, **state)
    return conn


def _row(job_id="gh:x:1", company="Nvidia", score=93.0, days_quiet=21):
    return {"job_id": job_id, "company": company, "weighted_score": score,
            "days_quiet": days_quiet}


def _boom(*a, **k):
    raise AssertionError("this path must never run from the scheduled nudge")


# ---------------------------------------------------------------- 名单里不该有谁

def test_a_linked_pair_is_one_application_not_two(cfg):
    """li: 影子行 + ATS 正主是同一个岗位（role_state.duplicate_of）。两行都进榜，
    她就会为同一份申请发两封跟进 —— 2026-08-10 的线上名单里 Otter 和 Lightning AI
    正是这样各出现了两次。funnel.load 早就只数正主，这里跟上。"""
    conn = _seed(cfg, job_id="gh:otter:1", company="Otter")
    _seed(cfg, job_id="li:99", company="Otter", conn=conn, duplicate_of="gh:otter:1")
    assert [r["job_id"] for r in followup.quiet(cfg, conn)] == ["gh:otter:1"]


def test_a_shadow_whose_canonical_never_went_out_still_counts(cfg):
    """链接指向一行从没投过的岗位（prune 之后的悬空链接）。那影子行就是唯一的申请，
    照样要跟进 —— 排重不能把真实申请排没了。"""
    conn = _seed(cfg, job_id="li:99", company="Otter", duplicate_of="gh:otter:gone")
    assert [r["job_id"] for r in followup.quiet(cfg, conn)] == ["li:99"]


def test_an_advanced_stage_is_never_chased_even_without_first_response_at(cfg):
    """first_response_at 是主判据，但 stage 是同一件事的另一半。手工改过、迁移过来的行
    可能只有 stage 前进了 —— 那也是有回音，不许再催。"""
    conn = _seed(cfg, job_id="gh:x:1")
    store.upsert_state(conn, "gh:x:1", stage="screen")      # 绕开 record_signal，故意不写 first_response_at
    assert conn.execute("SELECT first_response_at FROM role_state WHERE job_id='gh:x:1'"
                        ).fetchone()["first_response_at"] is None
    assert followup.quiet(cfg, conn) == []


def test_one_company_cannot_take_over_the_whole_list(cfg):
    """resolve_contact 是按公司找人的，不是按岗位 —— 同一家十个岗位进榜，就是发给
    同一个 recruiter 十封信。这正是本模块存在的意义所反对的。"""
    cfg = {**cfg, "followup": {**followup.DEFAULTS, "per_company": 3}}
    conn = None
    for i in range(6):
        conn = _seed(cfg, job_id=f"wd:nvidia:{i}", company="Nvidia", score=90.0 - i, conn=conn)
    for i in range(2):
        conn = _seed(cfg, job_id=f"gh:otter:{i}", company="Otter", score=70.0 - i, conn=conn)
    rows = followup.quiet(cfg, conn)
    assert [r["company"] for r in rows] == ["Nvidia"] * 3 + ["Otter"] * 2
    assert [r["weighted_score"] for r in rows] == [90.0, 89.0, 88.0, 70.0, 69.0]   # 每家留最高分的


def test_the_per_company_cap_can_be_switched_off(cfg):
    cfg = {**cfg, "followup": {**followup.DEFAULTS, "per_company": 0}}
    conn = None
    for i in range(5):
        conn = _seed(cfg, job_id=f"wd:nvidia:{i}", company="Nvidia", score=90.0 - i, conn=conn)
    assert len(followup.quiet(cfg, conn)) == 5


def test_recruiting_role_mailboxes_are_not_a_contact():
    """2026-08-10 线上跑出来的唯一「联系人」是 sample-d502a631@example.com —— 一个无人值守
    的推荐机器人。找不到人（None）会把她导向 portal，是对的；找到一个假人比找不到更糟。"""
    for addr in ["sample-d502a631@example.com", "sample-7cc7c2e4@example.com", "sample-8d7c9e33@example.com",
                 "sample-7b288904@example.com", "sample-ac27371e@example.com", "sample-ccc72a94@example.com",
                 "sample-bc9bde27@example.com", "sample-a70294a6@example.com", "sample-596db959@example.com"]:
        assert followup._NOT_A_PERSON.search(addr), addr
    # 团队别名上的真人照样是真人 —— 整段 local part 才算数，不是前缀。
    for addr in ["sample-09b4ccdf@example.com", "sample-2ce80dc1@example.com", "sample-bb896da6@example.com",
                 "sample-ee7fe3ae@example.com", "sample-29c84fb6@example.com"]:
        assert not followup._NOT_A_PERSON.search(addr), addr


# ---------------------------------------------------------------- 什么时候值得打断她

def test_the_headline_names_distinct_companies():
    """三个名额全被 Nvidia 占掉的通知只剩一条信息。"""
    rows = [_row("a", "Nvidia", 93), _row("b", "Nvidia", 90), _row("c", "Otter", 89),
            _row("d", "Mastercard", 88)]
    line = fn.headline(rows, n=3)
    assert line.count("Nvidia") == 1
    assert "Otter" in line and "Mastercard" in line


def test_the_headline_is_specific_not_a_todo_count():
    line = fn.headline([_row("a", "Nvidia", 93, 21)], n=3)
    assert "Nvidia" in line and "93" in line and "21" in line


def test_a_new_face_is_worth_a_notification():
    rows = [_row("a"), _row("b")]
    go, why = fn.should_notify([_row("b")], rows, {"last_notified": "2026-08-09"}, "2026-08-10")
    assert go and "新面孔" in why


def test_the_same_list_does_not_get_notified_twice(cfg):
    """名单没动就闭嘴。天天弹同一份名单，三天就被学会忽略了。"""
    rows = [_row("a"), _row("b")]
    go, why = fn.should_notify([], rows, {"last_notified": "2026-08-09"}, "2026-08-10")
    assert not go and "不吵她" in why


def test_an_ignored_list_gets_one_gentle_reminder_a_fortnight():
    rows = [_row("a")]
    state = {"last_notified": "2026-07-20"}          # 21 天前
    go, why = fn.should_notify([], rows, state, "2026-08-10")
    assert go and "没变" in why


def test_an_empty_list_is_never_a_notification():
    go, why = fn.should_notify([], [], {}, "2026-08-10")
    assert not go


def test_a_missing_or_corrupt_state_file_is_not_an_accident(tmp_path):
    """状态文件丢了/被写坏了，最多多叫一次，不许炸掉整个定时任务。"""
    assert fn.load_state(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert fn.load_state(bad) == {}


def test_state_round_trips(tmp_path):
    p = tmp_path / "s.json"
    fn.save_state(p, ["b", "a"], "2026-08-10")
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d == {"seen": ["a", "b"], "last_notified": "2026-08-10"}
    assert fn.load_state(p) == d
    assert not list(tmp_path.glob("*.tmp"))          # 原子替换不留残骸


# ---------------------------------------------------------------- 设计合同

def test_the_nudge_never_drafts_and_never_sends(cfg, tmp_path, monkeypatch):
    """app/followup.py 的合同：跟进信必须是人按下发送。定时器这一侧只许数数和点名 ——
    既不烧 LLM 的钱起草，更不碰任何发送路径。"""
    conn = _seed(cfg, job_id="gh:x:1", company="Otter", score=88.0)
    conn.close()
    calls = []
    monkeypatch.setattr(fn, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(fn, "notify", lambda t, m: calls.append((t, m)))
    monkeypatch.setattr(fn.cfgmod, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(fn.followup, "draft", _boom)
    monkeypatch.setattr(fn.followup, "log", _boom)
    monkeypatch.setattr(fn.followup, "resolve_contact", _boom)   # 连邮箱都不该碰

    assert fn.main([]) == 0

    assert len(calls) == 1
    title, msg = calls[0]
    assert "跟进" in title
    assert "Otter" in msg and "88" in msg          # 点名到公司和分数，不是「你有待办」
    assert "/followups" in msg                     # 通知里必须带着能点开的地方
    conn = store.connect_view(cfg)
    try:
        assert store.followups_for(conn, "gh:x:1") == []     # 什么都没记，什么都没发
    finally:
        conn.close()
    # 这条路径上根本没有发送能力：模块里不存在任何 mail/send 的名字。
    assert not [n for n in vars(fn) if "mail" in n.lower() or "send" in n.lower()]


def test_a_second_run_on_an_unchanged_list_stays_quiet(cfg, tmp_path, monkeypatch):
    conn = _seed(cfg, job_id="gh:x:1", company="Otter")
    conn.close()
    calls = []
    monkeypatch.setattr(fn, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(fn, "notify", lambda t, m: calls.append((t, m)))
    monkeypatch.setattr(fn.cfgmod, "load_config", lambda *a, **k: cfg)
    fn.main([])
    fn.main([])
    assert len(calls) == 1                         # 第二次名单没变 —— 不吵


def test_dry_run_writes_nothing_and_says_nothing(cfg, tmp_path, monkeypatch):
    conn = _seed(cfg, job_id="gh:x:1", company="Otter")
    conn.close()
    state = tmp_path / "state.json"
    calls = []
    monkeypatch.setattr(fn, "STATE", str(state))
    monkeypatch.setattr(fn, "notify", lambda t, m: calls.append((t, m)))
    monkeypatch.setattr(fn.cfgmod, "load_config", lambda *a, **k: cfg)
    assert fn.main(["--dry-run"]) == 0
    assert calls == [] and not state.exists()


def test_the_notification_url_follows_the_configured_ui_port(cfg):
    assert fn.followups_url({**cfg, "ui": {"host": "127.0.0.1", "port": 9999}}) \
        == "http://127.0.0.1:9999/followups"


def test_the_task_is_registered_for_the_health_page():
    """没注册的定时任务不会在 /health 上显示，失联了也没人知道 —— 这正是跟进功能
    之前的处境。"""
    from app import heartbeats
    assert "followup" in {n for n, _, _ in heartbeats.REGISTRY}
    assert any(r["name"] == "followup" for r in heartbeats.read())
