"""念稿练习 (recite) — 领域层：表、队列、判定门、派生指标。

设计定稿见 scratchpad/product/spec-v2.md + spec-v3.md（v3 是 v2 的 diff）与四份 critique-v3。
这个模块只做**确定性**的事：schema、队列选取、硬门/软分、Leitner 晋降箱、verbatim_ratio、
音频指标。LLM 判官在 ai.py::review_take()，ASR 在 recite_asr.py，导入在 recite_import.py。

三条贯穿全文件的设计约束（改之前先读，它们各自是一次被数据推翻的产物）：

1. **硬门里没有任何一个量依赖录音条件。** wpm 和 speaking_ratio 都退出了闸门位（裁决④：
   实测 208 条稿里 `wpm >= 125` 会 fail 掉 60% 的理想 take，而 `wpm <= 170` 触发 0 次 —— 这个
   量装反了方向）。语速现在**只**由 fluency 软分的行为锚点监控，那是全系统唯一看语速的地方，
   不得以"别处已经管了"为由放宽（面试专家 v3 §一，写进 spec §7 不要再动）。

2. **她看见的东西 == 她被评分的东西。** 录前左栏是 `content` 型要点卡，coverage 的分母也只有
   `content` + `canon`（裁决⑥/⑦）。v2 让她对着中文 gloss 说、按 key_points 评分，那是练习目标
   最不该有的错位。

3. **压缩档（constraint/truncate/transfer）的失败不降箱。** 它是拉伸练习不是保持测量；Leitner
   的降箱语义是"你忘了"，而压缩档失败的含义是"你还没练到能在 53 秒里讲完"（MEM3-P1-2）。
   不改这一条的话 box 2/3 会震荡，归档在概率上不可达 —— 和裁决⑤ 刚修好的是同一个 bug。
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
from pathlib import Path

from . import config as cfgmod
from . import store

# ---------------------------------------------------------------- 常数与配置

# box_days / cap：MEM3-P2-3。v2 的 `cap = horizon//2` 让 box 4/5 塌成一档；v3 的
# `round(h*0.25)` 在 h=10 时因为 banker's rounding 得 2，把 box 2–5 全压平（比 v2 更糟）。
# 定稿：horizon 默认 20（= 没有排期面试 = 不紧急，用自然阶梯），取整用 int(x+0.5)。
# 真有面试在 D 天内时由 backlog 的动态化给 cap = D//4，语义正确 —— 那时才该压缩间隔，
# 而不是由一个"假装人人都有面试"的默认常数来压。
BOX_DAYS = [1, 2, 3, 5, 8]

# 稿的墙钟语速。**这是墙钟不是发声语速**（= tts_rate 145 × 期望 speaking_ratio 0.85 ≈ 123，
# 与 217 条实测的隐含中位 120 吻合）。别再拿 TTS 的合成语速来改它：TTS 几乎没有停顿，
# 对它发声语速==墙钟语速，人不是，把停顿算两次会让每一条的 fluency 永久 2–3 分（MEM3-P1-1）。
TARGET_WPM = 120

VARIANT_TYPES = ("original", "paraphrase", "constraint", "truncate", "transfer")
# 施加了物理约束（更短的时间）的档。这几档：时长门 1.05×、覆盖门 3、r 不成旗、fail 不降箱。
CONSTRAINED = ("constraint", "truncate", "transfer")

VERDICTS = [(4.5, "Strong Hire"), (4.0, "Hire"), (3.4, "Lean"), (0.0, "No Hire")]

_DEFAULTS = {
    "pass_total": 3.6,
    "pass_streak": 2,
    "daily_cap": 6,
    "new_cap": 2,
    "review_cap": 4,
    "per_interview_cap": 2,
    "session_minutes": 25,
    "horizon_days": 20,
    "calibrating": True,
    "calibrate_takes": 20,
    "calibrate_days": 3,
    "tts_voice": "Samantha",
    # 范读的语速。任务书按记忆专家定 120（= 目标墙钟语速），让范读和 target_sec 同源：
    # 145 的范读会比 target_sec 短一截，等于给她示范一个比目标更快的节奏 —— 而她的病正是赶。
    # spec §7 写的"tts 145 不要再动"针对的是 R1 的推导常数，那一条在 TARGET_WPM 上兑现了。
    "tts_rate": 120,
    "r_flag_threshold": 0.6,
    # r > 0.6 这个阈值目前零观测数据支撑（FEY-v3 §5②）。攒够 N 个 paraphrase take 之前
    # 它只报警不阻断 —— 和 pass_total 走 calibrating 是同一条理由：没有分布就不设闸。
    "r_soft_until_takes": 20,
    "overdue_cap_days": 7,
    "fail_streak_rewrite": 3,
    "snooze_days": 3,
}


def cfg_recite(cfg: dict) -> dict:
    out = dict(_DEFAULTS)
    out.update(cfg.get("recite") or {})
    return out


def data_dir() -> Path:
    return cfgmod.resolve_path("data/recite")


def audio_dir(slot: str) -> Path:
    p = data_dir() / slot
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- schema

RECITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS recite_items (
  item_id     TEXT PRIMARY KEY,
  source      TEXT NOT NULL,
  company     TEXT, interview TEXT,
  kind        TEXT,
  question    TEXT, question_zh TEXT,
  script      TEXT NOT NULL,
  script_zh_gloss TEXT,
  key_points  TEXT,
  orig_text   TEXT,
  orig_audio  TEXT,
  critique_zh TEXT,
  target_sec  INTEGER,
  variants    TEXT,
  scope       TEXT DEFAULT 'unknown',
  quality     TEXT DEFAULT 'ok',
  deferred    INTEGER DEFAULT 0,
  script_history TEXT,
  created_at  TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS recite_state (
  item_id TEXT PRIMARY KEY,
  box INTEGER DEFAULT 1, due_on TEXT,
  n_takes INTEGER DEFAULT 0, n_pass INTEGER DEFAULT 0, streak INTEGER DEFAULT 0,
  last_fail INTEGER DEFAULT 0,
  best_total REAL, last_total REAL, last_fix TEXT, last_at TEXT,
  needs_rewrite INTEGER DEFAULT 0, snooze_until TEXT, archived INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS recite_takes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL, ts TEXT,
  mode TEXT,
  first_exposure INTEGER DEFAULT 0,
  variant TEXT DEFAULT 'original',
  why_line TEXT,
  audio_path TEXT, dur_sec REAL,
  transcript TEXT, metrics TEXT, verbatim_ratio REAL,
  scores TEXT, red_flags TEXT, missed_cues TEXT, fact_check TEXT,
  fix_next TEXT, one_line TEXT, verdict TEXT, judge_raw TEXT,
  passed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_recite_takes ON recite_takes(item_id, ts);
CREATE INDEX IF NOT EXISTS idx_recite_due ON recite_state(due_on, archived);
"""

# 全部 additive（store._migrate 的既有模式）。CREATE TABLE IF NOT EXISTS 对已存在的表是 no-op，
# 所以后加的列必须 ALTER 进来 —— 这张表 8/29 才建，但导入过一轮之后再加列就是迁移+重导。
_RECITE_MIGRATIONS = [
    ("recite_items", "story_id", "TEXT"),           # 映射表 §2 的故事编号，撞车检测 [IV-P0-1]
    ("recite_items", "link_group", "TEXT"),         # 互咬卡分组，口径必须一致       [PM-问题3]
    ("recite_items", "delivery_notes", "TEXT"),     # json, form 型, 每次只显 1 条   [MEM2-P1-3]
    ("recite_items", "why_q", "TEXT"),              # 从因果型 content 派生          [FEY-A2]
    ("recite_items", "why_ref", "TEXT"),
    ("recite_items", "gloss_precord", "INTEGER DEFAULT 0"),   # 箭头式，录前可展开    [裁决⑦]
    ("recite_items", "pace", "TEXT"),               # 'deliberate' = 刻意慢的短答    [FEY-R1]
    ("recite_items", "debrief_line", "TEXT"),       # 面试官会写下的那句            [PM-§B/R11]
    ("recite_items", "followup_q", "TEXT"),
    ("recite_items", "speaker_name", "TEXT"),       # 这场面试官叫她什么（Lisa/Joy） [PM-问题5.3]
    ("recite_items", "asr_dirty", "INTEGER DEFAULT 0"),       # orig_text 有 ASR 坏词 [PATCH 12]
    ("recite_items", "extra_cues", "TEXT"),         # [:4] 截掉的 content，灰字加分项 [PM-§C-4]
    ("recite_items", "dialog", "TEXT"),             # json turns：提问→回答→追问全往返（她的要求：
                                                    # follow-up 不许从上下文里切走，8/31）
    ("recite_items", "dialog_audio", "TEXT"),       # data/recite/dialog/<id>.mp3，同一窗口的原声
    ("recite_state", "exposed", "INTEGER DEFAULT 0"),         # 曝光轮由它驱动      [MEM2-P1-4b]
    ("recite_state", "exposure_stage", "INTEGER DEFAULT 0"),  # A0 可中断可恢复      [PM-问题5.1]
    ("recite_state", "exposure_at", "TEXT"),        # A0-② 的时刻，>2h 恢复要重走    [MEM3-3b]
    ("recite_takes", "variant_type", "TEXT DEFAULT 'original'"),
    ("recite_takes", "r_prev", "REAL"),             # vs 她上一次转写的 4-gram 重合   [MEM-H]
    ("recite_takes", "self_est", "REAL"),           # 她的自评，反馈淡出实验要用
    ("recite_takes", "variant_sec", "INTEGER"),
    ("recite_takes", "coverage", "INTEGER"),
    ("recite_takes", "total", "REAL"),
    ("recite_takes", "hard_ok", "INTEGER DEFAULT 0"),
    ("recite_takes", "gate_note", "TEXT"),
]

_ready: set[int] = set()


def ensure_schema(conn) -> None:
    """幂等。按连接对象只跑一次（DDL 便宜但不是免费，且这条路径在每个请求上）。"""
    if id(conn) in _ready:
        return
    conn.executescript(RECITE_SCHEMA)
    store._migrate(conn, _RECITE_MIGRATIONS)
    conn.commit()
    _ready.add(id(conn))


def connect(cfg: dict):
    conn = store.connect_user(cfg)
    ensure_schema(conn)
    return conn


# ---------------------------------------------------------------- key_points 四类分型

# 存量语料里 0/217 条的 key_points 是已分型对象（PM-§C-3 实测），全是裸字符串。所以今晚
# 左栏（content 要点卡）和唯一的硬门（coverage 分母）**同时**压在下面这个启发式上。
# 内容侧的 R2 会让复盘 agent 直接产出 `t`，那之后这里就只是兜底；在那之前它是单点故障，
# 所以 recite_import 会打印分型分布并在 content < 3 的条目超过 15% 时亮红灯。

# form = 交付指令（说话方式），不是内容拍子。它是 Wulf 的 constrained-action：执行当下
# 持有自我监控指令会抑制自动化、制造犹豫和改口 —— 正是她的病。所以它不进 coverage 分母，
# 只喂判官的 structure 段 + 练习页每次显示 1 条（外部焦点改写后的）。
_FORM_RX = re.compile(
    r"删掉|不要|不用|不许|禁止|别说|别用|别再|每句|吸气|主语|开场|换成|保留|说完|改成|"
    r"节奏|语速|停顿|句子.{0,4}(短|长|拆)|拆成|先说|一句话说|中途|改口|重起|眼神|语气")
# forbid = 她真的说过并因此失分的原话。转确定性正则是本轮最划算的一处（FEY-B3）：
# 比任何 LLM 判断都可靠、都便宜，每命中一次都是一条她立刻听得懂的反馈。
_FORBID_RX = re.compile(r"不许说|绝不能说|一个字都不要|禁止说出|千万别说|不要再说|不能出现")
# canon = 必须逐字命中的口径（年限 / 数字 / 打磨过的收尾）。进 coverage，但从 r 的分母剔除。
_CANON_RX = re.compile(r"CANON|口径|🔴|逐字|原话不动|按原文")
# 撰稿注记：给复盘作者看的，不是给她看的。直接丢弃（PATCH 3）。
_NOTE_RX = re.compile(r"\.md\b|§|复盘|见上文|见下文|待确认|TODO")

_NUM_RX = re.compile(r"\d")
_CAUSAL_RX = re.compile(r"因为|所以|导致|换来|代价|取舍|权衡|不是.{0,8}而是|⇒|→|才|否则")


def classify_key_point(raw) -> dict:
    """裸字符串 / 已分型对象 → {"t", "text", "probe"}。存量兼容用确定性前缀推断。"""
    if isinstance(raw, dict):
        t = (raw.get("t") or "").strip().lower()
        if t in ("content", "form", "forbid", "canon"):
            return {"t": t, "text": str(raw.get("text") or "").strip(),
                    "probe": str(raw.get("probe") or "").strip()}
        raw = str(raw.get("text") or "")
    s = str(raw or "").strip()
    if not s:
        return {"t": "drop", "text": "", "probe": ""}
    # canon 先判：含 CANON 的句子往往也含"不要"，顺序反了会被 form 吃掉
    if _CANON_RX.search(s):
        return {"t": "canon", "text": s, "probe": _quoted(s)}
    if _FORBID_RX.search(s):
        return {"t": "forbid", "text": s, "probe": _quoted(s)}
    if _NOTE_RX.search(s):
        return {"t": "drop", "text": s, "probe": ""}
    if _FORM_RX.search(s):
        return {"t": "form", "text": s, "probe": ""}
    return {"t": "content", "text": s, "probe": ""}


def _quoted(s: str) -> str:
    """从一条要点里抠出被引号包住的英文原句 —— forbid 型的 probe。"""
    m = re.search(r"[\"“'‘「『]([A-Za-z][^\"”'’」』]{6,120})[\"”'’」』]", s)
    return m.group(1).strip() if m else ""


def parse_key_points(raw) -> list[dict]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = [raw]
    if not isinstance(raw, list):
        return []
    out = [classify_key_point(x) for x in raw]
    return [k for k in out if k["t"] != "drop" and k["text"]]


def scored_cues(points: list[dict]) -> tuple[list[dict], list[dict]]:
    """coverage 的分母（≤4 条）+ 被截掉的加分项。

    PM-§C-4：46% 的条目有 ≥5 条 content，若按数组序 `[:4]` 硬截，被截掉的那条既不显示在
    左栏、也不进分母 —— 她漏说了没有任何代价，而那可能正是数字拍或落点拍。所以截取顺序
    显式定义为 canon 优先 → 含数字/因果词的 → 其余；被截掉的**仍显示在左栏**（灰色，标
    「加分项」），只是不进分母。保住 Cowan ~4 的工作记忆约束，又不让内容凭数组序消失。
    """
    pool = [k for k in points if k["t"] in ("content", "canon")]

    def rank(k):
        if k["t"] == "canon":
            return 0
        if _NUM_RX.search(k["text"]) or _CAUSAL_RX.search(k["text"]):
            return 1
        return 2

    ordered = sorted(pool, key=lambda k: (rank(k), pool.index(k)))
    return ordered[:4], ordered[4:]


def coverage_score(scored: list[dict], missed: list[str]) -> int:
    """coverage 由代码算，判官只输出 missed_cues（IV-P1-4a）。

    它是唯一**事实性**维度（说没说到），不是判断性维度 —— 少一个判官漂移面，硬门才可审计。
    """
    n = len(scored)
    if n == 0:
        return 3
    hit = max(0, n - len([m for m in missed if m]))
    frac = hit / n
    if hit == n:
        return 5
    return 4 if frac >= 0.8 else 3 if frac >= 0.6 else 2 if frac >= 0.4 else 1


# ---------------------------------------------------------------- 派生指标

_WORD_RX = re.compile(r"[A-Za-z0-9']+")
_CANON_SPAN_RX = re.compile(r"«([^»]*)»")


def words(text: str) -> list[str]:
    return _WORD_RX.findall((text or "").lower())


def target_sec_from_words(n_words: int) -> int:
    """R1：target_sec 不再手估，由词数导出，取 5 秒的整数倍。

    120 是**墙钟**语速（见 TARGET_WPM 的注释）。`pace: "deliberate"` 的条目不走这里 ——
    它的时长是刻意的不是估的（FEY-v3 §3：a03 53 词手写 35 秒靠停顿制造重量，按 145 导出
    会得 20 秒、dur_gate 28 秒，她按设计意图交付的 35 秒会被判 overtime 硬 fail）。
    """
    if n_words <= 0:
        return 45
    return max(20, int(round(n_words / TARGET_WPM * 60 / 5) * 5))


def strip_canon(script: str) -> str:
    """把 «…» 标注的逐字跨度从 script 里剥掉 —— 它们本来就该一样，不该进 r 的分母（FEY-B4）。"""
    return _CANON_SPAN_RX.sub(" ", script or "")


def canon_spans(script: str) -> list[str]:
    return [s.strip() for s in _CANON_SPAN_RX.findall(script or "") if s.strip()]


def _ngrams(ws: list[str], n: int = 4) -> set[tuple]:
    return {tuple(ws[i:i + n]) for i in range(len(ws) - n + 1)} if len(ws) >= n else set()


def verbatim_ratio(transcript: str, script: str) -> float:
    """r = |4g(她说的) ∩ 4g(稿)| / |4g(她说的)|，逐字跨度已从稿里剥离。

    **r 单独测不出内化和背诵的区别**（两者都让 r 上升）。判别量是 r 与 r_prev 的联合走势：
        r 涨 + r_prev 涨 = 收敛到稿        → 背诵
        r 平 + r_prev 涨 = 收敛到她自己的说法 → 内化，正是目标
    """
    t = _ngrams(words(transcript))
    if not t:
        return 0.0
    s = _ngrams(words(strip_canon(script)))
    return round(len(t & s) / len(t), 4)


def audio_metrics(path: Path) -> dict:
    """ffprobe + silencedetect。失败一律返回可用的默认值 —— 指标缺失绝不能吃掉一次录音。"""
    out = {"duration": 0.0, "speaking_sec": 0.0, "pauses_over_2s": 0,
           "longest_pause_sec": 0.0, "speaking_ratio": 0.0}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=60)
        out["duration"] = round(float((r.stdout or "0").strip() or 0), 2)
    except Exception:
        return out
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
             "-af", "silencedetect=noise=-32dB:d=0.6", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120)
        log = (r.stderr or "")
        starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log)]
        durs = [float(x) for x in re.findall(r"silence_duration: ([\d.]+)", log)]
        silence = sum(durs)
        out["pauses_over_2s"] = len([d for d in durs if d >= 2.0])
        out["longest_pause_sec"] = round(max(durs), 2) if durs else 0.0
        out["speaking_sec"] = round(max(0.0, out["duration"] - silence), 2)
        _ = starts
    except Exception:
        out["speaking_sec"] = out["duration"]
    if out["duration"] > 0:
        out["speaking_ratio"] = round(out["speaking_sec"] / out["duration"], 3)
    return out


def wpm(transcript: str, metrics: dict) -> float:
    """转写词数 / 发声分钟。**只喂 fluency 软分**，硬门里没有任何东西看它（裁决④）。"""
    spk = float(metrics.get("speaking_sec") or 0) or float(metrics.get("duration") or 0)
    if spk <= 0:
        return 0.0
    return round(len(words(transcript)) / (spk / 60.0), 1)


_HARD_FILLER = re.compile(r"\b(um|uh|erm)\b", re.I)
_SOFT_FILLER = re.compile(r"\b(like|you know|kind of|sort of|basically|actually)\b", re.I)


def filler_counts(transcript: str) -> dict:
    """UI 标"参考值"，不进 pass 判定 —— Parakeet 会清洗 disfluency，这个数系统性偏低。"""
    return {"hard_filler": len(_HARD_FILLER.findall(transcript or "")),
            "soft_filler": len(_SOFT_FILLER.findall(transcript or ""))}


# ---------------------------------------------------------------- 变体

def variant_for(item: dict, n_takes: int) -> dict:
    """第 2 次起按 n_takes 轮换。返回 {type, q, sec, hint}。"""
    vs = item.get("variants")
    if isinstance(vs, str):
        try:
            vs = json.loads(vs)
        except Exception:
            vs = []
    vs = [v for v in (vs or []) if isinstance(v, dict)]
    base = {"type": "original", "q": item.get("question") or "",
            "sec": int(item.get("target_sec") or 60), "hint": ""}
    if not vs or n_takes <= 0:
        return base
    pool = [base] + vs
    return pool[n_takes % len(pool)]


def gates(cfg_r: dict, variant_type: str, variant_sec: int, target_sec: int,
          calibrating: bool, n_paraphrase_takes: int) -> dict:
    """时长门 / 覆盖门 / r 是否成旗 —— 全部由变体分型驱动（PATCH 2）。

    FEY-A4 的两半必须成对，这是最容易被砍成一半的地方：
      · `1.05 × variant_sec` 而不是 `1.4 × variant_sec`（v2 的 1.4×0.7=0.98，允许时长几乎等于
        原始目标 —— 她可以完全无视压缩指令照原样讲，照样 pass，压缩档是装饰）；
      · 覆盖门同时松到 3（不松的话 80% 的要点塞进 73% 的词里唯一解是加速）。
    """
    if variant_type in CONSTRAINED:
        sec = max(10, int(variant_sec or round(0.7 * target_sec)))
        return {"dur_gate": round(1.05 * sec, 1), "coverage_gate": 3,
                "r_is_flag": False, "demotes_on_fail": False, "sec": sec}
    hard_r = (variant_type == "paraphrase" and not calibrating
              and n_paraphrase_takes >= int(cfg_r["r_soft_until_takes"]))
    return {"dur_gate": round(1.40 * target_sec, 1), "coverage_gate": 4,
            "r_is_flag": hard_r, "demotes_on_fail": True, "sec": target_sec}


# ---------------------------------------------------------------- 判定

def verdict_word(total: float) -> str:
    for lo, w in VERDICTS:
        if total >= lo:
            return w
    return "No Hire"


def forbid_hits(transcript: str, points: list[dict]) -> list[str]:
    """确定性正则红旗。probe 是她真的说过、并因此失分的英文原句。"""
    t = " ".join(words(transcript))
    out = []
    for k in points:
        if k["t"] != "forbid" or not k.get("probe"):
            continue
        p = " ".join(words(k["probe"]))
        if p and p in t:
            out.append(k["probe"])
    return out


def judge_take(*, cfg_r: dict, item: dict, points: list[dict], scored: list[dict],
               transcript: str, metrics: dict, review: dict, why_line: str,
               variant_type: str, variant_sec: int, mode: str, first_exposure: bool,
               box: int, calibrating: bool, r: float, n_paraphrase_takes: int,
               number_flags: list[str]) -> dict:
    """把确定性指标 + LLM 判官输出合成一次裁定。返回落库用的 dict。

    硬门四条全部与录音条件无关、可复算、可审计。软分是 LLM 的四维均值（coverage 由代码算，
    但它进均值 —— 它是分数里唯一有事实基础的那一维）。
    """
    target_sec = int(item.get("target_sec") or 60)
    g = gates(cfg_r, variant_type, variant_sec, target_sec, calibrating, n_paraphrase_takes)
    missed = [str(m) for m in (review.get("missed_cues") or []) if str(m).strip()]
    cov = coverage_score(scored, missed)

    duration = float(metrics.get("duration") or 0)
    fb = forbid_hits(transcript, points)
    reciting = bool(g["r_is_flag"] and r > float(cfg_r["r_flag_threshold"]))

    hard_flags = []
    if fb and not calibrating:
        hard_flags.append(f"说了不该说的：{fb[0]}")
    if reciting:
        hard_flags.append("reciting_under_variant")
    # 口径检查独立成条、不进分数（MEM-D）；只有硬档（banned_phrases）才进硬门。
    hard_flags += [f for f in number_flags if f.startswith("banned:")] if not calibrating else []

    why_submitted = bool((why_line or "").strip())
    soft = review.get("scores") or {}
    dims = []
    for d in ("structure", "concision", "fluency", "why_depth"):
        try:
            dims.append(float(soft.get(d)))
        except (TypeError, ValueError):
            pass
    # 判官挂了不能变成"过了"。coverage 是代码算的、永远有值，用它单独当均值会让一次
    # ASR/LLM 故障静默地记成一次 pass —— 那比不给分更糟，因为她会照着一个假的曲线校准。
    judged = bool(dims)
    llm_mean = round(sum(dims + [float(cov)]) / (len(dims) + 1), 2) if judged else float(cov)

    hard_ok = (cov >= g["coverage_gate"] and duration <= g["dur_gate"]
               and why_submitted and not hard_flags)
    why_depth = float(soft.get("why_depth") or 0)
    soft_ok = (judged and llm_mean >= float(cfg_r["pass_total"])
               and (box < 3 or why_depth >= 4))
    passed = bool(hard_ok and soft_ok and mode == "recall" and not first_exposure)

    notes = []
    if not judged:
        notes.append("评分没跑出来，录音已存 —— 这一次不计过关")
    if cov < g["coverage_gate"]:
        notes.append(f"覆盖 {cov} < {g['coverage_gate']}")
    if duration > g["dur_gate"]:
        notes.append(f"{duration:.0f}s > 上限 {g['dur_gate']:.0f}s")
    if not why_submitted:
        notes.append("没写 why")
    notes += hard_flags
    # undertime 不是失败（裁决③）：35 秒说完且要点全中是一次好回答。她的病是超时、绕圈，
    # 把"说短了"设成失败等于训练她注水，方向反了。只在卡片上说一句"这个长度可以"。
    if duration and duration < 0.7 * g["sec"] and cov >= g["coverage_gate"]:
        notes.append(f"{duration:.0f}s / 目标 {g['sec']}s — 要点全中，这个长度可以")

    return {
        "coverage": cov, "total": llm_mean, "verdict": verdict_word(llm_mean),
        "hard_ok": int(hard_ok), "passed": int(passed),
        "gate_note": " · ".join(notes), "dur_gate": g["dur_gate"],
        "coverage_gate": g["coverage_gate"], "r_is_flag": g["r_is_flag"],
        "demotes_on_fail": g["demotes_on_fail"],
        "red_flags": hard_flags, "forbid_hits": fb, "missed_cues": missed,
    }


# ---------------------------------------------------------------- 排程

def scaled_day(box: int, horizon_days: int) -> int:
    cap = max(1, int(horizon_days * 0.25 + 0.5))
    idx = max(0, min(len(BOX_DAYS) - 1, int(box) - 1))
    return min(BOX_DAYS[idx], cap)


def _today() -> str:
    return store.la_today()


def _plus(days: int) -> str:
    d = datetime.date.fromisoformat(_today()) + datetime.timedelta(days=max(0, int(days)))
    return d.isoformat()


def apply_outcome(conn, cfg_r: dict, item_id: str, *, passed: bool, first_exposure: bool,
                  variant_type: str, same_day: bool, hard_red: bool,
                  demotes_on_fail: bool, total: float, fix_next: str) -> dict:
    """晋箱 / 降箱 / due。写 recite_state，返回新状态。

    压缩档 fail 不降箱（`demotes_on_fail=False`）：它是拉伸练习不是保持测量。这完整保留了
    FEY-A4 想要的那个逼迫（她拿不到 pass，晋不了箱），只是不把她往回推 —— 不然 box 2/3
    会震荡，box 4/5 和归档在概率上不可达。
    """
    st = get_state(conn, item_id)
    box, streak = int(st["box"] or 1), int(st["streak"] or 0)
    calibrating = bool(cfg_r["calibrating"])
    horizon = int(cfg_r["horizon_days"])

    if first_exposure:
        # 曝光轮的基线 take：不 pass、不 reset box、不进 streak，只写基线分，due = 明天。
        due, n_pass = _plus(1), int(st["n_pass"] or 0)
        last_fail = int(st["last_fail"] or 0)
    elif passed:
        n_pass = int(st["n_pass"] or 0) + 1
        last_fail = 0
        if not same_day:
            streak += 1
            recent = [r["variant_type"] for r in conn.execute(
                "SELECT variant_type FROM recite_takes WHERE item_id=? AND passed=1"
                " ORDER BY id DESC LIMIT 2", (item_id,))]
            if streak >= int(cfg_r["pass_streak"]) and any(
                    (v or "original") != "original" for v in recent):
                box += 1
                streak = 0
        due = _plus(scaled_day(box, horizon))
    else:
        n_pass = int(st["n_pass"] or 0)
        streak = 0
        last_fail = 1
        if not calibrating and demotes_on_fail:
            box = 1 if (int(st["last_fail"] or 0) or hard_red) else max(1, box - 1)
        due = _plus(1)

    n_takes = int(st["n_takes"] or 0) + 1
    best = st["best_total"]
    best = total if best is None else max(float(best), total)
    needs_rewrite = int(st["needs_rewrite"] or 0)
    snooze = st["snooze_until"]
    if not passed and not first_exposure:
        recent_fail = [r["passed"] for r in conn.execute(
            "SELECT passed FROM recite_takes WHERE item_id=? AND first_exposure=0"
            " ORDER BY id DESC LIMIT ?", (item_id, int(cfg_r["fail_streak_rewrite"])))]
        if (len(recent_fail) >= int(cfg_r["fail_streak_rewrite"])
                and not any(recent_fail)):
            needs_rewrite = 1
            snooze = _plus(int(cfg_r["snooze_days"]))
            due = snooze
            _append_rough(item_id, "连续 fail，稿要改")

    conn.execute(
        "UPDATE recite_state SET box=?, streak=?, due_on=?, n_takes=?, n_pass=?,"
        " last_fail=?, best_total=?, last_total=?, last_fix=?, last_at=?,"
        " needs_rewrite=?, snooze_until=? WHERE item_id=?",
        (box, streak, due, n_takes, n_pass, last_fail, best, total, fix_next,
         store.la_now_iso(), needs_rewrite, snooze, item_id))
    conn.commit()
    return {"box": box, "streak": streak, "due_on": due, "n_takes": n_takes,
            "needs_rewrite": needs_rewrite}


def _append_rough(item_id: str, why: str) -> None:
    try:
        p = data_dir() / "rough_queue.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"- [ ] `{item_id}` — {why}（{_today()}）\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 队列

def get_state(conn, item_id: str):
    row = conn.execute("SELECT * FROM recite_state WHERE item_id=?", (item_id,)).fetchone()
    if row is None:
        conn.execute("INSERT OR IGNORE INTO recite_state(item_id, due_on) VALUES(?,?)",
                     (item_id, _today()))
        conn.commit()
        row = conn.execute("SELECT * FROM recite_state WHERE item_id=?", (item_id,)).fetchone()
    return row


def today_queue(conn, cfg: dict) -> list[dict]:
    """今日队列：due 的复习 + 至多 new_cap 条从未练过的，同一场面试至多 per_interview_cap 条。

    `new_cap: 2` 不是优化是必须的（MEM-P2-5c）：没有 new/review 配额时，Day 1 她拿到 6 条全新
    条目 → 全部首次曝光 → 全部不计过 → 全部回到明天；Day 2 再叠 6 条，两天锁死。而首次曝光是
    认知负荷最高的部分，一天来 6 次会直接耗竭。
    """
    c = cfg_recite(cfg)
    ensure_schema(conn)
    today = _today()
    rows = [dict(r) for r in conn.execute(
        "SELECT i.*, s.box, s.due_on, s.n_takes, s.n_pass, s.streak, s.last_total,"
        "       s.last_fix, s.exposed, s.exposure_stage, s.archived, s.needs_rewrite,"
        "       s.snooze_until"
        " FROM recite_items i LEFT JOIN recite_state s ON s.item_id=i.item_id"
        " WHERE COALESCE(i.deferred,0)=0 AND COALESCE(s.archived,0)=0"
        "   AND COALESCE(i.scope,'unknown') IN ('ok','unknown')"
        "   AND COALESCE(i.quality,'ok')='ok'"
        "   AND (s.snooze_until IS NULL OR s.snooze_until <= ?)"
        " ORDER BY COALESCE(s.due_on, i.created_at)", (today,))]

    # overdue 封顶：欠了超过 7 天的就地重置成今天到期。不封顶的话，出差一周回来会看到
    # 一屏按"欠了 23 天"排序的条目 —— 那个排序对学习没有任何意义，只制造愧疚。
    floor = _plus(-int(c["overdue_cap_days"]))
    stale = [r["item_id"] for r in rows if (r.get("due_on") or today) < floor]
    if stale:
        conn.executemany("UPDATE recite_state SET due_on=? WHERE item_id=?",
                         [(today, i) for i in stale])
        conn.commit()
        for r in rows:
            if r["item_id"] in stale:
                r["due_on"] = today

    due = [r for r in rows if (r.get("n_takes") or 0) > 0
           and (r.get("due_on") or today) <= today]
    new = [r for r in rows if not (r.get("n_takes") or 0)]
    due.sort(key=lambda r: (r.get("due_on") or today, r["item_id"]))

    out: list[dict] = []
    per_iv: dict[str, int] = {}

    def take(pool, limit):
        for r in pool:
            if len(out) >= int(c["daily_cap"]) or limit <= 0:
                break
            iv = r.get("interview") or ""
            if per_iv.get(iv, 0) >= int(c["per_interview_cap"]):
                continue
            per_iv[iv] = per_iv.get(iv, 0) + 1
            out.append(r)
            limit -= 1

    take(due, int(c["review_cap"]))
    take(new, int(c["new_cap"]))
    if len(out) < int(c["daily_cap"]):        # due 不足才用更多新条目补齐
        take([r for r in due + new if r not in out], int(c["daily_cap"]) - len(out))
    return out


def calibrating_now(conn, cfg: dict) -> bool:
    """前 N take 或前 M 天，先到为准。校准期 fail 不改 box，UI 说「过关线校准中」不说「不过」。"""
    c = cfg_recite(cfg)
    if not c["calibrating"]:
        return False
    row = conn.execute(
        "SELECT COUNT(*) n, MIN(ts) t0 FROM recite_takes WHERE first_exposure=0").fetchone()
    if (row["n"] or 0) >= int(c["calibrate_takes"]):
        return False
    if row["t0"]:
        try:
            d0 = datetime.date.fromisoformat(str(row["t0"])[:10])
            if (datetime.date.fromisoformat(_today()) - d0).days >= int(c["calibrate_days"]):
                return False
        except Exception:
            pass
    return True


def histogram(conn) -> dict:
    """页脚校准直方图。

    分母 `first_exposure=0 AND mode='recall'`（PATCH 9）：first_exposure 按定义永不 pass，在
    new_cap=2 下占日量 33%；把它混进分母会让第一周的过关率被系统性压低三分之一，而那正是
    决定 pass_total 往哪调的那一周 —— 结论会是「判官太严」于是往下调，真实原因只是分母脏。
    **校准只有一次机会。**

    再按 variant_type 分开（MEM3-3a）：原版档（覆盖门 4 / 时长门 1.4×）和压缩档（3 / 1.05×）
    是两套不同判据，混池的过关率第二周起会被变体比例带偏。
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT variant_type, passed, total, verbatim_ratio, r_prev, dur_sec"
        " FROM recite_takes WHERE first_exposure=0 AND mode='recall' AND total IS NOT NULL"
        " ORDER BY id DESC LIMIT 40")]
    def bucket(name, pool):
        if not pool:
            return None
        return {"name": name, "n": len(pool),
                "pass_rate": round(100 * sum(1 for r in pool if r["passed"]) / len(pool)),
                "p60": round(sorted(float(r["total"] or 0) for r in pool)[int(len(pool) * .6) - 1
                                                                          if len(pool) > 1 else 0], 2)}
    orig = [r for r in rows if (r["variant_type"] or "original") in ("original", "paraphrase")]
    var = [r for r in rows if (r["variant_type"] or "original") in CONSTRAINED]
    rs = [round(float(r["verbatim_ratio"] or 0), 2) for r in rows[:3][::-1]]
    return {"buckets": [b for b in (bucket("原版", orig), bucket("变体", var)) if b],
            "r_trend": rs,
            "spoken_min": round(sum(float(r["dur_sec"] or 0) for r in rows) / 60, 1)}


def prev_transcript(conn, item_id: str, exclude_take_id: int | None = None) -> str:
    """上一次转写 —— r_prev 的对照物。A0 基线 take 排除（裁决⑤/FEY-A1）：它发生在读完英文稿
    30 秒后，结构性偏高，混进序列会让曲线自带一个假的下降趋势。"""
    sql = ("SELECT transcript FROM recite_takes WHERE item_id=? AND first_exposure=0"
           " AND transcript IS NOT NULL AND transcript!=''")
    args: list = [item_id]
    if exclude_take_id:
        sql += " AND id<>?"
        args.append(exclude_take_id)
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
    return (row["transcript"] if row else "") or ""


# ---------------------------------------------------------------- 口径白名单

def load_numbers() -> dict:
    """data/recite_numbers.yaml（由 tools/recite_numbers_sync.py 从映射表 §〇 生成）。

    白名单不在代码里硬编码：映射表是故事库唯一事实源，代码里的硬编码副本必然漂移 ——
    而口径漂移正是这个功能要防的东西。
    """
    import yaml
    p = cfgmod.resolve_path("data/recite_numbers.yaml")
    if not p.exists():
        return {"allowed": [], "banned_phrases": []}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"allowed": [], "banned_phrases": []}


_PCT_RX = re.compile(r"(\d{1,3})\s*(?:percent|%)", re.I)


def fact_check(transcript: str, item: dict) -> list[dict]:
    """📋 事实核对 —— 独立成条，不进 total，不进硬门（除非 banned 硬档）。

    把 delivery 分数和口径正确性混进同一个数，会让两种完全不同的错误共享同一条信任通道；
    而误报花掉的不是时间是信任，信任是这个产品唯一的依从性资产（MEM-D）。
    措辞也是「上次你说的是 30%」而不是「越界」—— 记忆锚点校准，不是一次扣分（FEY-D）。
    """
    wl = load_numbers()
    allowed = {str(a.get("value")) for a in (wl.get("allowed") or [])}
    out = []
    for m in _PCT_RX.finditer(transcript or ""):
        v = m.group(1)
        if allowed and v not in allowed:
            ref = next((a for a in (wl.get("allowed") or [])
                        if str(a.get("story") or "") == str(item.get("story_id") or "")), None)
            out.append({"kind": "number", "level": "soft", "said": f"{v}%",
                        "note": (f"上次你说的是 {ref['value']}%（{ref.get('canonical', '')}）"
                                 if ref else "这个数不在口径白名单里"),
                        "provenance": (ref or {}).get("provenance", "")})
    for bad in (wl.get("banned_phrases") or []):
        if bad and bad.lower() in (transcript or "").lower():
            out.append({"kind": "banned", "level": "hard", "said": bad,
                        "note": "这个说法已经从口径里删掉了", "provenance": ""})
    return out


def number_flag_strings(checks: list[dict]) -> list[str]:
    return [f"banned:{c['said']}" for c in checks if c.get("level") == "hard"]


__all__ = [n for n in dir() if not n.startswith("_")]
