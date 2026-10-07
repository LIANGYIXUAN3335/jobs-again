"""鸭先知 — 市场趋势感知（owner 2026-09-09 三期拍板）。

数据来源是每天全量扫描攒下的 JD 语料（jobscan.db roles，只读）。四个信号：
  1. 主题份额与势头 — config trends.themes 词典按正则数岗位，新增岗位里份额环比；
  2. 挂龄 — posted_at/first_seen 到今天的天数。挂得越久 = 越招不到人 = 供不应求；
  3. 新词雷达 — 最近 14 天新岗的 title+JD 里冒出来的、老语料里没见过的词；
  4. 公司发岗速度 — 最近 7 天发岗数 vs 此前 8 周周均，突增 = 刚拿钱/新方向。

HARD RULE 同 scan/score：本模块零 LLM、零联网 — 纯本地统计。联网验证是周报
shell（tools/weekly_trend.sh）里那个 headless opus 的事，跟这里隔离。

CLI：
  python -m app.trends --snapshot    每日快照 → data/trends/YYYY-MM-DD.json（daily_scan 链尾）
  python -m app.trends --report      周报 markdown → data/trends/weekly/YYYY-MM-DD.md，打印路径
  python -m app.trends --mail PATH   把报告文件寄给 trends.report_to（发给 owner 自己）
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import re
import sqlite3
import statistics
import sys
from pathlib import Path

from . import config as cfgmod

TRENDS_DIR = Path("data/trends")

# 新词雷达的停用词：普通英文 + 招聘话术里到处都是的词。宁可漏掉不常见的好词，
# 也不要每周都报 "benefits" "collaborative" 这种噪音。
_STOP = set("""the and for with you our are will this that have from work team your job role
about who what all can our their they them able than more most other some such may per not
но we of in on to at as is be or an it by if do us into across within using use used skills
experience years engineer engineering software senior staff developer development company
benefits equity salary range remote hybrid onsite office location applicants employment
qualifications requirements responsibilities preferred plus bonus competitive candidates
build building design designing develop developing maintain scalable strong knowledge
understanding excellent communication degree computer science related field must nice
looking join help drive impact fast paced dynamic growing please apply equal opportunity
gender race religion veteran disability status accommodation applicable law privacy
including like new one two three grow make take own well highly working closely partner
schedules preference periods professionally seasoned responsibly commitments diploma
affordable entrepreneurs onsite-first generalist generalists
teams product products platform platforms systems system service services customers users
data月 comp pay insurance dental vision pto holidays perks culture mission值""".split())
_TOKEN_RE = re.compile(r"[a-z][a-z0-9+#-]{2,29}|[a-z]?\d{3,4}[a-z]\d*")  # 含 h100/gb200；不吃点号（域名=噪音）


def _themes(cfg: dict) -> dict[str, re.Pattern]:
    return {name: re.compile(rx, re.IGNORECASE | re.DOTALL)
            for name, rx in ((cfg.get("trends") or {}).get("themes") or {}).items()}


def _skills(cfg: dict) -> dict[str, re.Pattern]:
    """技能白名单词典（owner 2026-09-13）：落地层技能榜唯一来源，只放可学的具体东西。"""
    return {name: re.compile(rx, re.IGNORECASE)
            for name, rx in ((cfg.get("trends") or {}).get("skills") or {}).items()}


def _thr(cfg: dict, key: str, default):
    return ((cfg.get("trends") or {}).get("thresholds") or {}).get(key, default)


def _stop_set(cfg: dict) -> set:
    return _STOP | set((cfg.get("trends") or {}).get("newword_stop") or [])


def _company_onboard_dates(rows) -> dict[str, str]:
    """每家公司最早的真 ATS 行 first_seen（li: 影子行不算——它们早于板子接入就存在）。
    用途：环比/新增/新词/周序列一律剔除接入 <21 天的公司，防止面板扩容伪装成市场信号
    （2026-09-10 评审头名实测：Energy 近 7 天 348 个「新增」里 302 个是扩容回填）。"""
    first: dict[str, str] = {}
    for r in rows:
        fs = (r["first_seen"] or "")[:10]
        co = r["company_norm"] or r["company"]
        if fs and not str(r["job_id"]).startswith("li:") and (co not in first or fs < first[co]):
            first[co] = fs
    return first


def _stable_panel(rows, today: datetime.date):
    d21 = (today - datetime.timedelta(days=21)).isoformat()
    first = _company_onboard_dates(rows)
    return [r for r in rows if first.get(r["company_norm"] or r["company"], "9999") < d21]


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT job_id, company, company_norm, title, jd_text, posted_at, first_seen,"
        "       last_seen, weighted_score, verdict FROM jobs.roles WHERE jd_text IS NOT NULL AND jd_text != ''"
    ).fetchall()


def _age_days(r, today: datetime.date) -> int | None:
    start = (r["posted_at"] or r["first_seen"] or "")[:10]
    try:
        return (today - datetime.date.fromisoformat(start)).days
    except ValueError:
        return None


def theme_stats(cfg: dict, rows, today: datetime.date) -> dict:
    """每主题：在库数/份额/挂龄中位数/近7·14天新增/新增份额环比（近14天 vs 前14天）。
    环比用 first_seen 分窗算「新岗位里的份额」，不用快照 —— 快照攒够历史前这是唯一
    可比的口径（roles 表会随下架清理，总量份额往回看不准）。"""
    d14 = (today - datetime.timedelta(days=14)).isoformat()
    d7 = (today - datetime.timedelta(days=7)).isoformat()
    d28 = (today - datetime.timedelta(days=28)).isoformat()
    stable = _stable_panel(rows, today)          # 面板恒定口径：环比/新增只看老公司
    recent = [r for r in stable if (r["first_seen"] or "") >= d14]
    prior = [r for r in stable if d28 <= (r["first_seen"] or "") < d14]
    stable_ids = {r["job_id"] for r in stable}
    out = {}
    d3 = (today - datetime.timedelta(days=3)).isoformat()
    for name, rx in _themes(cfg).items():
        hits = [r for r in rows if rx.search(r["jd_text"]) or rx.search(r["title"] or "")]
        # 在架岗位的挂龄有幸存者偏差：活得越久的(常是幽灵岗)越容易被抽到。两个修正
        # (2026-09-10, 源自 LinkUp/DFH 文献 + ShouldApply 阈值):
        #   ① >90 天仍在架的按幽灵岗(evergreen)剔除出挂龄统计, 单独报 ghost 占比;
        #   ② 首选口径改为「完结耗时」= 已下架岗位从发布到消失的天数(posted_at 才是
        #      真起点; first_seen 对老岗只是接入日, 会把耗时算短)。
        by_co = collections.Counter((r["company_norm"] or r["company"]) for r in hits)
        top3 = sum(n for _, n in by_co.most_common(3))
        top3_pct = round(100 * top3 / len(hits)) if hits else 0
        # 置信度：读数代表「市场」还是「少数几家公司」。owner 2026-09-10 质疑
        # "最缺人基本全取自 Nvidia" —— 实测 inference/AI-DC top3 占 42-43%，质疑成立。
        conf = ("high" if len(by_co) >= 30 and top3_pct <= 40
                else "low" if len(by_co) < 15 or top3_pct > 60 else "medium")
        live = [r for r in hits if (r["last_seen"] or "") >= d3]
        ghost_days = int(_thr(cfg, "ghost_days", 45))
        live_ages = [a for a in (_age_days(r, today) for r in live) if a is not None]
        fresh_ages = sorted(a for a in live_ages if a <= ghost_days)
        n_ghost = sum(1 for a in live_ages if a > ghost_days)
        closed = [r for r in hits if (r["last_seen"] or "") and r["last_seen"][:10] < d3
                  and r["posted_at"]]
        # 完结耗时按「公司平衡」算：先每家公司取中位、再跨公司取中位 —— 否则 Nvidia
        # 一家 290 个岗直接接管整个主题的读数（大板子刷权重）。门槛：每家 ≥3 例、
        # 合格公司 ≥5 家才报数。
        spells_by_co: dict[str, list] = collections.defaultdict(list)
        for r in closed:
            if r["posted_at"][:10] <= r["last_seen"][:10]:
                spells_by_co[r["company_norm"] or r["company"]].append(
                    (datetime.date.fromisoformat(r["last_seen"][:10])
                     - datetime.date.fromisoformat(r["posted_at"][:10])).days)
        co_medians = sorted(statistics.median(v) for v in spells_by_co.values() if len(v) >= 3)
        fill = co_medians if len(co_medians) >= 5 else []
        s_hits = [r for r in hits if r["job_id"] in stable_ids]
        n_recent = sum(1 for r in s_hits if (r["first_seen"] or "") >= d14)
        n_prior = sum(1 for r in s_hits if d28 <= (r["first_seen"] or "") < d14)
        share_recent = n_recent / len(recent) if recent else 0
        share_prior = n_prior / len(prior) if prior else 0
        out[name] = {
            "count": len(hits),
            "share_pct": round(100 * len(hits) / len(rows), 1) if rows else 0,
            "median_age": statistics.median(fresh_ages) if fresh_ages else None,
            "ghost_pct": round(100 * n_ghost / len(live_ages), 1) if live_ages else 0,
            "median_fill": statistics.median(fill) if fill else None,
            "n_closed": sum(len(v) for v in spells_by_co.values()),
            "n_closed_cos": len(co_medians),
            "companies": len(by_co),
            "top3_share_pct": top3_pct,
            "confidence": conf,
            "new7": sum(1 for r in s_hits if (r["first_seen"] or "") >= d7),
            "new14": n_recent,
            "new_share_pct": round(100 * share_recent, 1),
            "new_share_delta_pp": round(100 * (share_recent - share_prior), 1),
        }
    return out


def novel_terms(cfg: dict, rows, today: datetime.date, top: int = 20) -> list[dict]:
    """最近 14 天新岗位里冒出来、老语料里几乎没有的词。

    双口径：文档频次 + **公司数**。同一家公司的 JD 模板会在它每个岗位里重复
    （首版被 Applied Intuition 的 75 份 boilerplate 刷屏），所以一个词必须有
    ≥4 家不同公司的新岗提到才算风向，单一公司的口头禅不算。"""
    d14 = (today - datetime.timedelta(days=14)).isoformat()
    old_df: collections.Counter = collections.Counter()
    new_df: collections.Counter = collections.Counter()
    new_cos: dict[str, set] = collections.defaultdict(set)
    n_old = n_new = 0
    stop = _stop_set(cfg)
    stable_ids = {r["job_id"] for r in _stable_panel(rows, today)}
    for r in rows:
        if (r["first_seen"] or "") >= d14 and r["job_id"] not in stable_ids:
            continue                      # 新接入公司的存量回填不算「新岗」
        toks = set(_TOKEN_RE.findall(f"{r['title'] or ''} {r['jd_text']}".lower())) - stop
        if (r["first_seen"] or "") >= d14:
            new_df.update(toks); n_new += 1
            for t in toks:
                new_cos[t].add(r["company_norm"] or r["company"])
        else:
            old_df.update(toks); n_old += 1
    if not n_new or not n_old:
        return []
    scored = []
    for t, c in new_df.items():
        if c < 5 or len(new_cos[t]) < 4:   # ≥5 个新岗 且 ≥4 家公司，模板口头禅出局
            continue
        old_rate = old_df.get(t, 0) / n_old
        new_rate = c / n_new
        if new_rate < 3 * old_rate + 0.002:   # 至少 3 倍于旧频率才算"冒出来"
            continue
        scored.append({"term": t, "new_docs": c, "companies": len(new_cos[t]),
                       "old_docs": old_df.get(t, 0),
                       "ratio": round(new_rate / old_rate, 1) if old_rate else None})
    scored.sort(key=lambda x: (-x["companies"], -(x["ratio"] or 999), -x["new_docs"]))
    return scored[:top]


def company_velocity(rows, today: datetime.date, top: int = 12) -> list[dict]:
    """最近 7 天发岗数 vs 此前 8 周周均。新接入的公司首扫会整批 first_seen 同一天，
    天然是假突增 —— 用「首次出现日 < 21 天前」过滤掉刚接入的。"""
    d7 = (today - datetime.timedelta(days=7)).isoformat()
    d63 = (today - datetime.timedelta(days=63)).isoformat()
    d21 = (today - datetime.timedelta(days=21)).isoformat()
    first_by_co = _company_onboard_dates(rows)
    last7: collections.Counter = collections.Counter()
    prior8w: collections.Counter = collections.Counter()
    for r in rows:
        co = r["company_norm"] or r["company"]
        fs = (r["first_seen"] or "")[:10]
        if fs >= d7:
            last7[co] += 1
        elif d63 <= fs < d7:
            prior8w[co] += 1
    out = []
    for co, n in last7.items():
        if first_by_co.get(co, "9999") >= d21:
            continue                                  # 刚接入的公司，首扫≠爆发
        weekly_avg = prior8w.get(co, 0) / 8
        if n >= 5 and n >= 3 * max(weekly_avg, 0.5):
            out.append({"company": co, "new7": n, "weekly_avg": round(weekly_avg, 1)})  # co=norm 名
    out.sort(key=lambda x: -x["new7"])
    return out[:top]


def sentinel_titles(rows, today: datetime.date, top: int = 10) -> list[dict]:
    """哨兵岗位（Bloomberg 读 OpenAI 招 power-trading 岗那招）：某公司第一次出现
    某个 title 词 = 战略动向。词须是行业通用词（≥5 家公司用过），否则是起名噪音。"""
    d7 = (today - datetime.timedelta(days=7)).isoformat()
    vocab_co: dict[str, set] = collections.defaultdict(set)
    old_by_co: dict[str, set] = collections.defaultdict(set)
    fresh = []
    for r in rows:
        toks = set(_TOKEN_RE.findall((r["title"] or "").lower())) - _STOP
        co = r["company_norm"] or r["company"]
        for t in toks:
            vocab_co[t].add(co)
        if (r["first_seen"] or "") >= d7:
            fresh.append((r, toks, co))
        else:
            old_by_co[co] |= toks
    out = []
    for r, toks, co in fresh:
        novel = [t for t in toks
                 if t not in old_by_co[co] and len(vocab_co[t]) >= 5]
        if novel:
            out.append({"company": r["company"], "title": r["title"], "new_words": novel})
    seen = set()
    uniq = []
    for o in out:                                  # 一家公司只报最有代表性的一条
        if o["company"] in seen:
            continue
        seen.add(o["company"]); uniq.append(o)
    return uniq[:top]


def vendor_mentions(cfg: dict, rows, today: datetime.date, top: int = 15) -> list[dict]:
    """厂商/产品提及榜（Revealera 的 tech-adoption 信号）：JD 里点名某产品 = 采用/
    集成意图，比技能份额更早。词典在 config trends.vendors。"""
    vendors = (cfg.get("trends") or {}).get("vendors") or {}
    if not vendors:
        return []
    d14 = (today - datetime.timedelta(days=14)).isoformat()
    out = []
    for name, rx_s in vendors.items():
        rx = re.compile(rx_s, re.IGNORECASE)
        hits = [r for r in rows if rx.search(r["jd_text"])]
        n_new = sum(1 for r in hits if (r["first_seen"] or "") >= d14)
        cos = len({r["company_norm"] or r["company"] for r in hits})
        if hits:
            out.append({"vendor": name, "mentions": len(hits), "companies": cos, "new14": n_new})
    out.sort(key=lambda x: -x["new14"])
    return out[:top]


_HN_CACHE = TRENDS_DIR / "hn_hiring_cache.json"


def _discover_hn_thread() -> str | None:
    """自动找当月 "Ask HN: Who is hiring?" 帖 id：whoishiring 机器人每月 1 号发帖，
    Algolia 按时间倒序第一条就是（带标题前缀双保险，防撞上 Who wants to be hired）。"""
    import json as _json
    import urllib.request
    url = ("https://hn.algolia.com/api/v1/search_by_date"
           "?query=%22who%20is%20hiring%22&tags=story,author_whoishiring&hitsPerPage=5")
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            hits = _json.loads(r.read().decode("utf-8", "ignore")).get("hits") or []
    except Exception:  # noqa: BLE001
        return None
    for h in hits:
        if (h.get("title") or "").startswith("Ask HN: Who is hiring?"):
            return h.get("objectID")
    return None


def refresh_hn_hiring(cfg: dict, today: datetime.date) -> None:
    """拉当月 HN "Who is hiring" 帖，按主题词典数「多少条招聘评论提到」→ 全市场
    （创业公司侧）对照。帖 id 自动发现（owner 2026-09-10：别让我每月手动换）；
    config trends.hn_hiring_thread 仅作手动覆盖。只在每日快照里跑；任何失败都
    静默留旧缓存 —— 对照列缺席不拦主表。"""
    import json as _json
    import urllib.request
    tid = (cfg.get("trends") or {}).get("hn_hiring_thread") or _discover_hn_thread()
    if not tid:
        return
    try:
        with urllib.request.urlopen(
                f"https://hn.algolia.com/api/v1/items/{tid}", timeout=30) as r:
            d = _json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:  # noqa: BLE001
        return
    comments = [c.get("text") or "" for c in (d.get("children") or [])]
    comments = [t for t in comments if t]        # 只数顶层 = 真正的招聘帖，楼中楼是求职者回复
    shares = {}
    for name, rx in _themes(cfg).items():
        hits = sum(1 for t in comments if rx.search(t))
        shares[name] = round(100 * hits / len(comments), 1) if comments else 0
    _HN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _HN_CACHE.write_text(_json.dumps(
        {"date": today.isoformat(), "title": d.get("title"),
         "n_comments": len(comments), "shares": shares}, ensure_ascii=False))


def hn_hiring_shares() -> dict | None:
    try:
        return json.loads(_HN_CACHE.read_text())
    except Exception:  # noqa: BLE001
        return None


def compute(cfg: dict, conn: sqlite3.Connection, today: datetime.date | None = None) -> dict:
    today = today or datetime.date.today()
    rows = _rows(conn)
    return {
        "date": today.isoformat(),
        "corpus": len(rows),
        "themes": theme_stats(cfg, rows, today),
        "novel_terms": novel_terms(cfg, rows, today),
        "company_velocity": company_velocity(rows, today),
        "sentinels": sentinel_titles(rows, today),
        "vendors": vendor_mentions(cfg, rows, today),
        "hn_hiring": hn_hiring_shares(),
    }


# ---------- 第三期：行动建议（纯本地推导） ----------

def _cap_roles(cfg: dict, cand: list, top_roles: int) -> list:
    """推荐岗位纪律（owner 2026-09-13）：一家公司最多 per_company_cap 个、同名 title
    去重 —— 否则 Nvidia 的 Fleet Intelligence 三连岗刷满整个清单。"""
    rec = (cfg.get("trends") or {}).get("recommend") or {}
    cap = int(rec.get("per_company_cap", 2))
    dedupe = bool(rec.get("dedupe_title", True))
    out, per_co, seen_titles = [], collections.Counter(), set()
    for r in cand:
        co = r["company"]
        tkey = (co, (r["title"] or "").strip().lower())
        if per_co[co] >= cap:
            continue
        if dedupe and tkey in seen_titles:
            continue
        per_co[co] += 1
        seen_titles.add(tkey)
        out.append(r)
        if len(out) >= top_roles:
            break
    return out

def suggestions(cfg: dict, conn: sqlite3.Connection, data: dict, top_roles: int = 8) -> dict:
    """趋势 → 行动。供需指数 = 挂龄超出基线的比例 × 新增份额涨幅（都是正向缺人信号）。
    产出：该主打的 angle + 供不应求主题里 review 队列现成可投的岗。"""
    themes = data["themes"]
    ages = sorted(v["median_age"] for v in themes.values() if v["median_age"])
    base_age = (statistics.median(ages) if ages else 30) or 30   # 全主题中位当基线，不取最失真的 min
    exclude = set(((cfg.get("trends") or {}).get("recommend") or {}).get("exclude_themes") or [])
    ranked = []
    for name, v in themes.items():
        if name in exclude:
            continue                        # 观察哨主题看趋势不进榜（非软件岗为主）
        if not v["median_age"] or v["count"] < 30:
            continue
        if v.get("confidence") == "low":
            continue                        # 少数公司刷出来的读数不配进「最缺人」榜
        idx = (v["median_age"] / base_age - 1) * 100 + min(v["new_share_delta_pp"], 5) * 5
        if (v.get("top3_share_pct") or 0) > 40:
            idx *= 0.7          # 一家撑起来的主题（如当年 AI-DC 的 63.4）不配满分
        ranked.append((round(idx, 1), name, v))
    ranked.sort(reverse=True)
    angle_map = (cfg.get("trends") or {}).get("angle_map") or {}
    hot = ranked[0] if ranked else None
    picks = []
    if hot:
        rx = _themes(cfg)[hot[1]]
        rows = conn.execute(
            "SELECT r.company, r.title, r.weighted_score, r.first_seen, r.posted_at,"
            "       COALESCE(s.status,'new') st, COALESCE(s.tailor_status,'none') ts"
            " FROM jobs.roles r LEFT JOIN role_state s ON s.job_id=r.job_id"
            " WHERE r.jd_text IS NOT NULL AND r.verdict != 'drop'").fetchall()
        floor = float((cfg.get("resume_scoring") or {}).get("floor", 65))
        gd = int(_thr(cfg, "ghost_days", 45))
        today_ = datetime.date.fromisoformat(data["date"])
        cand = [r for r in rows if r["st"] == "new" and rx.search(r["title"] or "")
                and (r["weighted_score"] or 0) >= floor
                and (_age_days(r, today_) or 999) <= gd]
        # 只按 title 命中（rx.search(company) 会把叫 Grid 的公司整家误收——评审实锤 bug）；
        # 挂龄超幽灵线、分数低于 floor 的不配出现在「今天能行动」的推荐位。
        cand.sort(key=lambda r: -(r["weighted_score"] or 0))
        picks = [{"company": r["company"], "title": r["title"],
                  "score": round(r["weighted_score"] or 0),
                  "resume_ready": r["ts"] == "ready"}
                 for r in _cap_roles(cfg, cand, top_roles)]
    return {
        "ranking": [{"theme": n, "undersupply_index": i,
                     "median_age": v["median_age"], "delta_pp": v["new_share_delta_pp"],
                     "companies": v.get("companies"), "top3_share_pct": v.get("top3_share_pct"),
                     "confidence": v.get("confidence"),
                     "angle": angle_map.get(n)} for i, n, v in ranked],
        "hot_theme": hot[1] if hot else None,
        "hot_roles": picks,
    }


def theme_details(cfg: dict, conn: sqlite3.Connection, data: dict,
                  top_themes: int = 3, top_roles: int = 8, top_skills: int = 12) -> list[dict]:
    """把主题落到人身上（owner 2026-09-10：光有关键词不够，要岗位和工作细节）。

    对供需指数前 N 的主题，回答三个问题：
      这类岗要什么技能 —— 复用打分器的 77 词硬技能词典，数该主题 JD 里各技能的出现率；
      我缺什么该学什么 —— 对照 base 简历：JD 高频但 base 没有的技能 = 学习清单；
      具体是哪些岗 —— review 队列里 title 命中、没投过的代表岗位。"""
    from . import tailoring
    base_text = tailoring._base_text(tailoring.load_base(cfg))
    skills = _skills(cfg)
    have = {name for name, rx in skills.items() if rx.search(base_text)}
    sug = suggestions(cfg, conn, data)
    pick = [r["theme"] for r in sug["ranking"][:top_themes]]
    rows = conn.execute(
        "SELECT r.company, r.title, r.jd_text, r.weighted_score, r.posted_at, r.first_seen,"
        "       r.verdict, COALESCE(s.status,'new') st, COALESCE(s.tailor_status,'none') ts"
        " FROM jobs.roles r LEFT JOIN role_state s ON s.job_id=r.job_id"
        " WHERE r.jd_text IS NOT NULL AND r.jd_text != ''").fetchall()
    today = datetime.date.fromisoformat(data["date"])
    themes = _themes(cfg)
    # 全语料底率一次算好：技能榜排序按倍率(lift = 主题出现率/全语料出现率)。
    # 纯出现率会被 Python/AWS 这类到处都是的词霸榜；倍率才回答"这个主题特别要什么"。
    base_rate = {name: (sum(1 for r in rows if rx.search(r["jd_text"])) / len(rows) if rows else 0)
                 for name, rx in skills.items()}
    out = []
    for name in pick:
        rx = themes[name]
        hits = [r for r in rows if rx.search(r["jd_text"]) or rx.search(r["title"] or "")]
        if not hits:
            continue
        srows = []
        for sname, srx in skills.items():
            p = sum(1 for r in hits if srx.search(r["jd_text"])) / len(hits)
            if p < 0.10:                      # 占比 <10% 的不进榜，防小样本高倍率
                continue
            lift = p / base_rate[sname] if base_rate[sname] else 0
            srows.append({"skill": sname, "share_pct": round(100 * p),
                          "lift": round(lift, 1), "have": sname in have})
        srows.sort(key=lambda x: -x["lift"])
        floor = float((cfg.get("resume_scoring") or {}).get("floor", 65))
        gd = int(_thr(cfg, "ghost_days", 45))
        cand = [r for r in hits
                if r["st"] == "new" and r["verdict"] != "drop" and rx.search(r["title"] or "")
                and (r["weighted_score"] or 0) >= floor and (_age_days(r, today) or 999) <= gd]
        cand.sort(key=lambda r: -(r["weighted_score"] or 0))
        roles = [{"company": r["company"], "title": r["title"],
                  "score": round(r["weighted_score"] or 0),
                  "age": _age_days(r, today),
                  "resume_ready": r["ts"] == "ready"} for r in _cap_roles(cfg, cand, top_roles)]
        out.append({"theme": name, "n_jds": len(hits), "skills": srows[:top_skills],
                    "roles": roles})
    return out


# ---------- 「该补」模拟重算（owner 2026-09-13：出现率≥15% 换成"补了能多过线几个岗"） ----------

def compute_skill_gaps(cfg: dict, conn: sqlite3.Connection, thr: float = 70, top: int = 10) -> list[dict]:
    """对每个 base 简历没有的硬技能 s：假装简历里有 s，重算所有未投岗的匹配分，
    数「从 <thr 翻到 ≥thr」的岗位数和公司数。跨主题合并 —— 反复出现的技能自然靠前。

    重算走解析路（从 ResumeScore 暴露的 matched/missing 反推 coverage 增量），不真跑
    9k 次×N 技能的正则。候选技能限于打分器词表（加词表外的技能不动真实分数，模拟没意义）。
    重活（~9k 次 score_resume）只在每日快照里跑，页面/周报读快照。"""
    from . import resume_score, tailoring
    base = tailoring.load_base(cfg)
    base_text = tailoring._base_text(base)
    merged = tailoring.merge(base, {})
    rs = (cfg.get("resume_scoring") or {})
    K = float(rs.get("shrink_k", 6)); P0 = float(rs.get("shrink_prior", 0.60))
    rows = conn.execute(
        "SELECT r.job_id, r.company, r.title, r.jd_text FROM jobs.roles r"
        " LEFT JOIN role_state s ON s.job_id=r.job_id"
        " WHERE r.jd_text IS NOT NULL AND r.jd_text != '' AND r.verdict != 'drop'"
        "   AND COALESCE(s.status,'new') = 'new'").fetchall()
    gained_jobs: dict[str, set] = collections.defaultdict(set)
    gained_cos: dict[str, set] = collections.defaultdict(set)
    for r in rows:
        try:
            sc = resume_score.score_resume(r["jd_text"], merged, cfg, base_text, title=r["title"])
        except Exception:  # noqa: BLE001
            continue
        if sc.overall >= thr or not sc.missing_hard:
            continue
        need = (2 * (len(sc.matched_hard) + len(sc.missing_hard))
                + len(sc.matched_soft) + len(sc.missing_soft))
        got = sc.skills_coverage * (need + K) / 100 - K * P0
        new_cov = 100 * (got + 2 + K * P0) / (need + K)
        new_overall = sc.overall + 0.60 * (new_cov - sc.skills_coverage)
        if new_overall >= thr:
            for skill in sc.missing_hard:
                gained_jobs[skill].add(r["job_id"])
                gained_cos[skill].add(r["company"])
    # 只留「可学的具体技能」（trends.skills 白名单交集）：打分器词表里还有
    # authorization / low latency / backup & restore 这类岗位属性词——它们提得分是真的，
    # 但没法报班学，进「该补」清单只会稀释注意力（owner 2026-09-10 iterate 轮抓出）。
    learnable = {k.lower() for k in ((cfg.get("trends") or {}).get("skills") or {})}
    out = [{"skill": k, "jobs": len(v), "companies": len(gained_cos[k])}
           for k, v in gained_jobs.items()
           if len(gained_cos[k]) >= 3 and k.lower() in learnable]
    out.sort(key=lambda x: -x["jobs"])
    return out[:top]


def latest_gaps() -> tuple[str, list[dict]]:
    """读最近一份带 skill_gaps 的每日快照（重算太贵，页面和周报都不现场跑）。
    返回 (快照日期, 榜单)——展示层必须把日期印出来，>3 天旧要加警告。"""
    try:
        for p in sorted(TRENDS_DIR.glob("????-??-??.json"), reverse=True):
            d = json.loads(p.read_text())
            if "skill_gaps" in d:
                return p.stem, d["skill_gaps"]
    except Exception:  # noqa: BLE001
        pass
    return "", []


# ---------- 快照 / 周报 / 邮件 ----------

def write_snapshot(cfg: dict, conn) -> Path:
    refresh_hn_hiring(cfg, datetime.date.today())
    data = compute(cfg, conn)
    data["skill_gaps"] = compute_skill_gaps(cfg, conn)
    TRENDS_DIR.mkdir(parents=True, exist_ok=True)
    p = TRENDS_DIR / f"{data['date']}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    _append_skill_history(cfg, conn, data["date"])
    return p


def _append_skill_history(cfg: dict, conn, date: str) -> None:
    """技能周序列（owner 2026-09-13 第 8 条）：每天把每个技能在近 14 天新岗里的占比追加进
    CSV。现在只存不展示；攒够四周后落地层加 4 周 ↑↓ 箭头。同日重跑不重复写。"""
    import csv
    hist = TRENDS_DIR / "skill_history.csv"
    if hist.exists() and f"\n{date}," in hist.read_text()[-20000:]:
        return
    d14 = (datetime.date.fromisoformat(date) - datetime.timedelta(days=14)).isoformat()
    all_rows = _rows(conn)
    rows = [r for r in all_rows if (r["first_seen"] or "") >= d14]
    stable = [r for r in _stable_panel(all_rows, datetime.date.fromisoformat(date))
              if (r["first_seen"] or "") >= d14]
    new_file = not hist.exists()
    with hist.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            # companies 列防单一公司 JD 模板伪造 spike；panel_stable_share 是画趋势线
            # 该用的口径（剔除接入 <21 天公司，面板扩容不冒充需求增长）。
            w.writerow(["date", "skill", "share", "n", "companies", "panel_stable_share"])
        for name, rx in _skills(cfg).items():
            hits = [r for r in rows if rx.search(r["jd_text"])]
            n_st = sum(1 for r in stable if rx.search(r["jd_text"]))
            w.writerow([date, name, round(len(hits) / len(rows), 4) if rows else 0, len(hits),
                        len({r["company_norm"] or r["company"] for r in hits}),
                        round(n_st / len(stable), 4) if stable else 0])


def _fmt_pct(x):  # noqa: ANN001
    return f"{x:+.1f}pp" if isinstance(x, (int, float)) else "—"


def weekly_report(cfg: dict, conn) -> Path:
    today = datetime.date.today()
    data = compute(cfg, conn, today)
    sug = suggestions(cfg, conn, data)
    L = [f"# 鸭先知周报 · {today.isoformat()}",
         "",
         f"语料：{data['corpus']} 条在库岗位（210 家公司每日全量，jobscan 私有纵向数据）。",
         "读法：**挂龄** = 岗位挂出至今的天数，挂得久 = 招不到人 = 供不应求；",
         "**新增份额环比** = 最近 14 天新岗位里该主题的占比，对比再前 14 天。",
         "", "## 一、主题热度与供需", "",
         "| 主题 | 在库 | 公司数 | top3集中 | 置信度 | 完结耗时* | 挂龄中位** | 幽灵岗 | 近7新增 | 环比(面板恒定) | HN帖† |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, v in sorted(data["themes"].items(),
                          key=lambda kv: -(kv[1]["median_fill"] or kv[1]["median_age"] or 0)):
        fill = (f"{v['median_fill']}天({v['n_closed_cos']}家)" if v["median_fill"] else "样本不足")
        conf_cn = {"high": "高", "medium": "中", "low": "⚠低"}[v.get("confidence", "medium")]
        hn = data.get("hn_hiring") or {}
        hn_s = hn.get("shares", {}).get(name)
        L.append(f"| {name} | {v['count']} | {v['companies']} | {v['top3_share_pct']}% | {conf_cn} "
                 f"| **{fill}** | {v['median_age']}天 | {v['ghost_pct']}% | {v['new7']} "
                 f"| {_fmt_pct(v['new_share_delta_pp'])} | {str(hn_s) + '%' if hn_s is not None else '—'} |")
    L += ["", "\\*完结耗时 = 已下架岗从发布到消失的天数（首选口径：真实的招满/关闭周期）；",
          f"\\*\\*挂龄 = 仍在架岗位的年龄，已剔除 >{_thr(cfg, 'ghost_days', 45)} 天的幽灵岗（单列 幽灵岗 占比）。",
          "科技公司幽灵岗研究估计高达 ~21%（arXiv:2410.21771），挂龄必须配合完结耗时读。",
          "绝对天数偏短（看板翻页抖动和 repost 重置会把耗时算短）——读**主题之间的相对差**，别读绝对值。",
          "完结耗时按公司平衡（每家先取中位再跨公司取中位），大板子刷不动权重；括号里是合格公司数。",
          "**置信度**：⚠低 = 不足15家公司或 top3 公司占 >60%，读数代表少数公司而非市场，已排除出「最缺人」榜。",
          "†HN帖 = 当月 HN Who is hiring 帖里提到该主题的招聘评论占比（创业公司侧的全市场对照）。"]
    for d in theme_details(cfg, conn, data):
        L += ["", f"## ▸ 落地：「{d['theme']}」({d['n_jds']} 份 JD)", "",
              "**这类岗特别要什么**（按倍率排：出现率(全语料倍数)，✅=你有 📚=你没有）：", ""]
        L.append("　" + " · ".join(
            f"{'✅' if s_['have'] else '📚'}{s_['skill']} {s_['share_pct']}%({s_['lift']}×)"
            for s_ in d["skills"]))
        if d["roles"]:
            L += ["", "**具体岗位**（title 命中·未投·按分数）：", ""]
            for p_ in d["roles"]:
                rdy = "📄" if p_["resume_ready"] else "⭘"
                L.append(f"- {rdy} {p_['company']} — {p_['title']}（{p_['score']} 分·挂 {p_['age']} 天）")
    L += ["", "## 二、新词雷达（近 14 天冒出来的词）", ""]
    if data["novel_terms"]:
        L.append("| 词 | 新岗提及 | 公司数 | 旧语料 | 倍率 |")
        L.append("|---|---|---|---|---|")
        for t in data["novel_terms"]:
            L.append(f"| `{t['term']}` | {t['new_docs']} | {t['companies']} | {t['old_docs']} | {t['ratio'] or '∞'}× |")
    else:
        L.append("（本周没有达到阈值的新词）")
    L += ["", "## 二点五、哨兵岗位（公司第一次招这类 title = 战略动向）", ""]
    if data.get("sentinels"):
        for o in data["sentinels"]:
            L.append(f"- **{o['company']}** 首招「{o['title']}」（新词：{', '.join(o['new_words'][:3])}）")
    else:
        L.append("（本周无）")
    L += ["", "## 二点六、厂商/产品提及榜（JD 点名 = 采用意图，比技能更早的信号）", ""]
    if data.get("vendors"):
        L.append("| 产品 | JD 提及 | 公司数 | 近14天新岗 |")
        L.append("|---|---|---|---|")
        for x in data["vendors"]:
            L.append(f"| {x['vendor']} | {x['mentions']} | {x['companies']} | {x['new14']} |")
    else:
        L.append("（config trends.vendors 未配置）")
    L += ["", "## 三、公司发岗速度突变（近 7 天 ≥3× 此前周均）", ""]
    if data["company_velocity"]:
        L.append("| 公司 | 近7天 | 此前周均 |")
        L.append("|---|---|---|")
        for c in data["company_velocity"]:
            L.append(f"| {c['company']} | {c['new7']} | {c['weekly_avg']} |")
    else:
        L.append("（无显著突增）")
    L += ["", "## 四、行动建议（第三期·本地推导）", ""]
    if sug["ranking"]:
        L.append("供需指数 = 挂龄超基线% + 新增份额涨幅×5（越高越缺人）：")
        L.append("")
        for r in sug["ranking"][:5]:
            angle = f" → 简历主打 **{r['angle']}** angle" if r["angle"] else ""
            L.append(f"- **{r['theme']}**：指数 {r['undersupply_index']}（挂龄 {r['median_age']}天，"
                     f"环比 {_fmt_pct(r['delta_pp'])}，{r.get('companies')}家公司·top3占{r.get('top3_share_pct')}%·"
                     f"置信{ {'high':'高','medium':'中','low':'低'}[r.get('confidence','medium')] }）{angle}")
        if sug["hot_roles"]:
            L += ["", f"**最缺人主题「{sug['hot_theme']}」在 review 队列里现成可投的岗**（title 命中）：", ""]
            for p in sug["hot_roles"]:
                rdy = "📄" if p["resume_ready"] else "⭘"
                L.append(f"- {rdy} {p['company']} — {p['title']}（{p['score']} 分）")
    gap_date, gaps = latest_gaps()
    if gaps:
        stale = ""
        try:
            if (today - datetime.date.fromisoformat(gap_date)).days > 3:
                stale = " ⚠️快照过期"
        except ValueError:
            pass
        L += ["", f"## ▸ 该补什么（模拟重算·快照 {gap_date}{stale}）", ""]
        for g in gaps:
            L.append(f"- **{g['skill']}** → +{g['jobs']} 岗 / {g['companies']} 家")
        L += ["", "口径：每个技能独立模拟、**数字不可加总**（同一岗可能被多个技能翻线）；",
              "「翻线」= 过 tailor 门槛可以投了，不等于有竞争力；只列 ≥3 家公司受益的。"]
    L += ["", "---", "*本报告由 app/trends.py 本地统计生成，零联网零 LLM；"
          "「本周风向（联网验证）」一节由周一的 headless opus 补写。*", ""]
    out_dir = TRENDS_DIR / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{today.isoformat()}.md"
    p.write_text("\n".join(L), encoding="utf-8")
    return p


def mail_report(cfg: dict, path: Path) -> dict:
    """把周报寄给 owner 自己（trends.report_to）。这不是外发邮件：收件人固定是
    config 里 owner 写死的自己的地址，属于 owner 明示要求的定期自寄报告。"""
    from . import mail
    to = (cfg.get("trends") or {}).get("report_to")
    if not to:
        raise RuntimeError("trends.report_to unset")
    body = Path(path).read_text(encoding="utf-8")
    subject = f"鸭先知周报 · {Path(path).stem}"
    return mail.send_reply(cfg, to, subject, body, from_name="JobScan 鸭先知", new_thread=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="鸭先知 trend 模块")
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--mail", metavar="PATH")
    args = ap.parse_args(argv)
    from . import store
    cfg = cfgmod.load_config()
    conn = store.connect_view(cfg)   # user 库 + ATTACH jobs — suggestions 要读 role_state
    if args.snapshot:
        print("snapshot →", write_snapshot(cfg, conn))
    if args.report:
        print(weekly_report(cfg, conn))
    if args.mail:
        print(mail_report(cfg, Path(args.mail)))
    if not (args.snapshot or args.report or args.mail):
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
