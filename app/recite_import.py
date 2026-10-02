"""念稿练习 · 导入器（幂等 CLI）。

    python -m app.recite_import --from-recaps <dir>     # 主力通道
    python -m app.recite_import --from-playbook         # BQ playbook 粗导入（标 rough）
    python -m app.recite_import --from-openers          # 6 条开口微稿
    python -m app.recite_import --all <dir>

**可重跑**：同一个 item_id 走 UPSERT，只覆盖内容字段，绝不动 recite_state（她的 box / streak /
due 是练出来的，重导内容不该清掉）。原声拷贝和范读生成都跳过已存在的文件。内容侧还在返工
这批 recap.json，所以全量导入由主会话在返工完成后再跑一遍，这里必须能跑第二遍。

**准入只看 `script != null`**（裁决⑧）。`kind` 过滤会丢掉 think-aloud 24 条 + other 2 条 =
26 条（12.5%），其中包含全样本最佳范本（哨兵不变式）和 Amazon 那场自标「本场最贵的一句」。
复盘 agent 已经替我们做了准入判断：不可练的条目显式写 `script: null` + `script_null_reason`。
`kind` 保留用于列表页筛选和 variants 生成策略，不再承担准入职责。

**今晚唯一的单点故障是 key_points 分型器**（PM-§C-3）：语料里 0/217 条是已分型对象，而左栏
（content 要点卡）和唯一的硬门（coverage 分母）同时压在它上面。所以本文件跑完必须打印分型
分布，并在 `content < 3 条的 item 占比 > 15%` 时亮红灯 —— 和「imported < 6 是红灯」同级。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as cfgmod
from . import recite
from . import store

MIN_TARGET, MAX_TARGET = 35, 240


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return re.sub(r"-+", "-", s) or "x"


# ---------------------------------------------------------------- gloss 判据

_ARROW = "→"


def gloss_precord_ok(gloss: str) -> bool:
    """录前可展开的 gloss（裁决⑦ 的唯一例外，确定性三行）。

    实测 27 条箭头式里**只有 5 条**通过这个判据（arrow gloss 中位 273 字，比散文式的 181
    还长），也就是 gloss_precord ≈ 2%，R9 返工完成前近似恒为 0（PM-§C-2）。这不是错误 ——
    守门就该守住；不合格的一律录后解锁，等 R9 重出。别按 27 条去做 UI 分支。
    """
    g = (gloss or "").strip()
    return bool(g and _ARROW in g and g.count(_ARROW) + 1 <= 6 and len(g) <= 200)


def gloss_problems(gloss: str) -> list[str]:
    """gloss 的确定性质检（进 rough_queue，不阻断导入）。"""
    g = (gloss or "").strip()
    out = []
    if not g:
        return ["gloss 为空"]
    for a, b in (("（", "）"), ("(", ")"), ("「", "」"), ("《", "》")):
        if g.count(a) != g.count(b):
            out.append(f"括号没配平 {a}{b}")
    if len(g) > 260:
        out.append(f"gloss {len(g)} 字，25 秒读不完")
    if _ARROW not in g:
        out.append("散文式 gloss（R9 待重出为箭头式）")
    # R8：gloss 里不许整句英文，否则「看意思免费」这一侧会漏掉英文。标识符级英文可留。
    for m in re.finditer(r"[A-Za-z][A-Za-z ,'’]{25,}", g):
        if len(m.group(0).split()) >= 5:
            out.append("gloss 含整句英文（R8）")
            break
    return out


# ---------------------------------------------------------------- why 问句

_CAUSAL = re.compile(r"因为|所以|导致|换来|代价|取舍|权衡|不是.{0,10}而是|⇒|→|才|否则|前提")

_GENERIC_WHY = [
    ("这个决定的代价是什么？", ""),
    ("你当时排除掉的方案是什么？为什么排除？", ""),
]


def derive_why(points: list[dict], item: dict) -> tuple[str, str]:
    """why 问句从**因果型 content 要点**派生（PATCH 7 / FEY-A2）。

    **禁止**从 `critique_zh` 派生。v2 的「这一遍你打算怎么避免上面这条？」不是 why 问句，
    是 how-will-you-fix-it 问句，三个后果：① 与 fix_next 完全同义（先让她猜一遍 fix_next，
    再给她看 fix_next）；② 测不到 why_depth（行为锚点要的是「理由 + 取舍 + 被排除的方案」）；
    ③ 把费曼位从内容挪到了交付，让 why 维变成 delivery 的第三次重复测量。
    兜底也永远不退回交付型问句。
    """
    for k in points:
        if k["t"] in ("content", "canon") and _CAUSAL.search(k["text"]):
            return (f"为什么是这样？——「{k['text']}」这一条背后的理由、代价，"
                    f"和你当时排除掉的那个方案。", k["text"])
    n = (len(item.get("id") or "") + len(item.get("question") or "")) % len(_GENERIC_WHY)
    q, ref = _GENERIC_WHY[n]
    return q, ref


# ---------------------------------------------------------------- 变体

def _para_question(item: dict) -> str:
    """同义问法。真实来源应是映射表 §2 的同一故事多题型映射；MVP 用 question_zh 起头。"""
    zh = (item.get("question_zh") or "").strip()
    return f"换个问法：{zh}" if zh else ""


def _too_similar(a: str, b: str) -> bool:
    """paraphrase 退化守卫（FEY-v3 §5①）。

    r 红旗现在全押在 paraphrase 档上，那么这个档的**质量**就成了红旗的前提。对
    technical-explain（语料主体）来说，同义问法大概率退化成 question_zh 的反译 —— 也就是
    同一个问题的翻译。对一个实质未变的问题判「你答得和原稿太像」是纯误伤，而且会教出
    「每次换个说法免得被抓」的同义改写表演。实质不同才发 paraphrase。
    """
    wa, wb = set(recite.words(a)), set(recite.words(b))
    if not wa or not wb:
        return True
    return len(wa & wb) / max(1, len(wa | wb)) > 0.55


def build_variants(item: dict, target_sec: int, kind: str) -> list[dict]:
    out: list[dict] = []
    if kind == "think-aloud":
        # 迁移目标是**句式**不是内容（IV-v3 §五1）：同义问法对一条不变式没有意义（问法只有
        # 一种），换题目复述同一套句式才是它的正确练法。transfer 在判定上跟 constraint 走同
        # 一支（r 不成旗 —— 句式复用正是目标；覆盖门 3 —— 换题后具体名词必然对不上）。
        out.append({"type": "transfer", "sec": target_sec,
                    "q": "同一套说法，套到另一道题上说一遍（LRU / LFU / escape room 任选）",
                    "hint": "内容会变，句式不变 —— 要练的是那个开口的形状"})
    else:
        pq = _para_question(item)
        if pq and not _too_similar(pq, item.get("question") or ""):
            out.append({"type": "paraphrase", "sec": target_sec, "q": pq, "hint": ""})
    sec = max(15, int(round(0.7 * target_sec / 5) * 5))
    out.append({"type": "constraint", "sec": sec, "q": item.get("question") or "",
                "hint": f"这次 {sec} 秒讲完"})
    # truncate：不等 R7（IV-v3 §四）。作者标注的语义断点在 272 条 JSON 里 0 条，且即便 R7
    # 落地也只覆盖最长的十几条。而 truncate 练的不是「作者选的那个断点」，是「在你实际拿到
    # 的时间里把话说完整」—— 真实的打断从来不落在作者标好的拍子上，落在面试官失去耐心的
    # 那一秒。有 truncate_at_sec 时用作者标注覆盖这个默认值（同一个字段，无迁移）。
    tsec = item.get("truncate_at_sec")
    if tsec or target_sec >= 75:
        out.append({"type": "truncate",
                    "sec": int(tsec) if tsec else int(round(0.55 * target_sec / 5) * 5),
                    "q": item.get("question") or "",
                    "hint": "面试官在这里插话了 —— 把话收完整，别抢着讲完剩下的"})
    return out


# ---------------------------------------------------------------- 撞车 / 分组

MAPPING = Path("/path/to/interview/workspace/behavior question/Story→题型映射表.md")

# 中文里没有词边界，且这批稿是英文，所以匹配词表取项目行里的英文/产品词。
_TERM_RX = re.compile(r"[A-Za-z][A-Za-z0-9./-]{2,}")
_STORY_ROW = re.compile(r"^\|\s*\*\*(~~)?([A-D]\d{1,2})(~~)?\*\*\s*\|\s*(.+?)\s*\|", re.M)
_TERM_STOP = {"SWE", "own", "service", "the", "and", "PM", "AI", "API", "CI", "CD",
              "JUnit", "Java", "MySQL", "MongoDB", "products", "team", "doc", "flag"}


def load_stories() -> list[tuple[str, list[str]]]:
    """从映射表 §一 读故事编号 + 匹配词 —— **映射表是故事库唯一事实源**，不在代码里抄一份。

    抄一份的后果这个产品自己就在防：口径漂移。词表随映射表变，改了映射表重跑导入器即可。
    """
    if not MAPPING.exists():
        return []
    text = MAPPING.read_text(encoding="utf-8")
    m = re.search(r"##\s*一、故事清单(.*?)(?=\n##\s)", text, re.S)
    out = []
    for _, sid, _, cell in _STORY_ROW.findall(m.group(1) if m else text):
        terms = [t for t in _TERM_RX.findall(re.sub(r"[*~`]", "", cell))
                 if t not in _TERM_STOP and len(t) >= 4]
        if terms:
            out.append((sid, terms))
    return out


def story_key(script: str, stories: list[tuple[str, list[str]]]) -> str:
    """把这条稿钉到映射表的故事编号上。命中最多匹配词的那个故事赢，至少要命中 2 个。"""
    low = (script or "").lower()
    best, best_n = "", 0
    for sid, terms in stories:
        n = sum(1 for t in terms if t.lower() in low)
        if n > best_n:
            best, best_n = sid, n
    return best if best_n >= 2 else ""


# ── 互咬检测 ───────────────────────────────────────────────────────────────
# a02/a06 是同一个故事的两个剪辑，而面试官 Joy 是在**同一轮里连着问的**（15:06 → 24:44）。
# 真实房间里读作 "one project, two angles — I don't have a second data point on this
# candidate"，比量级不足更贵（IV-P0-1 / B 批 #1）。所以不能只靠 story_id 命中映射表 ——
# 映射表覆盖不到的项目也会撞，这里用稀有词重合直接量。
_COMMON = set("""about after also because been before being both could does each even every
first from have here into just like made make many more most much must never only other over
same should some still such than that their them then there these they thing think this those
through time under very want were what when where which while will with would your""".split())


def rare_tokens(text: str) -> set[str]:
    return {w for w in recite.words(text) if len(w) >= 5 and w not in _COMMON}


def collide(a: str, b: str) -> int:
    ta, tb = rare_tokens(a), rare_tokens(b)
    if not ta or not tb:
        return 0
    return len(ta & tb)


# ---------------------------------------------------------------- 原声

_ASR_DIRTY = ("promiscuous", "data drop", "antipodes")


def copy_orig(seg_root: Path, slug: str, short_id: str, item_id: str) -> str:
    """原声段**物理拷贝**到 data/recite/orig/ —— 源在会被清理的 scratchpad。幂等。"""
    dst = recite.audio_dir("orig") / f"{item_id}.mp3"
    if dst.exists() and dst.stat().st_size > 0:
        return dst.name
    src = seg_root / slug / f"{short_id}.mp3"
    if not src.exists():
        return ""
    try:
        shutil.copyfile(src, dst)
        return dst.name
    except Exception:
        return ""


# ---------------------------------------------------------------- 写库

_FIELDS = ("item_id source company interview kind question question_zh script script_zh_gloss"
           " key_points orig_text orig_audio critique_zh target_sec variants scope quality"
           " deferred story_id link_group delivery_notes why_q why_ref gloss_precord pace"
           " debrief_line followup_q speaker_name asr_dirty extra_cues").split()


def upsert_item(conn, row: dict) -> None:
    row = {k: row.get(k) for k in _FIELDS}
    row["updated_at"] = store.la_now_iso()
    cols = list(row.keys())
    sql = (f"INSERT INTO recite_items ({', '.join(cols)}, created_at) "
           f"VALUES ({', '.join(':' + c for c in cols)}, :updated_at) "
           f"ON CONFLICT(item_id) DO UPDATE SET "
           + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "item_id"))
    conn.execute(sql, row)


def ensure_state(conn, item_id: str, due_on: str) -> None:
    """只在这条从没有过 state 时写 —— 重导内容不清她练出来的 box / streak / due。"""
    conn.execute("INSERT OR IGNORE INTO recite_state(item_id, due_on) VALUES(?,?)",
                 (item_id, due_on))


# ---------------------------------------------------------------- 通道 ① recap.json

def import_recaps(conn, root: Path, cfg: dict) -> dict:
    files = sorted(root.rglob("*.recap.json"))
    seg_root = root / "segments"
    stats = {"files": len(files), "items": 0, "imported": 0, "skipped_no_script": 0,
             "skipped_target": 0, "orig_copied": 0, "types": {"content": 0, "form": 0,
                                                              "forbid": 0, "canon": 0},
             "thin_content": 0, "precord": 0, "rough": 0, "collisions": []}
    scope_todo: list[str] = []
    stories = load_stories()
    print(f"  映射表故事词表：{len(stories)} 个故事")
    i = 0
    for f in files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  !! 读不了 {f.name}: {e}")
            continue
        company = doc.get("company") or ""
        interview = doc.get("interview") or f.stem.replace(".recap", "")
        slug = _slugify(interview)
        speaker = doc.get("interviewee_speaker") or ""
        items = doc.get("items") or []
        stats["items"] += len(items)
        here: list[tuple[str, str, str]] = []      # (item_id, kind, script)
        for it in items:
            script = (it.get("script") or "").strip()
            if not script:
                stats["skipped_no_script"] += 1
                continue
            raw_id = str(it.get("id") or "")
            short = raw_id.split("-")[-1] if raw_id else f"a{len(here):02d}"
            item_id = f"iv:{slug}:{short}"

            pts = recite.parse_key_points(
                it.get("key_points") or it.get("cue_points") or [])
            for k in pts:
                stats["types"][k["t"]] = stats["types"].get(k["t"], 0) + 1
            scored, extra = recite.scored_cues(pts)
            if len(scored) < 3:
                stats["thin_content"] += 1

            # R1：target_sec 由词数导出。pace:"deliberate" 的条目保留作者手写值 —— 那条的
            # 时长是刻意的不是估的（靠停顿制造重量的短答，导出会把它压到 dur_gate 之下）。
            pace = (it.get("pace") or "").strip()
            hand = it.get("target_sec")
            if pace == "deliberate" and hand:
                target = int(hand)
            else:
                target = recite.target_sec_from_words(len(recite.words(script)))
            if not (MIN_TARGET <= target <= MAX_TARGET):
                stats["skipped_target"] += 1
                continue

            gloss = it.get("script_zh_gloss") or ""
            probs = gloss_problems(gloss)
            if probs:
                stats["rough"] += 1
                recite._append_rough(item_id, "；".join(probs))
            pre = gloss_precord_ok(gloss)
            stats["precord"] += int(pre)

            why_q, why_ref = derive_why(pts, it)
            kind = _kind_of(it)
            variants = build_variants(it, target, kind)
            sk = story_key(script, stories)
            here.append((item_id, kind, script))

            oa = it.get("orig_audio") or {}
            name = copy_orig(seg_root, slug, short, item_id) if oa else ""
            if name:
                stats["orig_copied"] += 1
            dirty = any(w in (it.get("orig_text") or "").lower() for w in _ASR_DIRTY)

            upsert_item(conn, {
                "item_id": item_id, "source": "interview", "company": company,
                "interview": interview, "kind": kind,
                "question": it.get("question") or "", "question_zh": it.get("question_zh") or "",
                "script": script, "script_zh_gloss": gloss,
                "key_points": json.dumps(pts, ensure_ascii=False),
                "orig_text": it.get("orig_text") or "", "orig_audio": name,
                "critique_zh": it.get("critique_zh") or "", "target_sec": target,
                "variants": json.dumps(variants, ensure_ascii=False),
                "scope": "unknown", "quality": "ok", "deferred": 0,
                "story_id": sk, "link_group": None,
                "delivery_notes": json.dumps(
                    [external_focus(k["text"]) for k in pts if k["t"] == "form"],
                    ensure_ascii=False),
                "why_q": why_q, "why_ref": why_ref, "gloss_precord": int(pre),
                "pace": pace or None,
                "debrief_line": it.get("debrief_line") or doc.get("debrief_line") or "",
                "followup_q": it.get("followup_q") or "", "speaker_name": speaker,
                "asr_dirty": int(dirty),
                "extra_cues": json.dumps([k["text"] for k in extra], ensure_ascii=False),
            })
            # 打散 due（PM-问题3）：全部导入成同一天到期 = 第二天一堵 20 条的墙。
            ensure_state(conn, item_id, recite._plus(i % 5))
            i += 1
            stats["imported"] += 1
            if kind == "bq":
                scope_todo.append(f"- [ ] `{item_id}` {it.get('question_zh') or ''}")
        # 同一份复盘里两条讲故事型的条目不得共用同一个项目（R10）。撞了就落 link_group，
        # 列表页黄字提示，并进 rough_queue —— 换素材是内容侧的活（B 批 #1），产品侧只负责
        # 让它无法被忽略。
        # 只在**讲故事**型条目之间查（bq / technical-deepdive）。self-intro 和相邻的追问
        # （a09→a10）天然复用同一批词，把它们算进来只会产出噪音，而噪音会让这个提示很快
        # 被无视 —— 一个每条都亮的黄字等于没有黄字。
        tellers = [(i2, k2, s2) for i2, k2, s2 in here
                   if k2 in ("bq", "technical-deepdive")]
        for x in range(len(tellers)):
            for y in range(x + 1, len(tellers)):
                n = collide(tellers[x][2], tellers[y][2])
                if n >= 8:
                    gid = f"{slug}:g{x}"
                    for iid in (tellers[x][0], tellers[y][0]):
                        conn.execute("UPDATE recite_items SET link_group=? WHERE item_id=?",
                                     (gid, iid))
                    stats["collisions"].append(
                        (interview, f"共用 {n} 个稀有词", [tellers[x][0], tellers[y][0]]))
                    recite._append_rough(tellers[x][0],
                                         f"和 {tellers[y][0]} 像是同一个项目的两个剪辑（R10）")
    conn.commit()
    _write_scope_todo(scope_todo)
    return stats


def _kind_of(it: dict) -> str:
    """kind 加确定性行为触发词判定（PATCH 12）：没有行为触发词的落 technical-deepdive。"""
    k = (it.get("kind") or "").strip() or "other"
    if k != "bq":
        return k
    q = (it.get("question") or "") + " " + (it.get("question_zh") or "")
    if re.search(r"tell me about a time|describe a situation|give me an example|"
                 r"讲一个你|说一次你|讲一次", q, re.I):
        return "bq"
    return "technical-deepdive"


_EXTERNAL = [
    (re.compile(r"吸气|说完最后一个词|每一拍"), "让面试官听到每个句号"),
    (re.compile(r"边说边推|改口|中途重起"), "想清楚再开口，宁可空半秒"),
    (re.compile(r"it really depends|开场白|铺垫|throat"), "第一个词就是答案"),
]


def external_focus(text: str) -> str:
    """form 型要点做外部焦点改写（R12 / MEM2-P1-3）。

    内部焦点指令（「每一拍说完最后一个词再吸气」）在执行当下持有会抑制自动化、制造犹豫和
    改口 —— 正是她的病。外部焦点（「让面试官听到每个句号」）指向效果不指向动作。
    """
    for rx, repl in _EXTERNAL:
        if rx.search(text):
            return repl
    return text


def _write_scope_todo(lines: list[str]) -> None:
    """量级待核清单。**只扫今天+明天队列里的 bq 条目**（IV-v3 §三-P）：51 条手扫在凌晨
    是不会发生的，一个 4 条的日常动作会发生。这里全量落盘，页面按队列取前几条。"""
    if not lines:
        return
    p = recite.data_dir() / "scope_todo.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# 量级待核（scope）\n\n扫的时候问一句：这个决定谁拍板的？影响了几个团队？\n"
                 "thin 的直接 `UPDATE recite_items SET scope='thin' WHERE item_id=...`\n\n"
                 + "\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 通道 ② BQ playbook（rough）

# 实机结构（`company-specific/<Company>/<Company> BQ Prep.md`）：
#   ### N. 标题
#   **① 中英混杂 oral 念稿版：**   > …
#   **② Pure simple English:**     > …          ← 这一段才是念稿
#   **Cue card / 要点卡:**         - …          ← 要点卡
#   **可能的追问 / Follow-ups…**   1. **"…"** →  ← 真实追问，天然是好的 paraphrase 来源
_Q_HDR = re.compile(r"^###\s+(\d+)\.\s+(.+?)\s*$", re.M)
_ENG = re.compile(r"\*\*②[^*]*\*\*\s*\n((?:>.*\n|\s*\n)+)")
_CUE = re.compile(r"\*\*Cue card[^*]*\*\*\s*\n((?:\s*[-*].*\n)+)")
_FUP = re.compile(r"\*\*可能的追问[^*]*\*\*\s*\n\s*1\.\s*\*\*[\"“]([^\"”]+)")
_MD = re.compile(r"\*\*|\*|`")


def _quote_block(raw: str) -> str:
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith(">"):
            out.append(_MD.sub("", line[1:].strip()))
    return " ".join(x for x in out if x).strip()


def import_playbook(conn, cfg: dict, limit_companies: list[str] | None = None) -> dict:
    """BQ Prep md 的粗导入通道。**标 rough、deferred=1，不进每日队列**，只在「⚠ 稿要改」
    分区手动可练。

    今晚做到「能跑就行」：md 结构在 28 家之间并不完全统一，一个真正稳的解析器不是今晚的活。
    无 Cue card 的公司**直接跳过并报出来**，不静默产出 rough（PM-P0-1.1）—— 由 script 首句
    拼出来的假要点卡，形式上正是要禁的「可以原样念出口的英文短语」。
    """
    root = Path("/path/to/interview/workspace/behavior question/company-specific")
    stats = {"companies": 0, "imported": 0, "no_cue": [], "no_script": 0}
    if not root.exists():
        print("  (playbook 根目录不在，跳过)")
        return stats
    for md in sorted(root.glob("*/*BQ Prep*.md")):
        if any(x in str(md) for x in ("_archive", "_retired", "backup", "_format")):
            continue
        company = md.parent.name
        if limit_companies and company not in limit_companies:
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        stats["companies"] += 1
        hdrs = list(_Q_HDR.finditer(text))
        if not hdrs:
            stats["no_cue"].append(company)
            continue
        n_here = 0
        for i2, h in enumerate(hdrs):
            block = text[h.end(): hdrs[i2 + 1].start() if i2 + 1 < len(hdrs) else len(text)]
            cue_m, eng_m = _CUE.search(block), _ENG.search(block)
            if not (cue_m and eng_m):
                stats["no_script"] += 1
                continue
            script = _quote_block(eng_m.group(1))
            cues = [_MD.sub("", x).strip() for x in
                    re.findall(r"^\s*[-*]\s+(.+)$", cue_m.group(1), re.M)]
            if len(script.split()) < 40 or not cues:
                stats["no_script"] += 1
                continue
            q = _MD.sub("", h.group(2)).strip()
            item_id = f"bq:{_slugify(company)}:{int(h.group(1)):02d}"
            pts = recite.parse_key_points(cues)
            target = recite.target_sec_from_words(len(recite.words(script)))
            if not (MIN_TARGET <= target <= MAX_TARGET):
                stats["no_script"] += 1
                continue
            why_q, why_ref = derive_why(pts, {"id": item_id, "question": q})
            fup = _FUP.search(block)
            item = {"question": q, "question_zh": q}
            variants = build_variants(item, target, "bq")
            if fup:
                # 真实追问天然实质不同，是比 question_zh 反译好得多的 paraphrase 来源
                # （FEY-v3 §5① 点名的那条）。
                variants.insert(0, {"type": "paraphrase", "sec": target,
                                    "q": f"追问：{fup.group(1)}", "hint": ""})
            upsert_item(conn, {
                "item_id": item_id, "source": "playbook", "company": company,
                "interview": f"{company} BQ Prep", "kind": "bq", "question": q,
                "question_zh": q, "script": script, "script_zh_gloss": "",
                "key_points": json.dumps(pts, ensure_ascii=False), "orig_text": "",
                "orig_audio": "", "critique_zh": "", "target_sec": target,
                "variants": json.dumps(variants, ensure_ascii=False),
                "scope": "unknown", "quality": "rough", "deferred": 1,
                "why_q": why_q, "why_ref": why_ref, "gloss_precord": 0,
                "followup_q": fup.group(1) if fup else "",
                "delivery_notes": json.dumps(
                    [external_focus(k["text"]) for k in pts if k["t"] == "form"],
                    ensure_ascii=False),
                "extra_cues": json.dumps(
                    [k["text"] for k in recite.scored_cues(pts)[1]], ensure_ascii=False),
            })
            ensure_state(conn, item_id, recite._plus(0))
            n_here += 1
            stats["imported"] += 1
        if not n_here:
            stats["no_cue"].append(company)
    conn.commit()
    return stats


# ---------------------------------------------------------------- 通道 ③ 开口微稿

def import_openers(conn, cfg: dict) -> dict:
    """T6。任何情况下不许被挤掉 —— 它是 "not communicative" 这条挂点在 MVP 里的唯一覆盖，
    且工程量为零（就是 6 条 items）。"""
    p = cfgmod.resolve_path("data/seeds/openers.json")
    if not p.exists():
        print("  (data/seeds/openers.json 不在，跳过)")
        return {"imported": 0}
    seeds = json.loads(p.read_text(encoding="utf-8"))
    n = 0
    for i, s in enumerate(seeds):
        item_id = f"op:{i + 1:02d}"
        pts = recite.parse_key_points(s.get("key_points") or [])
        target = int(s.get("target_sec") or
                     recite.target_sec_from_words(len(recite.words(s.get("script") or ""))))
        why_q, why_ref = derive_why(pts, {"id": item_id, "question": s.get("question") or ""})
        upsert_item(conn, {
            "item_id": item_id, "source": "opener", "company": "", "interview": "开口微稿",
            "kind": "opener", "question": s.get("question") or "",
            "question_zh": s.get("question_zh") or "", "script": s.get("script") or "",
            "script_zh_gloss": s.get("script_zh_gloss") or "",
            "key_points": json.dumps(pts, ensure_ascii=False), "orig_text": "", "orig_audio": "",
            "critique_zh": "", "target_sec": target,
            "variants": json.dumps(build_variants(s, target, "think-aloud"),
                                   ensure_ascii=False),
            "scope": "ok", "quality": "ok", "deferred": 0,
            "why_q": s.get("why_q") or why_q, "why_ref": s.get("why_ref") or why_ref,
            "gloss_precord": int(gloss_precord_ok(s.get("script_zh_gloss") or "")),
            "extra_cues": "[]",
        })
        ensure_state(conn, item_id, recite._plus(i % 3))
        n += 1
    conn.commit()
    return {"imported": n}


# ---------------------------------------------------------------- 通道 ④ 参考件（他人实录）

def import_reference(conn, recap_path: Path, speaker: str) -> dict:
    """他人实录整场导入（她的裁决 8/31：所有面试都要在库里，包括阿雅的 Apple screen1）。

    和主通道的差别：**不受 `script != null` 准入约束**（他人实录本来就不产她的念稿），
    source='reference' + deferred=1 —— 库里可见、可听、可看对话，但不进练习队列、不建
    recite_state。target_sec 用原声段时长兜底。
    """
    doc = json.loads(recap_path.read_text(encoding="utf-8"))
    interview = doc.get("interview") or recap_path.stem.replace(".recap", "")
    slug = _slugify(interview)
    seg_root = recap_path.parent.parent / "segments"
    if not seg_root.exists():
        seg_root = recap_path.parent / "segments"
    n = copied = 0
    for it in doc.get("items") or []:
        raw_id = str(it.get("id") or "")
        short = raw_id.split("-")[-1] if raw_id else f"a{n:02d}"
        item_id = f"iv:{slug}:{short}"
        script = (it.get("script") or "").strip()
        oa = it.get("orig_audio") or {}
        target = int(hand) if (hand := it.get("target_sec")) else 0
        if not target:
            if script:
                target = recite.target_sec_from_words(len(recite.words(script)))
            else:
                target = max(MIN_TARGET, min(MAX_TARGET,
                                             int(oa.get("end", 0)) - int(oa.get("start", 0))))
        pts = recite.parse_key_points(it.get("key_points") or [])
        name = copy_orig(seg_root, slug, short, item_id) if oa else ""
        copied += bool(name)
        upsert_item(conn, {
            "item_id": item_id, "source": "reference", "company": doc.get("company") or "",
            "interview": interview, "kind": _kind_of(it),
            "question": it.get("question") or "", "question_zh": it.get("question_zh") or "",
            "script": script, "script_zh_gloss": it.get("script_zh_gloss") or "",
            "key_points": json.dumps(pts, ensure_ascii=False),
            "orig_text": it.get("orig_text") or "", "orig_audio": name,
            "critique_zh": it.get("critique_zh") or "", "target_sec": target,
            "variants": "[]", "scope": "unknown", "quality": "ok", "deferred": 1,
            "story_id": "", "link_group": None, "delivery_notes": "[]",
            "why_q": it.get("why_q") or "", "why_ref": it.get("why_ref") or "",
            "gloss_precord": 0, "pace": None,
            "debrief_line": it.get("debrief_line") or "",
            "followup_q": it.get("followup_q") or "", "speaker_name": speaker,
            "asr_dirty": 0, "extra_cues": "[]",
        })
        n += 1
    conn.commit()
    return {"imported": n, "orig_copied": copied, "interview": interview}


# ---------------------------------------------------------------- 报告

def report(stats: dict) -> int:
    """红灯判据。返回 exit code。"""
    print(f"\n  文件 {stats['files']} · 条目 {stats['items']} · "
          f"导入 {stats['imported']} · 无 script 跳过 {stats['skipped_no_script']} · "
          f"target 出界跳过 {stats['skipped_target']}")
    print(f"  原声拷贝 {stats['orig_copied']} · gloss 录前可展开 {stats['precord']} · "
          f"gloss 进 rough_queue {stats['rough']}")
    t = stats["types"]
    tot = sum(t.values()) or 1
    print(f"  key_points 分型：content {t.get('content', 0)} · form {t.get('form', 0)} · "
          f"forbid {t.get('forbid', 0)} · canon {t.get('canon', 0)}  "
          f"(content 占 {100 * t.get('content', 0) // tot}%)")
    for iv, k, ids in stats["collisions"]:
        print(f"  🟡 撞车：{iv} 里 {len(ids)} 条共用故事「{k}」→ {', '.join(ids)}")
    bad = 0
    if stats["imported"] < 6:
        print(f"  🔴 红灯：只导入了 {stats['imported']} 条，队列上限 6，第一屏不满")
        bad = 1
    thin_pct = 100 * stats["thin_content"] / max(1, stats["imported"])
    if thin_pct > 15:
        print(f"  🔴 红灯：{stats['thin_content']}/{stats['imported']} ({thin_pct:.0f}%) 的条目"
              f" content 型要点 < 3 条 —— 分型器可能跑偏了，左栏和唯一的硬门都靠它，人看一眼")
        bad = 1
    else:
        print(f"  ✅ 分型器：content < 3 条的条目占 {thin_pct:.0f}%（红线 15%）")
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.recite_import")
    ap.add_argument("dir", nargs="?", help="recap.json 所在目录（含 segments/）")
    ap.add_argument("--from-recaps", dest="recaps")
    ap.add_argument("--from-playbook", action="store_true")
    ap.add_argument("--from-openers", action="store_true")
    ap.add_argument("--company", action="append", default=[])
    ap.add_argument("--all", dest="all_dir")
    ap.add_argument("--reference", help="单场他人实录的 recap.json 路径")
    ap.add_argument("--speaker", default="", help="他人实录里的受访者叫什么（如 阿雅）")
    a = ap.parse_args(argv)

    cfg = cfgmod.load_config()
    conn = recite.connect(cfg)
    src = a.recaps or a.all_dir or a.dir
    rc = 0
    if src:
        print(f"→ recap 通道：{src}")
        rc |= report(import_recaps(conn, Path(src), cfg))
    if a.from_playbook or a.all_dir:
        print("→ playbook 通道（rough，不进队列）")
        s = import_playbook(conn, cfg, a.company or None)
        print(f"  公司 {s['companies']} · 导入 {s['imported']}")
        if s["no_cue"]:
            print(f"  ⚠️ 无 Cue card，拒绝导入：{', '.join(s['no_cue'])}")
    if a.reference:
        print(f"→ 参考件通道（他人实录）：{a.reference}")
        s = import_reference(conn, Path(a.reference), a.speaker)
        print(f"  「{s['interview']}」导入 {s['imported']} 条 · 原声拷贝 {s['orig_copied']}")
    if a.from_openers or a.all_dir:
        print("→ 开口微稿")
        print(f"  导入 {import_openers(conn, cfg)['imported']}")
    n = conn.execute("SELECT COUNT(*) n FROM recite_items").fetchone()["n"]
    print(f"\n  recite_items 共 {n} 条。范读：python -m tools.recite_tts --only-missing")
    conn.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
