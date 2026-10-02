const params = new URLSearchParams(location.search);
const inReview = (params.get("status") || "new") === "new";

async function api(path, method = "POST", body = null) {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : null,
  });
  if (!r.ok) {
    alert(`${r.status}: ${await r.text()}`);
    throw new Error(`api ${path} -> ${r.status}`);
  }
  return r.json();
}

const rows = () => [...document.querySelectorAll("tr[data-job-id]")];
const rowOf = (el) => el.closest("tr[data-job-id]");

/* ---------- status actions ---------- */
async function setStatus(tr, status, scrollAfter = true) {
  if (!tr) return;
  const variant = status === "applied" ? (tr.querySelector(".variant")?.value || "base") : null;
  await api(`/api/roles/${encodeURIComponent(tr.dataset.jobId)}/status`, "POST",
    { status, resume_variant: variant });
  const chip = tr.querySelector(".status-chip");
  if (chip) { chip.textContent = status; chip.dataset.status = status; }
  if (inReview && status !== "new") {
    const jd = tr.nextElementSibling;
    tr.classList.add("gone");
    setTimeout(() => {
      if (jd && jd.classList.contains("jd-row")) jd.remove();
      tr.remove();
      // Re-anchor the keyboard cursor onto whatever row slid up into this slot — but ONLY
      // if a cursor exists. focusStep() treats focusIdx < 0 as "start at row 0", so calling
      // it unconditionally made every MOUSE click scroll the page back to the top: a mouse
      // user never presses j/k, so focusIdx is always -1.
      if (focusIdx >= 0) focusStep(0, scrollAfter);
    }, 250);
  }
}

/* ---------- queue for tailoring ---------- */
async function queueTailoring(tr) {
  if (!tr) return;
  const res = await api(`/api/roles/${encodeURIComponent(tr.dataset.jobId)}/queue-tailoring`,
    "POST", { angle: null });
  const chip = tr.querySelector(".chip.tailor");
  if (!chip) {
    const span = document.createElement("span");
    span.className = "chip tailor";
    span.textContent = "queued";
    tr.querySelector(".title .apply").before(span, document.createTextNode(" "));
  }
  // Owner 2026-09-23: queueing starts the generation right away (background claude workers),
  // so the message says what is happening instead of asking for a manual run.
  const chipEl = tr.querySelector(".chip.tailor");
  if (chipEl) chipEl.textContent = res.started ? "generating…" : "queued";
  alert(res.started
    ? `已排队并开始生成（${res.queued} 个岗位在这一批，约 2-3 分钟一份，完成后自动入库，刷新可见 📄）。`
    : res.waiting
      ? `已排队。正在跑的批次结束后会自动开始（队列里 ${res.queued} 个）。`
      : `Prompt written:\n${res.prompt_path}`);
}

/* ---------- JD expand ---------- */
async function toggleJD(tr) {
  const next = tr.nextElementSibling;
  if (next && next.classList.contains("jd-row")) { next.remove(); return; }
  const role = await api(`/api/roles/${encodeURIComponent(tr.dataset.jobId)}`, "GET");
  const d = document.createElement("tr");
  d.className = "jd-row";
  const td = document.createElement("td");
  td.colSpan = tr.children.length;
  const meta = document.createElement("div");
  meta.className = "jd-meta";
  meta.textContent = `${role.job_id} · ${role.department || "no dept"} · why: ${role.why || "-"} · caution: ${role.caution || "-"}`;
  const pre = document.createElement("pre");
  pre.textContent = role.jd_text || "(no JD text)";
  td.append(meta, pre);
  d.append(td);
  tr.after(d);
}

/* ---------- notes editing ---------- */
function editNotes(cell) {
  if (cell.querySelector("textarea")) return;
  const tr = rowOf(cell);
  const old = cell.textContent.trim() === "+" ? "" : cell.textContent.trim();
  cell.textContent = "";
  const ta = document.createElement("textarea");
  ta.value = old;
  cell.append(ta);
  ta.focus();
  let done = false;
  const finish = (text) => {
    if (done) return;
    done = true;
    cell.textContent = text || "+";
  };
  const save = async () => {
    const v = ta.value;
    await api(`/api/roles/${encodeURIComponent(tr.dataset.jobId)}/notes`, "POST", { notes: v });
    finish(v);
  };
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save(); }
    if (e.key === "Escape") finish(old);
  });
  ta.addEventListener("blur", () => { if (!done) save(); });
}

/* ---------- keyboard ---------- */
let focusIdx = -1;
function focusStep(delta, scroll = true) {
  const rs = rows();
  if (!rs.length) { focusIdx = -1; return; }
  focusIdx = focusIdx < 0 ? 0 : Math.max(0, Math.min(rs.length - 1, focusIdx + delta));
  rs.forEach((r) => r.classList.remove("focused"));
  rs[focusIdx].classList.add("focused");
  // Rows run 250-360px tall, so scrolling the replacement row into view after a MOUSE click
  // yanks the page a few hundred px out from under the pointer. Keyboard nav still scrolls —
  // there the cursor is the only thing telling you where you are.
  if (scroll) rs[focusIdx].scrollIntoView({ block: "nearest" });
}

document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea, select")) return;
  const tr = rows()[focusIdx];
  const acts = { a: "applied", s: "skip", x: "rejected", i: "interviewing", n: "new" };
  if (e.key === "j") { e.preventDefault(); focusStep(focusIdx < 0 ? 0 : 1); }
  else if (e.key === "k") { e.preventDefault(); focusStep(-1); }
  else if (e.key === "Enter" && tr) { e.preventDefault(); toggleJD(tr); }
  else if (e.key === "t" && tr) { e.preventDefault(); queueTailoring(tr); }
  else if (acts[e.key] && tr) { e.preventDefault(); setStatus(tr, acts[e.key]); }
});

/* ---------- delegated clicks / changes ---------- */
document.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");
  if (btn) {
    // Mouse and keyboard share ONE cursor: clicking a row's button moves the cursor there,
    // so the re-anchor after the row disappears lands on its replacement — already under the
    // pointer, so scrollIntoView({block:"nearest"}) has nothing to scroll. Without this the
    // cursor would still be wherever j/k last left it and the page would jump back to it.
    if (btn.dataset.act === "queue") queueTailoring(rowOf(btn));
    else {
      focusIdx = rows().indexOf(rowOf(btn));
      setStatus(rowOf(btn), btn.dataset.act, false);   // false → don't scroll; the pointer is here
    }
    return;
  }
  if (e.target.closest("#ingest-btn")) {
    const b = e.target.closest("#ingest-btn");
    b.disabled = true; b.textContent = "ingesting…";
    api("/api/ingest", "POST").then((r) => {
      b.disabled = false; b.textContent = "ingest résumés";
      alert(r.ingested ? `Rendered ${r.ingested} résumé PDF(s). Reloading.` : "Nothing new in output/resumes/.");
      if (r.ingested) location.reload();
    }).catch(() => { b.disabled = false; b.textContent = "ingest résumés"; });
    return;
  }
  // ↗ opens the application page. Record WHICH role first: for LinkedIn-sourced roles the
  // apply_url is the LinkedIn page, and clicking Apply there hands you off to the employer's
  // own ATS whose URL carries no id we store — so the extension's URL lookup can never join
  // back to li:<id>. The pin is that join, and it expires (store.PIN_TTL_MIN) so it can't
  // attach a stale résumé later. Fire-and-forget: the link must open even if JobScan is down.
  const apply = e.target.closest("a.apply");
  if (apply) {
    const tr = rowOf(apply);
    // raw fetch, not api(): api() alerts on failure, and a pin that didn't stick must never
    // interrupt the click — the worst case is the extension falls back to what it does today.
    if (tr) {
      fetch("/api/pin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: tr.dataset.jobId }),
        keepalive: true,          // the tab may navigate away before this settles
      }).catch(() => {});
    }
    return;                       // let the browser follow the href normally
  }
  if (e.target.closest(".title-link")) { toggleJD(rowOf(e.target)); return; }
  const notes = e.target.closest(".notes-cell");
  if (notes) { editNotes(notes); return; }
  const rm = e.target.closest(".co-remove");
  if (rm) {
    api(`/api/applied-companies/${encodeURIComponent(rm.dataset.name)}`, "DELETE")
      .then(() => location.reload());
  }
});

document.addEventListener("change", (e) => {
  if (e.target.matches("select.more")) {
    const v = e.target.value;
    e.target.value = "";
    if (v) {                                   // also a pointer path — same no-scroll rule
      focusIdx = rows().indexOf(rowOf(e.target));
      setStatus(rowOf(e.target), v, false);
    }
  }
  // 2026-09-28：筛选下拉不再改一项就自动提交——侧栏里一次调好几项再点 Apply（帮手工作台改版）。
});

/* ---------- 筛选侧栏收起 / 展开（owner 2026-09-28）：状态存 localStorage，list.html 顶部的内联脚本负责无闪恢复 ---------- */
(() => {
  const layout = document.querySelector(".layout"), btn = document.querySelector("#pane-toggle");
  if (!layout || !btn) return;
  const paint = () => { btn.textContent = layout.classList.contains("collapsed") ? "▶" : "◀"; };
  paint();
  btn.addEventListener("click", () => {
    const collapsed = layout.classList.toggle("collapsed");
    try { localStorage.setItem("jobscan.filterpane", collapsed ? "1" : "0"); } catch (_) {}
    paint();
  });
})();

document.querySelector("#add-co")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const inp = e.target.querySelector("input");
  const name = inp.value.trim();
  if (name) { await api("/api/applied-companies", "POST", { name }); location.reload(); }
});

/* ---------- 投递身份（apply profile / name card）切换 ---------- */
// 独立监听器：.idcard 没有 data-act，不会撞上面的状态按钮处理。
document.addEventListener("click", async (e) => {
  const b = e.target.closest("button.idcard");
  if (!b) return;
  const ap = window.APPLY_PROFILES || {};
  const keys = Object.keys(ap.profiles || {});
  if (keys.length < 2) return;
  const next = keys[(keys.indexOf(b.dataset.profile) + 1) % keys.length];
  const card = ap.profiles[next] || {};
  if (!confirm(`把这个岗位的投递身份切成「${card.display_name || next}」？\n简历抬头（名字/邮箱/电话）立刻重打，自动填表也跟着换。`)) return;
  b.disabled = true;
  try {
    const r = await api(
      `/api/roles/${encodeURIComponent(rowOf(b).dataset.jobId)}/apply-profile`,
      "POST", { profile: next });
    b.dataset.profile = next;
    b.textContent = card.label || next[0].toUpperCase();
    b.title = `投递身份：${card.display_name || next}`;
    b.classList.toggle("alt", next !== (ap.default || "casey"));
    if (r.note) alert(`身份已记录，但简历没能当场重打：${r.note}\n(每日 autotailor 重出这份简历时会用新身份)`);
  } finally { b.disabled = false; }
});

/* ---------- 「这家不让投了」→ cooldown（owner 2026-09-22） ---------- */
// Same effect as a config.yaml `cooldowns:` entry (the scanner skips the company, review hides
// its roles) without editing YAML, and it expires by itself. Independent listener: .cdbtn has
// no data-act, so it never collides with the status buttons above.
document.addEventListener("click", (e) => {
  const b = e.target.closest("button.cdbtn");
  if (!b) return;
  const tr = rowOf(b);
  const company = tr && tr.dataset.company;
  if (!company || (b.nextElementSibling && b.nextElementSibling.classList.contains("cd-pop"))) return;
  const pop = document.createElement("span");
  pop.className = "cd-pop";
  pop.innerHTML = `冷却 <select><option value="30">30 天</option><option value="60">60 天</option>` +
    `<option value="90">90 天</option><option value="180">180 天</option></select> ` +
    `<input placeholder="原因（可选）" size="18"> <button class="ok">确定</button><button class="no">取消</button>`;
  b.after(pop);
  pop.querySelector("input").focus();
  pop.querySelector(".no").onclick = () => pop.remove();
  pop.querySelector(".ok").onclick = async () => {
    const days = +pop.querySelector("select").value;
    const reason = pop.querySelector("input").value.trim();
    const r = await api("/api/companies/cooldown", "POST", { company, days, reason });
    pop.remove();
    if (inReview) {
      rows().filter((x) => x.dataset.company === company).forEach((x) => {
        const jd = x.nextElementSibling;
        x.classList.add("gone");
        setTimeout(() => { if (jd && jd.classList.contains("jd-row")) jd.remove(); x.remove(); }, 250);
      });
    }
    alert(`${company} 已加入 cooldown，到 ${r.until}。\n扫描从下一次起跳过这家；review 里它的 ${r.hidden_roles} 个岗位已隐藏。\n到期自动恢复；要提前解除去 /companies 页。`);
  };
});

/* ---------- 全局投递身份开关（顶栏，像登录邮箱） ---------- */
document.querySelectorAll(".id-switch a").forEach((a) =>
  a.addEventListener("click", async (e) => {
    e.preventDefault();
    await api("/api/identity", "POST", { identity: a.dataset.id });
    const u = new URL(location);
    u.searchParams.delete("identity");   // URL 参数会盖过 cookie，切换后清掉
    location.href = u;
  }));

/* ---------- one-click batch tailoring (owner 2026-09-23: 「不需要一个一个地去点」) ---------- */
// Sends the page's own query string — the same filter params index() read — so the server
// rebuilds exactly this list and queues every row on it that has no résumé yet. The run itself
// is the nightly engine (tools/auto_tailor.sh: 4 headless claude workers → one ingest), so this
// page only polls /api/tailor-batch/status; nothing here does any tailoring.
const BATCH_FILTER_KEYS = ["status", "q", "company", "min_score", "verdict", "seen", "remote",
  "seniority", "tag", "identity", "tailored", "ease", "sort", "rot", "showall"];
let batchPollTimer = null;

function batchBody(dryRun) {
  const body = { dry_run: dryRun };
  for (const k of BATCH_FILTER_KEYS) {
    const v = params.get(k);
    if (v !== null) body[k] = v;        // absent identity → server falls back to the cookie
  }
  return body;
}

function postBatch(dryRun) {
  return fetch("/api/tailor-batch", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(batchBody(dryRun)),
  });
}

const batchProgress = (s) => `已写 ${s.written}/${s.total} · 已入库 ${s.ingested}`;

async function pollBatch() {
  const btn = document.querySelector("#tailor-batch-btn");
  const st = document.querySelector("#tailor-batch-status");
  if (!btn || !st) return;
  let s;
  try { s = await (await fetch("/api/tailor-batch/status")).json(); }
  catch { batchPollTimer = setTimeout(pollBatch, 15000); return; }   // server hiccup: keep polling
  if (s.running) {
    btn.disabled = true;
    st.className = "dim busy";
    st.textContent = `进行中 · ${batchProgress(s)}`;
    st.title = (s.log_tail || []).join("\n");
    batchPollTimer = setTimeout(pollBatch, 15000);
    return;
  }
  batchPollTimer = null;
  btn.disabled = false;
  st.className = "dim done";
  st.textContent = `完成 · ${batchProgress(s)} `;
  const a = document.createElement("a");
  a.href = "#"; a.textContent = "刷新看结果";
  a.addEventListener("click", (e) => { e.preventDefault(); location.reload(); });
  st.appendChild(a);
}

document.querySelector("#tailor-batch-btn")?.addEventListener("click", async () => {
  const btn = document.querySelector("#tailor-batch-btn");
  const st = document.querySelector("#tailor-batch-status");
  btn.disabled = true;
  st.className = "dim"; st.textContent = "统计中…";
  try {
    const r = await postBatch(true);
    if (!r.ok) { alert(`${r.status}: ${await r.text()}`); st.textContent = ""; return; }
    const dry = await r.json();
    if (!dry.queued) { st.textContent = "当前列表里的岗位都已有简历，没什么可做的"; return; }
    // 4 workers in parallel, 2–3 min per résumé → wall clock ≈ ceil(N/4) × 2–3 min
    const rounds = Math.ceil(dry.queued / 4);
    const capNote = dry.capped ? `\n（超出单批上限，排名最低的 ${dry.capped} 个这次不做）` : "";
    if (!confirm(`给当前列表里还没有简历的 ${dry.queued} 个岗位一次性做简历？大约 ${rounds * 2}–${rounds * 3} 分钟（4 路并行，每份 2-3 分钟）。${capNote}`)) {
      st.textContent = ""; return;
    }
    st.textContent = "写 prompt、启动 4 路 worker…";
    const r2 = await postBatch(false);
    if (r2.status === 409) { alert((await r2.json()).detail); st.textContent = ""; return; }
    if (!r2.ok) { alert(`${r2.status}: ${await r2.text()}`); st.textContent = ""; return; }
    const res = await r2.json();
    if (!res.started) {
      st.textContent = `没有启动：${res.queued} 个可做，${(res.skipped || []).length} 个写 prompt 失败`;
      return;
    }
    st.className = "dim busy";
    st.textContent = `已启动 ${res.queued} 份 · 等第一份写出来…`;
    if (batchPollTimer) clearTimeout(batchPollTimer);
    batchPollTimer = setTimeout(pollBatch, 15000);   // button stays disabled until running=false
  } finally {
    if (!batchPollTimer) btn.disabled = false;
  }
});

// A reload mid-batch must not lose the progress line: if the lock is held, resume polling.
if (document.querySelector("#tailor-batch-btn")) {
  fetch("/api/tailor-batch/status").then((r) => r.json()).then((s) => {
    if (s.running && s.total) pollBatch();
  }).catch(() => {});
}
