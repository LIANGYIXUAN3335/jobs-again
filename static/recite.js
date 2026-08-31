/* 念稿练习 · 练习页（v2 库优先返工）。
 *
 * v1 是一台状态机：曝光轮弹窗 → 录音 → why 门 → 才解锁音频和稿子。实机试用后全部拆掉。
 * v2 只有一条线：**页面一屏全开** → 想听就听（面试原声随时可播）→ 录 → 立刻回听 →
 * 原声 vs 我的来回 A/B → 想要评分再点 [让 AI 评一下]。AI 范读（TTS）已从 UI 全部拿掉，
 * 盘上的 data/recite/ref/ 和 tools/recite_tts.py 留着但界面不展示。
 * 录音一停就上传落盘（score 不带 = 只存不评），
 * 所以「不点评分」永远不等于「录音丢了」。
 *
 * 录音走浏览器 MediaRecorder。127.0.0.1 是 secure context，getUserMedia 可用，不需要 https。
 * **mime 三选一**：Safari 只给 audio/mp4，Chrome 给 audio/webm，写死任何一个就等于在另一个
 * 浏览器上一按录音就抛。服务端按 content-type 决定扩展名，ffmpeg 两种都吃。
 */
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("recite-data").textContent);
  var $ = function (id) { return document.getElementById(id); };

  var MIMES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
  var rec = null, chunks = [], t0 = 0, timer = null, blob = null;
  var takeId = null, mineUrl = "";

  function pickMime() {
    if (!window.MediaRecorder) return "";
    for (var i = 0; i < MIMES.length; i++) {
      if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(MIMES[i])) return MIMES[i];
    }
    return "";                       // 让浏览器自己挑
  }

  function fmt(s) {
    s = Math.floor(s);
    return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2);
  }

  function tick() {
    var s = (Date.now() - t0) / 1000;
    $("clock").textContent = fmt(s);
    var pct = Math.min(100, 100 * s / (D.variant.sec || D.target_sec || 60));
    $("progress").style.width = pct + "%";
  }

  // ── 盖稿开关：默认关（稿子一直在）。开了这一 take 才算闭卷、才计过关/箱位。 ────
  var mask = $("mask"), eng = $("engtext");
  function curMode() { return mask && mask.checked ? "recall" : "read"; }
  if (mask && eng) {
    mask.addEventListener("change", function () {
      eng.classList.toggle("masked", mask.checked);
    });
  }

  // ── 录音 ────────────────────────────────────────────────────────────────
  async function start() {
    var stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      $("recnote").textContent = "拿不到麦克风：" + e.name +
        " —— 在地址栏左边的图标里允许麦克风，然后刷新。";
      return;
    }
    var m = pickMime();
    try {
      rec = m ? new MediaRecorder(stream, { mimeType: m }) : new MediaRecorder(stream);
    } catch (e) {
      rec = new MediaRecorder(stream);
    }
    chunks = [];
    rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
    rec.onstop = function () {
      blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
      stream.getTracks().forEach(function (t) { t.stop(); });
      clearInterval(timer);
      mineUrl = URL.createObjectURL(blob);
      $("a_mine").src = mineUrl;
      $("mine-track").classList.remove("hide");
      // 对比区只在有原声时才有意义 —— 范读已经从 UI 拿掉，没原声就只剩「我的 vs 我的」。
      if (D.has_orig) $("cmp").classList.remove("hide");
      $("cmp-mine").dataset.src = mineUrl;
      $("scorebox").classList.remove("hide");
      $("recbtn").disabled = false;
      $("recbtn").textContent = "● 再 录 一 遍";
      $("recbtn").classList.remove("on");
      $("stopbtn").disabled = true;
      $("recnote").textContent = D.has_orig ? "录完了 —— 先听自己一遍，再和面试原声来回比。"
                                            : "录完了 —— 听一遍自己。";
      $("a_mine").play().catch(function () {});
      upload();
    };
    rec.start();
    t0 = Date.now();
    timer = setInterval(tick, 200);
    $("recbtn").textContent = "● 录 音 中";
    $("recbtn").classList.add("on");
    $("recbtn").disabled = true;
    $("stopbtn").disabled = false;
    $("recnote").textContent = "录着呢。说完按停止。";
  }

  $("recbtn").addEventListener("click", start);
  $("stopbtn").addEventListener("click", function () {
    if (rec && rec.state === "recording") rec.stop();
  });

  // 录音一停就落盘（不带 score → 后端只存不评）。评分是之后一个可选按钮的事。
  async function upload() {
    takeId = null;
    $("scorenote").textContent = "录音上传中…";
    var fd = new FormData();
    fd.append("audio", blob, "take" + (blob.type.indexOf("mp4") >= 0 ? ".mp4" : ".webm"));
    fd.append("mode", curMode());
    fd.append("variant_type", D.variant.type || "original");
    fd.append("variant_sec", D.variant.sec || D.target_sec || 60);
    fd.append("variant_q", D.variant.q || "");
    fd.append("variant_hint", D.variant.hint || "");
    fd.append("first_exposure", D.first_exposure ? "1" : "0");
    try {
      var r = await (await fetch("/api/recite/" + encodeURIComponent(D.item_id) + "/take",
                                 { method: "POST", body: fd })).json();
      takeId = r.take_id || null;
      $("scorenote").textContent = takeId ? "录音已存盘。要不要评分随你。" : "上传没成功。";
      if (takeId) addTakeRow(r.audio_url);
    } catch (e) {
      $("scorenote").textContent = "上传失败（" + e + "）—— 录音还在这个页面上，别刷新。";
    }
  }

  function addTakeRow(url) {
    var list = $("takelist");
    if (!list || !url) return;
    var d = document.createElement("div");
    d.className = "t";
    d.innerHTML = '<span class="ts">刚刚</span><audio controls preload="none" src="' + url +
                  '"></audio><span class="ts">未评</span>';
    list.insertBefore(d, list.firstChild);
  }

  // ── A/B 对比：一个播放器，点谁播谁 ───────────────────────────────────────
  document.querySelectorAll("#cmp .ab button").forEach(function (b) {
    b.addEventListener("click", function () {
      if (!b.dataset.src) return;
      document.querySelectorAll("#cmp .ab button").forEach(function (x) { x.classList.remove("on"); });
      b.classList.add("on");
      var a = $("a_cmp");
      a.src = b.dataset.src;
      a.play().catch(function () {});
    });
  });

  // ── AI 评分：可选。点了才跑 ASR + 判官。 ─────────────────────────────────
  function dots(n) {
    n = Math.max(0, Math.min(5, Math.round(n || 0)));
    return "●".repeat(n) + "○".repeat(5 - n);
  }

  $("scorebtn").addEventListener("click", async function () {
    if (!takeId) { $("scorenote").textContent = "还没有已存盘的录音。"; return; }
    var btn = this;
    btn.disabled = true;
    $("scorenote").textContent = "转写 + 评分中…（Parakeet 冷启动可能要一分钟）";
    var r;
    try {
      r = await (await fetch("/api/recite/take/" + takeId + "/rescore",
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ why_line: $("whyline").value.trim(), mode: curMode() }) })).json();
    } catch (e) {
      $("scorenote").textContent = "评分请求挂了，录音已在盘上 —— 可以再点一次。";
      btn.disabled = false;
      return;
    }
    btn.disabled = false;
    btn.textContent = "再评一次";
    $("scorenote").textContent = "";
    render(r);
  });

  function render(r) {
    $("card").classList.remove("hide");

    $("transcript").textContent = r.transcript || "（转写没跑出来）";
    if (r.metrics && r.metrics.asr_error) {
      $("asrnote").textContent = "⚠️ 转写失败：" + r.metrics.asr_error + " —— 录音已存，可以再评一次。";
    } else if (r.metrics) {
      $("asrnote").textContent = "引擎 " + (r.metrics.asr_engine || "?") +
        " · " + (r.metrics.duration || 0).toFixed(0) + "s · " + (r.metrics.wpm || 0) + " wpm · " +
        ">2s 停顿 " + (r.metrics.pauses_over_2s || 0) + " 次 · 填充词（参考值，不进判定）" +
        ((r.metrics.hard_filler || 0) + (r.metrics.soft_filler || 0));
    }

    $("verdict").textContent = r.verdict || "—";
    $("total").textContent = r.total != null ? ("← " + r.total + " / 5") : "";
    $("calibnote").textContent = r.calibrating ? "过关线校准中 —— 这一次不改箱位。" : "";
    $("gatenote").textContent = r.gate_note || "";

    var s = r.scores || {}, rows = [["覆盖（代码算）", r.coverage],
      ["结构", s.structure], ["句子", s.concision], ["口条", s.fluency], ["为什么", s.why_depth]];
    $("dims").innerHTML = rows.filter(function (x) { return x[1] != null; }).map(function (x) {
      return '<div class="dim-row"><span>' + x[0] + '</span><span class="dots">' +
             dots(x[1]) + " " + x[1] + "</span></div>";
    }).join("");

    var f = (r.red_flags || []).map(function (x) { return '<div class="flag">🚩 ' + x + "</div>"; });
    if (r.judge_error) f.push('<div class="flag">评分没跑出来（' + r.judge_error + "），录音已存。</div>");
    $("flags").innerHTML = f.join("");
    $("facts").innerHTML = (r.fact_check || []).map(function (c) {
      return '<div class="fact">📋 「' + c.said + "」 " + c.note +
             (c.provenance ? "<br><span class='small'>" + c.provenance + "</span>" : "") +
             " <span class='small'>（事实核对不进分数）</span></div>";
    }).join("");
    $("missed").innerHTML = (r.missed_cues || []).map(function (m) {
      return "❌ 没说到：「" + m + "」";
    }).join("<br>");

    // r 高在原版档**不是指控**：多数稿只有 ~110 词，练熟后逐字重合必然偏高。
    var rl = "📎 逐字重合 " + (r.verbatim_ratio != null ? r.verbatim_ratio : "—");
    if (r.r_prev != null) rl += " · 和你上一次的重合 " + r.r_prev;
    rl += r.r_is_flag ? "（这一档会看它）" : "（这一档仅记录，不是指控）";
    $("rline").textContent = rl;

    var fx = $("fixnext");
    fx.textContent = r.fix_next ? "▸ 下一遍只改这一件事：" + r.fix_next : "";
    fx.classList.toggle("hide", !r.fix_next);
    $("oneline").textContent = r.one_line || "";
    window.__take = r;
  }

  $("rewrite").addEventListener("click", function () { mark("needs_rewrite"); });
  $("thin").addEventListener("click", function () { mark("scope_thin"); });
  function mark(action) {
    fetch("/api/recite/" + encodeURIComponent(D.item_id) + "/mark",
          { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: action }) })
      .then(function () { location.href = "/recite"; });
  }
})();
