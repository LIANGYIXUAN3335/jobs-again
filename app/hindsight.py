"""回头看 Hindsight — ideas 雷达的复盘层（owner 2026-09-10 拍板，judge-panel 定稿方案）。

每天全自动给雷达抓到的新品拍「出生照」，在 T+30/90/180 天回访拿真实结局，
攒出「当天看着热闹的东西，90 天后到底怎么样了」的私有账本 → 产品直觉的原料。

家规：
  · 零 LLM 零 API key —— 只碰 GitHub REST(匿名+etag)、HN Algolia、裸 GET 三样。
  · ideas.db 的第二个（也是最后一个）写者：app.ideas 08:30 抓取、本模块 21:40 回访，
    彻底错峰。第三个写者禁止直连；web 进程只写 user_state.db 的 idea_predictions。
  · 防幸存者偏差是命门：入队按类型全收，绝不按热度筛；unknown 如实标记单列。
  · snapshots 是不可变账本，verdict 永远可由它重算（`reverdict` 子命令，对齐 rescore 家规）。
  · 量的是「开发者热度轨迹」不是商业成败：闷声赚钱的 B2B 会标 flat、转私有会标
    dead —— 任何报告都必须带这句免责。

CLI:
  python -m app.hindsight run          每晚主循环：入队(全收) + 当日 gh 基线 + 到期回访 + verdict
  python -m app.hindsight reverdict    改完 config hindsight: 阈值后，从 snapshots 就地重算
  python -m app.hindsight stats        打印基数率 JSON（月报的原料，肉眼第一课）

退出码：0 正常；2 = 警戒（今天 items 有新行但入组 0 条 / 到期积压 >14 天）——
hb_run 会把非零 exit 写进心跳卡片，triage 的红灯体系自然接住，不用改 /health。
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request

from . import config as cfgmod
from .ideas import connect_ideas

UA = {"User-Agent": "jobscan-hindsight/1.0 (personal research; low volume)"}
GH_BUDGET = 55          # 每次运行最多 GitHub 请求数（匿名 60/h，留余量）
GH_SLEEP = 2.0          # 请求间隔
PHASES = ["t30", "t90", "t180"]
PHASE_DAYS = {"t30": 30, "t90": 90, "t180": 180}
# 共享域：HN 提及搜索必须搜全路径，不然把别人的故事算成你的
SHARED_DOMAINS = {"github.com", "github.io", "vercel.app", "netlify.app", "youtube.com",
                  "youtu.be", "huggingface.co", "producthunt.com", "apps.apple.com",
                  "chromewebstore.google.com", "news.ycombinator.com", "x.com", "twitter.com"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS track(
  key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,              -- 'gh' | 'site' | 'hn_only' | 'ph'
  gh_repo TEXT,
  domain TEXT,
  probe_url TEXT,
  base_stars INTEGER,
  base_taken_at TEXT,
  etag TEXT,
  is_backfill INTEGER NOT NULL DEFAULT 0,
  next_due TEXT NOT NULL,
  next_phase TEXT NOT NULL,
  verdict TEXT NOT NULL DEFAULT 'pending',
  verdict_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_track_due ON track(next_due) WHERE next_phase != 'done';
CREATE TABLE IF NOT EXISTS snapshots(
  key TEXT NOT NULL,
  phase TEXT NOT NULL,
  taken_at TEXT NOT NULL,
  stars INTEGER, forks INTEGER, open_issues INTEGER,
  pushed_at TEXT, archived INTEGER, gone INTEGER,
  hn_mentions INTEGER, hn_top_pts INTEGER,
  http_status INTEGER, redirect_domain TEXT,
  raw TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(key, phase)
);
CREATE TABLE IF NOT EXISTS retro_reports(
  month TEXT PRIMARY KEY, content TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def _cfg_thresholds(cfg: dict) -> dict:
    h = cfg.get("hindsight") or {}
    return {
        "takeoff_mult": float(h.get("takeoff_mult", 3.0)),
        "takeoff_abs": int(h.get("takeoff_abs", 500)),
        "takeoff_total": int(h.get("takeoff_total", 5000)),
        "takeoff_mentions": int(h.get("takeoff_mentions", 2)),
        "takeoff_mention_pts": int(h.get("takeoff_mention_pts", 50)),
        "grow_pct": float(h.get("grow_pct", 30)),
        "dead_flat_pct": float(h.get("dead_flat_pct", 5)),
    }


def connect(cfg: dict):
    conn = connect_ideas(cfg)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _http_json(url: str, etag: str | None = None, timeout: int = 15):
    """GET → (status, json|None, new_etag)。304 时 json=None。"""
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/json",
                                               **({"If-None-Match": etag} if etag else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore")), r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        return e.code, None, None
    except Exception:  # noqa: BLE001 — DNS/超时/断连都归为「本次探不到」
        return 0, None, None


def _probe_alive(url: str) -> tuple[int, str]:
    """裸 GET 活性探测 → (status, 最终落点的注册域)。跳到别的域=疑似收购/改名。"""
    req = urllib.request.Request(url, headers=UA, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            host = urllib.parse.urlsplit(r.url).hostname or ""
            return r.status, ".".join(host.split(".")[-2:])
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001
        return 0, ""


def _domain_of(url: str) -> str:
    host = urllib.parse.urlsplit(url or "").hostname or ""
    return ".".join(host.split(".")[-2:]) if host else ""


def _gh_repo_of(url: str) -> str | None:
    m = re.match(r"https?://github\.com/([\w.-]+)/([\w.-]+?)(?:/|\?|#|$)", url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _hn_query_for(t: dict) -> str | None:
    """HN 提及搜索词：普通域搜域名；共享域搜全路径；没有可搜的返回 None。"""
    if t["domain"] and t["domain"] not in SHARED_DOMAINS:
        return t["domain"]
    if t["gh_repo"]:
        return f"github.com/{t['gh_repo']}"
    if t["probe_url"] and _domain_of(t["probe_url"]) in SHARED_DOMAINS:
        path = urllib.parse.urlsplit(t["probe_url"])
        return (path.hostname or "") + path.path
    return None


# ---------- intake：按类型全收（防幸存者偏差的命门） ----------

def intake(cfg: dict, conn, today: datetime.date, resolve_cap: int = 200) -> dict:
    """把 items 里还没入队的可跟踪项收编进 track。全收、不筛热度、不设条数帽
    （只有 show_hn 的 Algolia URL 解析设每次上限，纯粹是礼貌）。"""
    rows = conn.execute(
        "SELECT i.* FROM items i LEFT JOIN track t ON t.key=i.key WHERE t.key IS NULL").fetchall()
    added = skipped = resolved = 0
    for r in rows:
        src, url = r["source"], r["url"]
        kind = gh_repo = domain = probe = None
        note = ""
        if src == "gh":
            kind, gh_repo, probe = "gh", _gh_repo_of(url), url
        elif src == "ph":
            kind, probe = "ph", url                      # PH 信号先天残疾，如实弱跟踪
        elif src in ("show_hn", "hn_front"):
            real = url
            if src == "show_hn" and "news.ycombinator.com" in (url or ""):
                if resolved >= resolve_cap:
                    continue                             # 这批先不入队，明天接着解析
                sid = r["key"].split(":", 1)[1]
                _, d, _ = _http_json(f"https://hn.algolia.com/api/v1/items/{sid}")
                resolved += 1
                real = (d or {}).get("url") or ""
            if src == "hn_front" and not _gh_repo_of(real):
                skipped += 1                             # 新闻文章没有成败可言，不进队
                continue
            if _gh_repo_of(real):
                kind, gh_repo, probe = "gh", _gh_repo_of(real), real
            elif real:
                kind, probe = "site", real
            else:
                kind, note = "hn_only", "纯文本帖，无外链，只数 HN 提及"
        if not kind:
            skipped += 1
            continue
        first = datetime.date.fromisoformat(r["first_seen"][:10])
        phase = next((p for p in PHASES
                      if first + datetime.timedelta(days=PHASE_DAYS[p]) >= today), None)
        due = (first + datetime.timedelta(days=PHASE_DAYS[phase])) if phase else today
        backfill = 1 if (today - first).days > 1 else 0
        # 已经全过 t180 的老项：直接安排 t180 补拍一站定稿（due=today）
        conn.execute(
            "INSERT OR IGNORE INTO track(key,kind,gh_repo,domain,probe_url,is_backfill,"
            " next_due,next_phase,note) VALUES(?,?,?,?,?,?,?,?,?)",
            (r["key"], kind, gh_repo, _domain_of(probe or ""), probe, backfill,
             due.isoformat(), phase or "t180", note))
        added += 1
    conn.commit()
    return {"added": added, "skipped": skipped, "resolved": resolved,
            "candidates": len(rows)}


# ---------- 探针 ----------

def _gh_snapshot(t: dict, budget: dict) -> dict | None:
    """GitHub 仓库探针。budget 字典跨调用记账；预算/限额用尽返回 None（明天再来）。"""
    if budget["left"] <= 0:
        return None
    time.sleep(GH_SLEEP)
    budget["left"] -= 1
    status, d, etag = _http_json(f"https://api.github.com/repos/{t['gh_repo']}",
                                 etag=t.get("etag"))
    if status == 403:
        budget["left"] = 0                               # 被限流，本次全线收工
        return {"gone": 0, "http_status": 403}
    if status == 404:
        return {"gone": 1, "http_status": 404}
    if status == 304:
        return {"gone": 0, "http_status": 304, "not_modified": True, "etag": t.get("etag")}
    if status != 200 or not d:
        return {"gone": 0, "http_status": status}
    if d.get("full_name") and d["full_name"] != t["gh_repo"]:
        pass                                             # 301 改名：调用方更新 gh_repo + note
    return {"gone": 0, "http_status": 200, "etag": etag,
            "stars": d.get("stargazers_count"), "forks": d.get("forks_count"),
            "open_issues": d.get("open_issues_count"), "pushed_at": d.get("pushed_at"),
            "archived": 1 if d.get("archived") else 0, "full_name": d.get("full_name")}


def _hn_mentions(query: str, since_epoch: int, skip_before: str) -> tuple[int, int]:
    """自上个快照以来的 HN 新提及（剔除发布当天余波）→ (条数, 最高分)。"""
    q = urllib.parse.quote(query)
    _, d, _ = _http_json(
        f"https://hn.algolia.com/api/v1/search?query={q}&tags=story"
        f"&numericFilters=created_at_i>{since_epoch}&hitsPerPage=30")
    hits = (d or {}).get("hits") or []
    hits = [h for h in hits if (h.get("created_at") or "")[:10] > skip_before]
    return len(hits), max((h.get("points") or 0 for h in hits), default=0)


def checkpoint(cfg: dict, conn, t: dict, today: datetime.date, budget: dict) -> bool:
    """给一个到期项拍快照 + 推进相位。返回 False = 预算不够，原地等明天。"""
    phase = t["next_phase"]
    snap = {"stars": None, "forks": None, "open_issues": None, "pushed_at": None,
            "archived": None, "gone": None, "hn_mentions": None, "hn_top_pts": None,
            "http_status": None, "redirect_domain": None}
    if t["kind"] == "gh" and t["gh_repo"]:
        g = _gh_snapshot(t, budget)
        if g is None:
            return False
        if g.get("not_modified"):
            prev = conn.execute("SELECT stars,forks,open_issues,pushed_at,archived FROM snapshots"
                                " WHERE key=? ORDER BY taken_at DESC LIMIT 1", (t["key"],)).fetchone()
            if prev:
                g.update({k: prev[k] for k in ("stars", "forks", "open_issues",
                                               "pushed_at", "archived")})
        snap.update({k: g.get(k) for k in snap if k in g})
        snap["http_status"] = g.get("http_status")
        if g.get("etag"):
            conn.execute("UPDATE track SET etag=? WHERE key=?", (g["etag"], t["key"]))
        if g.get("full_name") and g["full_name"].lower() != (t["gh_repo"] or "").lower():
            conn.execute("UPDATE track SET gh_repo=?, note=note||? WHERE key=?",
                         (g["full_name"], f"[改名 {t['gh_repo']}→{g['full_name']}]", t["key"]))
    elif t["probe_url"]:
        status, final_dom = _probe_alive(t["probe_url"])
        snap["http_status"] = status
        snap["gone"] = 1 if status in (404, 410) or status == 0 else 0
        if final_dom and t["domain"] and final_dom != t["domain"]:
            snap["redirect_domain"] = final_dom
    q = _hn_query_for(t)
    if q:
        prev_at = conn.execute("SELECT MAX(taken_at) FROM snapshots WHERE key=?",
                               (t["key"],)).fetchone()[0]
        first_seen = conn.execute("SELECT first_seen FROM items WHERE key=?",
                                  (t["key"],)).fetchone()[0]
        anchor = prev_at or first_seen
        epoch = int(datetime.datetime.fromisoformat(anchor[:10]).timestamp())
        skip_before = (datetime.date.fromisoformat(first_seen[:10])
                       + datetime.timedelta(days=2)).isoformat()
        snap["hn_mentions"], snap["hn_top_pts"] = _hn_mentions(q, epoch, skip_before)
    conn.execute(
        "INSERT OR REPLACE INTO snapshots(key,phase,taken_at,stars,forks,open_issues,"
        " pushed_at,archived,gone,hn_mentions,hn_top_pts,http_status,redirect_domain,raw)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (t["key"], phase, today.isoformat(), snap["stars"], snap["forks"],
         snap["open_issues"], snap["pushed_at"], snap["archived"], snap["gone"],
         snap["hn_mentions"], snap["hn_top_pts"], snap["http_status"],
         snap["redirect_domain"], ""))
    nxt = PHASES.index(phase) + 1
    if nxt >= len(PHASES):
        conn.execute("UPDATE track SET next_phase='done' WHERE key=?", (t["key"],))
    else:
        first = datetime.date.fromisoformat(
            conn.execute("SELECT first_seen FROM items WHERE key=?", (t["key"],)).fetchone()[0][:10])
        due = max(first + datetime.timedelta(days=PHASE_DAYS[PHASES[nxt]]),
                  today + datetime.timedelta(days=7))
        conn.execute("UPDATE track SET next_phase=?, next_due=? WHERE key=?",
                     (PHASES[nxt], due.isoformat(), t["key"]))
    conn.commit()
    return True


# ---------- verdict：五档，纯规则，随时可重算 ----------

def decide(cfg: dict, conn, key: str, today: datetime.date) -> str:
    th = _cfg_thresholds(cfg)
    t = dict(conn.execute("SELECT * FROM track WHERE key=?", (key,)).fetchone())
    snaps = [dict(r) for r in conn.execute(
        "SELECT * FROM snapshots WHERE key=? ORDER BY taken_at", (key,))]
    if not snaps:
        return "pending"
    last = snaps[-1]
    dead_votes = sum(1 for s in snaps[-2:] if s["gone"])
    mentions = sum(s["hn_mentions"] or 0 for s in snaps)
    top_pts = max((s["hn_top_pts"] or 0 for s in snaps), default=0)
    if t["kind"] == "gh":
        if last["archived"]:
            return "dead"                                # archived 是本人宣布，一票即死
        if dead_votes >= 2:
            return "dead"
        stars = last["stars"]
        base = t["base_stars"]
        first_stars = next((s["stars"] for s in snaps if s["stars"] is not None), None)
        ref = base if base is not None else first_stars
        if stars is not None:
            if ((ref and stars >= th["takeoff_mult"] * ref and stars - ref >= th["takeoff_abs"])
                    or stars >= th["takeoff_total"]
                    or (mentions >= th["takeoff_mentions"] and top_pts >= th["takeoff_mention_pts"])):
                return "took_off"
            if ref is not None and ref > 0 and stars >= ref * (1 + th["grow_pct"] / 100):
                return "alive_growing"
        pushed = (last["pushed_at"] or "")[:10]
        recent_push = pushed and (today - datetime.date.fromisoformat(pushed)).days <= 30
        if last["gone"]:
            return "unknown"                             # 一票死还差一票，先不判
        if recent_push:
            return "alive_growing"
        if (t["next_phase"] == "done" and ref is not None and stars is not None
                and stars < ref * (1 + th["dead_flat_pct"] / 100) and not recent_push):
            return "dead"
        return "alive_flat" if last["http_status"] in (200, 304) or stars is not None else "unknown"
    # site / ph / hn_only
    if dead_votes >= 2:
        return "dead"
    if mentions >= th["takeoff_mentions"] and top_pts >= th["takeoff_mention_pts"]:
        return "took_off"
    if last["gone"]:
        return "unknown"
    if t["kind"] == "hn_only":
        return "alive_growing" if mentions else "unknown"
    if last["http_status"] and last["http_status"] < 400:
        return "alive_growing" if mentions else "alive_flat"
    return "unknown"


def reverdict(cfg: dict, conn, today: datetime.date) -> int:
    n = 0
    for r in conn.execute("SELECT key FROM track WHERE verdict != 'pending'"):
        v = decide(cfg, conn, r["key"], today)
        conn.execute("UPDATE track SET verdict=?, verdict_at=? WHERE key=?",
                     (v, today.isoformat(), r["key"]))
        n += 1
    conn.commit()
    return n


# ---------- 主循环 ----------

def run(cfg: dict, conn, today: datetime.date | None = None) -> int:
    today = today or datetime.date.today()
    res = intake(cfg, conn, today)
    print(f"intake: +{res['added']} tracked, {res['skipped']} skipped, "
          f"{res['resolved']} show_hn resolved (of {res['candidates']} new)")
    budget = {"left": GH_BUDGET}
    # 当日新 gh 项的 T+0 基线 —— 易腐品，最高优先
    fresh = conn.execute(
        "SELECT t.*, i.first_seen FROM track t JOIN items i ON i.key=t.key"
        " WHERE t.kind='gh' AND t.base_stars IS NULL AND t.is_backfill=0"
        " AND i.first_seen>=?", ((today - datetime.timedelta(days=1)).isoformat(),)).fetchall()
    n_base = 0
    for t in map(dict, fresh):
        g = _gh_snapshot(t, budget)
        if g is None:
            break
        if g.get("stars") is not None:
            conn.execute("UPDATE track SET base_stars=?, base_taken_at=?, etag=? WHERE key=?",
                         (g["stars"], today.isoformat(), g.get("etag"), t["key"]))
            conn.execute("INSERT OR REPLACE INTO snapshots(key,phase,taken_at,stars,forks,"
                         "open_issues,pushed_at,archived,gone,http_status,raw)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?,'')",
                         (t["key"], "t0", today.isoformat(), g["stars"], g.get("forks"),
                          g.get("open_issues"), g.get("pushed_at"), g.get("archived"), 0,
                          g.get("http_status")))
            n_base += 1
    conn.commit()
    # 到期回访，最老优先
    due = [dict(r) for r in conn.execute(
        "SELECT * FROM track WHERE next_phase!='done' AND next_due<=? ORDER BY next_due",
        (today.isoformat(),))]
    n_cp = 0
    for t in due:
        if not checkpoint(cfg, conn, t, today, budget):
            break
        v = decide(cfg, conn, t["key"], today)
        conn.execute("UPDATE track SET verdict=?, verdict_at=? WHERE key=?",
                     (v, today.isoformat(), t["key"]))
        n_cp += 1
    conn.commit()
    backlog = conn.execute(
        "SELECT COUNT(*) FROM track WHERE next_phase!='done' AND next_due<=?",
        ((today - datetime.timedelta(days=14)).isoformat(),)).fetchone()[0]
    print(f"baselines {n_base} · checkpoints {n_cp}/{len(due)} due · gh budget left {budget['left']}"
          f" · backlog>14d {backlog}")
    new_items_today = conn.execute("SELECT COUNT(*) FROM items WHERE first_seen=?",
                                   (today.isoformat(),)).fetchone()[0]
    if (new_items_today and not res["added"] and res["candidates"]) or backlog > 0:
        print("WARN: 入组饿死或到期积压 >14 天 — 走 exit 2 让心跳卡片变响")
        return 2
    return 0


def stats(cfg: dict, conn) -> dict:
    base = {r[0]: r[1] for r in conn.execute(
        "SELECT verdict, COUNT(*) FROM track GROUP BY verdict")}
    by_kind = {}
    for r in conn.execute("SELECT kind, verdict, COUNT(*) n FROM track GROUP BY kind, verdict"):
        by_kind.setdefault(r["kind"], {})[r["verdict"]] = r["n"]
    ages = [r[0] for r in conn.execute(
        "SELECT julianday('now')-julianday(i.first_seen) FROM track t JOIN items i ON i.key=t.key")]
    return {"tracked": sum(base.values()), "verdicts": base, "by_kind": by_kind,
            "median_age_days": round(statistics.median(ages)) if ages else None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["run", "reverdict", "stats"])
    args = ap.parse_args(argv)
    cfg = cfgmod.load_config()
    conn = connect(cfg)
    today = datetime.date.today()
    if args.cmd == "run":
        return run(cfg, conn, today)
    if args.cmd == "reverdict":
        print("re-decided:", reverdict(cfg, conn, today))
        return 0
    print(json.dumps(stats(cfg, conn), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
