/* Dedicated extension page: owns one leased application, never an employer credential. */
(() => {
  "use strict";
  const BASE = "http://127.0.0.1:8787";
  const ROOT = "/api/agent-lab/runner/";
  const BOARD_START_WINDOW = 180;
  const PENDING_KEY = "jobscanRunnerPending";
  const WORKER_KEY = "jobscanRunnerWorker";
  const HOSTS = new Set(["jobs.ashbyhq.com", "job-boards.greenhouse.io", "boards.greenhouse.io",
    "job-boards.eu.greenhouse.io", "boards.eu.greenhouse.io", "jobs.lever.co", "jobs.eu.lever.co"]);
  const OUTCOMES = new Set(["submitted", "expired", "needs_user", "blocked", "error", "unknown"]);
  const ROLES = {filler: "Filler", reviewer: "Reviewer", system: "系统"};
  const PHASES = {idle: "等待简历就绪", paused: "后续领取已暂停", pausing: "当前申请完成后暂停", running: "正在执行",
    claimed: "已领取申请", completed: "本轮已完成", submitted: "雇主已确认提交", expired: "已下架",
    needs_user: "等待处理", blocked: "本轮已停下", error: "运行出错", unknown: "提交结果待核实"};
  const $ = id => document.getElementById(id);
  let workerId = sessionStorage.getItem(WORKER_KEY);
  if (!workerId) { workerId = crypto.randomUUID(); sessionStorage.setItem(WORKER_KEY, workerId); }
  let armed = false, busy = false, timer = null, token = "", connected = false;
  let active = null, pendingFinish = null, events = [], runEvents = [], draining = null, state = null;
  let notice = "", pollNotice = "", identityConflict = "", lastJob = null, requestBusy = false;
  let recoveryBlocked = false, seenBatch = null, selectedIdentity = "";
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  // By code point: half an emoji is a lone surrogate, which the local service rejects outright.
  const text = (value, max = 1600) => [...String(value == null ? "" : value)].slice(0, max).join("");

  function allowedURL(raw) {
    const url = new URL(raw);
    if (url.protocol !== "https:" || !HOSTS.has(url.hostname) || url.username || url.password ||
        (url.port && url.port !== "443")) throw new Error("岗位链接不在本轮支持的官方招聘域名内。");
    return url.href;
  }
  function sameJobURL(observed, expected) {
    if (typeof observed !== "string" || !observed.trim()) return false;
    try {
      const actual = new URL(allowedURL(observed)), claimed = new URL(allowedURL(expected));
      const rolePath = url => url.pathname.replace(/\/+$/, "").replace(/\/(?:application|apply)$/, "").replace(/\/+$/, "");
      const tracking = new Set(["source", "src", "ref", "referrer", "referral", "gh_src", "lever-source", "lever-origin"]);
      const identityQuery = url => [...url.searchParams].filter(([key]) =>
        !key.toLowerCase().startsWith("utm_") && !tracking.has(key.toLowerCase()))
        .map(pair => JSON.stringify(pair)).sort().join("\n");
      const path = rolePath(claimed);
      return !!path && actual.origin === claimed.origin && rolePath(actual) === path && identityQuery(actual) === identityQuery(claimed);
    } catch (_) { return false; }
  }
  function withTimeout(promise, ms, message) {
    let timeout;
    return Promise.race([promise, new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error(message)), ms);
    })]).finally(() => clearTimeout(timeout));
  }
  async function settings({requireCasey = false} = {}) {
    const saved = await chrome.storage.local.get({jobscanPort: 0, jobscanIdentity: "", jobscanReviewTokens: {}});
    if (Number(saved.jobscanPort) !== 8787) throw new Error("请先在扩展弹窗选择 Casey · 8787 工作台。");
    const identity = selectedIdentity = String(saved.jobscanIdentity || "").trim();
    identityConflict = identity && identity !== "casey"
      ? "扩展当前选择了其他名片。请先在扩展弹窗切回默认 Casey · 8787 工作台；此页不会修改你的全局选择。" : "";
    if (identityConflict) armed = false;
    if (requireCasey && identityConflict) throw new Error(identityConflict);
    token = String((saved.jobscanReviewTokens || {})["8787"] || "");
    if (!token) throw new Error("尚未连接：请使用填表实验室的一次性连接码。");
  }
  async function api(path, body, unauthenticated = false) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(new URL(path, BASE), {
        method: body === undefined ? "GET" : "POST", redirect: "error", cache: "no-store",
        headers: {"Content-Type": "application/json", ...(unauthenticated ? {} : {"X-JobScan-Review": token,
          "X-JobScan-Extension-Version": chrome.runtime.getManifest?.().version || "",
          "X-JobScan-Extension-Identity": selectedIdentity})},
        // After stringify a lone surrogate is an escape nothing could repair, and one rejected
        // event would hold back /finish forever. So each string is mended on its way in.
        ...(body === undefined ? {} : {body: JSON.stringify(body, (_, v) => typeof v === "string" ? v.toWellFormed() : v)}),
        signal: controller.signal,
      });
      const result = await response.json();
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) { armed = false; connected = false; }
        const error = new Error(typeof result.detail === "string" ? result.detail : `JobScan 返回 ${response.status}`);
        error.status = response.status; throw error;
      }
      return result;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("本机 JobScan 请求超时；保留当前申请，不重复提交。");
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function leaseBody() {
    if (!active) throw new Error("没有当前申请的执行凭据。");
    return {worker_id: workerId, lease_id: active.lease_id, token: active.token};
  }
  function persist() {
    if (active) sessionStorage.setItem(PENDING_KEY, JSON.stringify({active, events, runEvents, pendingFinish}));
    else sessionStorage.removeItem(PENDING_KEY);
  }
  function cleanMessage(value) {
    let message = text(value);
    for (const secret of [token, active?.token]) if (secret) message = message.split(secret).join("[已隐藏]");
    return message;
  }
  function enqueue(role, message, to) {
    if (!active || !ROLES[role] || typeof message !== "string" || !message.trim()) return;
    // All entries reflect actual execution. Cap memory if the local service goes offline.
    if (events.length >= 200) return;
    const event = {role, text: cleanMessage(message), at: Date.now() / 1000, localId: crypto.randomUUID()};
    if (ROLES[to]) event.to = to;
    events.push(event); runEvents.push(event); persist(); render();
    void flushEvents().catch(error => { notice = cleanMessage(error.message); render(); });
  }
  async function flushEvents() {
    if (draining) return draining;
    draining = (async () => {
      while (active && events.length) {
        const event = events[0];
        await api(ROOT + "event", {...leaseBody(), role: event.role, text: event.text,
          ...(event.to ? {to: event.to} : {})});
        events.shift(); persist();
      }
    })();
    try { await draining; } finally { draining = null; }
  }
  function formatTime(value) {
    const d = typeof value === "number" ? new Date(value > 1e12 ? value : value * 1000) : new Date(value || Date.now());
    return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", second: "2-digit"});
  }
  function render() {
    $("connection").textContent = connected ? "已连接 · 本机 8787" : "尚未连接";
    $("start").disabled = !connected || busy || requestBusy || !!active || !!state?.active || armed || recoveryBlocked || !!identityConflict;
    $("pause").disabled = !connected || requestBusy || (!armed && !state?.enabled);
    $("pair").disabled = requestBusy || busy || !!active;
    $("refresh").disabled = requestBusy;
    const runningJob = active?.job || state?.active?.job || state?.active || lastJob;
    const phaseKey = state?.pause_reason === "expired" ? "expired" : state?.phase;
    let phase = "尚未开始", detail = "打开此页不会开始投递；点击开始后，本轮最多处理 1 份。";
    if (active) {
      phase = pendingFinish ? "正在保存本轮结果" : "正在执行申请";
      detail = armed ? "申请页需保持前台；Filler 与 Reviewer 的实际消息会出现在下面。" : "已暂停后续领取；当前申请仍会完成检查并记录结果。";
    } else if (armed) {
      phase = PHASES[phaseKey] || "已启动，等待领取";
      detail = "每 3 秒检查一次队列；没有符合条件的就绪简历时不会打开申请。";
    } else if (phaseKey && phaseKey !== "idle") {
      phase = PHASES[phaseKey] || text(phaseKey, 80);
      detail = state.active ? "已有申请由执行页持有；本页只显示状态，不会重复领取。"
        : state.enabled ? "此执行页尚未启动领取。" : "本页会保留本轮状态与申请标签。";
    }
    $("phase").textContent = phase; $("detail").textContent = detail;
    $("notice").textContent = [...new Set([notice, identityConflict].filter(Boolean))].join(" ");
    $("job-company").textContent = text(runningJob?.company) || "—";
    $("job-title").textContent = text(runningJob?.title) || "—";
    $("job-state").textContent = active ? (pendingFinish ? PHASES[pendingFinish.outcome] : "执行中") : PHASES[phaseKey] || "未领取";
    const link = $("application-link"); link.hidden = true;
    try { if (runningJob?.url) { link.href = allowedURL(runningJob.url); link.hidden = false; } } catch (_) { /* no unsafe links */ }
    const remoteEvents = Array.isArray(state?.events) ? state.events : [];
    const feed = $("events"); feed.replaceChildren();
    const rows = (active ? runEvents.map(event => ({event, pending: events.some(e => e.localId === event.localId)}))
      : remoteEvents.map(event => ({event, pending: false}))).slice(-100);
    if (!rows.length) { const item = document.createElement("li"); item.className = "empty"; item.textContent = "尚无本轮记录。"; feed.append(item); }
    for (const {event, pending} of rows) {
      const item = document.createElement("li"); if (pending) item.className = "pending";
      const meta = document.createElement("div"); meta.className = "event-meta";
      const who = document.createElement("span"); who.className = "event-role";
      who.textContent = (ROLES[event.role] || "系统") + (ROLES[event.to] ? ` → ${ROLES[event.to]}` : "");
      const when = document.createElement("span"); when.textContent = formatTime(event.at || event.timestamp) + (pending ? " · 待保存" : "");
      meta.append(who, when); const body = document.createElement("div"); body.className = "event-text";
      body.textContent = cleanMessage(event.text || event.message || ""); item.append(meta, body); feed.append(item);
    }
    const jobs = $("jobs"); jobs.replaceChildren();
    for (const record of (Array.isArray(state?.jobs) ? state.jobs : []).slice(-5)) {
      const job = record.job || record;
      const row = document.createElement("li"), title = document.createElement("div"), outcome = document.createElement("div");
      title.className = "job-title"; title.textContent = [job.company, job.title].filter(Boolean).map(v => text(v, 200)).join(" · ");
      outcome.className = "job-outcome"; outcome.textContent = PHASES[record.outcome || record.status] || text(record.outcome || record.status || "等待处理", 100);
      row.append(title, outcome); jobs.append(row);
    }
  }
  // A batch started moments ago on the 8787 board arms this open page once. Opening the
  // page later never starts anything, even if an old batch is still marked enabled.
  function armFromBoard() {
    const batch = state?.batch_id;
    if (!batch || batch === seenBatch) return;
    seenBatch = batch;
    if (state.enabled === true && Date.now() / 1000 - (state.started_at || 0) < BOARD_START_WINDOW &&
        !active && !pendingFinish && !recoveryBlocked && !identityConflict) armed = true;
  }
  async function refreshStatus() {
    await settings(); state = await api(ROOT + "status"); connected = true;
    if (state.enabled === false) armed = false;
    armFromBoard();
    render();
    await updateIfStale();
  }
  // The board knows which version is on disk. When this page sits idle on an older one, load the
  // new one instead of asking for a click; background.js opens this page again afterwards.
  async function updateIfStale() {
    const want = state?.worker?.expected_version, have = chrome.runtime.getManifest?.().version;
    if (!want || !have || want === have || active || pendingFinish || armed || state.active || state.enabled) return;
    const saved = await chrome.storage.local.get({jobscanReloadedFor: ""});
    if (saved.jobscanReloadedFor === want) return;   // Reloading did not reach it; never loop.
    await chrome.storage.local.set({jobscanReloadedFor: want, jobscanReopenRunner: true});
    chrome.runtime.reload();
  }
  async function ready(tabId) {
    const deadline = Date.now() + 20000;
    // This page is hidden while Filler works, and a hidden page's timers can slow to one tick a
    // minute by the later jobs of a batch. So it always gets several tries, however long they take.
    for (let attempt = 0; attempt < 5 || Date.now() < deadline; attempt++) {
      try {
        const result = await withTimeout(chrome.tabs.sendMessage(tabId, {type: "jobscanRunnerReady"}, {frameId: 0}),
          2000, "填表组件尚未响应。");
        if (result?.ready === false && result.gate) {
          const {kind, evidence} = result.gate;
          if (["expired", "login", "captcha", "verification"].includes(kind) &&
              !sameJobURL(result.gate.url, active.job.url))
            return {gate: {outcome: "blocked", evidence: "页面地址无法与领取的岗位对应，保留页面核对，未标记已下架，也未启动填写。"}};
          if (kind === "expired" && typeof evidence === "string" && evidence.trim())
            return {gate: {outcome: "expired", evidence: cleanMessage(evidence)}};
          const gateOutcomes = {login: "blocked", captcha: "needs_user", verification: "needs_user"};
          if (gateOutcomes[kind]) return {gate: {outcome: gateOutcomes[kind],
            evidence: typeof evidence === "string" && evidence.trim() ? cleanMessage(evidence)
              : {login: "申请需要登录，已停止本轮无账号投递。", captcha: "申请出现 CAPTCHA，需要用户处理。",
                verification: "申请需要验证码，已保留页面等待处理。"}[kind]}};
          if (kind === "expired") return {gate: {outcome: "blocked", evidence: "页面检查未提供明确的下架证据，保留申请核对，未标记已下架。"}};
        }
        if (result?.ready === true || result?.status === "ready") {
          if (result.job_id && result.job_id !== active.job.job_id) throw new Error("申请页岗位与领取的岗位不一致。");
          return;
        }
      } catch (error) {
        if (error.message.includes("岗位不一致")) throw error;
      }
      await delay(650);
    }
    throw new Error("20 秒内未找到可执行的填表组件；申请页已保留，请检查页面是否跳转或需要登录。");
  }
  async function finish() {
    if (!active || !pendingFinish) return;
    let recorded = true;
    try {
      await flushEvents();
      await api(ROOT + "finish", {...leaseBody(), ...pendingFinish});
    } catch (error) {
      if (error.status === 404) {
        // This page no longer holds the lease: it was paired again, or another page took the job over. No retry can succeed.
        recorded = false;
        notice = "这个执行凭据已经不归这一页了（可能重新连接过，或者别的执行页接手了）。结果没有记上，请核对保留的申请页；本页不会重新执行。";
      } else {
        if (error.status !== 409) throw error;
        // An expired lease is terminal. Never overwrite it with a late client claim
        // of success, nor keep retrying events against a closed lease forever.
        const snapshot = await api(ROOT + "status");
        const record = (snapshot.jobs || []).find(row => row.lease_id === active.lease_id);
        if (!record || record.status === "claimed") throw error;
        recorded = false;
        notice = `本轮执行凭据已结束，JobScan 记录为${PHASES[record.status] || record.status}。请核对保留的申请页；本页不会重新执行。`;
      }
    }
    lastJob = active.job;
    // The employer's answer is recorded, so nothing is left to look at on that page.
    // If the service kept another result, the notice above tells her to keep the page, so it stays.
    const definite = recorded && (pendingFinish.outcome === "submitted" || pendingFinish.outcome === "expired" || /^雇主拒收/.test(pendingFinish.evidence || ""));
    const finishedTab = active.tabId;
    // Stay armed: the batch ends when the service turns it off, which refreshStatus reads.
    active = null; pendingFinish = null; events = []; runEvents = []; persist();
    // Not awaited: a page asking "Leave or Stay?" would hold this promise, and with it the whole batch, until someone answers.
    if (definite && Number.isInteger(finishedTab)) { await delay(800); chrome.tabs.remove?.(finishedTab)?.catch?.(() => {}); }
    await refreshStatus();
  }
  // Greenhouse answers Submit by loading a confirmation page, and that ends the message channel the click was waiting on
  // (Mercury, 2026-11-08). The page opens by itself with no application behind it too, so it counts only as the same tab,
  // after the execute message, landing on this posting's confirmation and saying the application was submitted.
  async function greenhouseReceipt(tabId, jobUrl) {
    let why = "";
    for (let tries = 0; tries < 12; tries++) {
      try {
        const tab = await chrome.tabs.get(tabId), now = new URL(tab.url || tab.pendingUrl || "about:blank"), job = new URL(jobUrl);
        if (!/(^|\.)greenhouse\.io$/.test(job.hostname)) return {words: ""};
        const same = now.hostname === job.hostname && (job.pathname.startsWith("/embed/job_app")
          ? now.pathname === "/embed/job_app/confirmation" && ["for", "token"].every(key => now.searchParams.get(key) === job.searchParams.get(key))
          : now.pathname.replace(/\/confirmation\/?$/, "") === job.pathname.replace(/\/+$/, "") && /\/confirmation\/?$/.test(now.pathname));
        why = same ? "回执页还没读到那句话" : `标签页在 ${text(now.origin + now.pathname, 120)}`;
        if (same) {
          // A board can word its own thanks (Scale AI, 2026-11-08), so a received or thanks counts as well as submitted.
          const [read] = await chrome.scripting.executeScript({target: {tabId, frameIds: [0]},
            func: () => (document.querySelector(".confirmation") || document.body)?.innerText || ""});
          const words = String(read?.result || "").replace(/\s+/g, " ").trim();
          why = `回执页上的字是：${text(words, 160) || "（空）"}`;
          if (/\bapplication (?:has been |was )?(?:successfully )?(?:submitted|received)\b|\bapplication submitted\b|\bthanks? (?:you )?for (?:applying|your application)\b/i.test(words) &&
              !/\bnot\b|n['’]t\b|\bfail|\berror\b/i.test(words.slice(0, 300))) return {words: text(words, 600)};
        }
      } catch (error) { why = cleanMessage(error.message); }   // The tab can be mid-load; a closed one never becomes a receipt.
      await delay(1500);
    }
    return {words: "", why};
  }
  async function execute(claim) {
    active = {lease_id: claim.lease_id, token: claim.token, job: claim.job, expires_at: claim.expires_at, tabId: null, executeSent: false};
    events = []; runEvents = []; pendingFinish = null; persist(); render();
    try {
      if (!active.lease_id || !active.token || !active.job?.job_id) throw new Error("领取响应缺少完整执行凭据。");
      if (active.job.apply_profile !== "casey" || active.job.resume_ready !== true || active.job.status !== "new")
        throw new Error("岗位身份、简历就绪状态或未投递状态不符合本轮条件。");
      if (!Number.isFinite(active.expires_at) || active.expires_at * 1000 - Date.now() < 60000)
        throw new Error("本次执行凭据已过期或即将过期，未打开申请。");
      await settings({requireCasey: true});
      const url = allowedURL(active.job.url);
      const tab = await chrome.tabs.create({url, active: true});
      if (!Number.isInteger(tab.id)) throw new Error("无法创建申请标签。");
      active.tabId = tab.id; persist();
      // Filling pauses while the tab is hidden; a window behind another app counts as hidden.
      if (Number.isInteger(tab.windowId)) await chrome.windows.update(tab.windowId, {focused: true}).catch(() => {});
      enqueue("system", `已打开 ${text(active.job.company, 120)} · ${text(active.job.title, 200)}，等待填表组件。`);
      const readiness = await ready(tab.id);
      if (readiness?.gate) {
        pendingFinish = readiness.gate;
        notice = readiness.gate.evidence;
        enqueue("system", `Gatekeeper：${PHASES[pendingFinish.outcome]}。${pendingFinish.evidence} 未启动 Filler 或 Reviewer。`);
      } else {
        await settings({requireCasey: true});
        const deadline = Math.min(Date.now() + 12 * 60 * 1000, active.expires_at * 1000 - 30000);
        if (deadline <= Date.now()) throw new Error("本次执行凭据已接近过期，未启动填写。");
        active.executeSent = true; persist();
        // This message is deliberately sent ONCE. A lost response is not permission to submit again.
        const result = await withTimeout(chrome.tabs.sendMessage(tab.id, {type: "jobscanRunnerExecute",
          job_id: active.job.job_id, apply_profile: "casey", lease: leaseBody(), deadline}, {frameId: 0}),
          Math.max(1, deadline - Date.now()),
        "执行回复超时，提交结果尚不明确。申请页已保留，禁止自动重试。");
        const outcome = OUTCOMES.has(result?.status) ? result.status : "unknown";
        const evidence = cleanMessage(result?.evidence || result?.reason || (outcome === "unknown" ? "执行组件未返回明确结果。" : "执行组件已返回结果。"));
        pendingFinish = {outcome, evidence};
        enqueue("system", `执行返回：${PHASES[outcome] || outcome}。${evidence}`);
      }
    } catch (error) {
      const found = active.executeSent && Number.isInteger(active.tabId) ? await greenhouseReceipt(active.tabId, active.job.url) : {words: ""};
      const receipt = found.words;
      if (found.why) enqueue("system", `找 Greenhouse 回执页没找到：${found.why}`);
      if (receipt) {
        // The page that would have recorded it is gone, so this page does: JobScan must not offer the role again.
        const marked = await chrome.runtime.sendMessage({type: "markApplied", jobId: active.job.job_id, variant: "tailored"}).catch(() => null);
        pendingFinish = {outcome: "submitted", evidence: cleanMessage(`Greenhouse 回执页：${receipt}` + (marked?.ok ? "" : "（JobScan 记账失败，不要重投）"))};
        enqueue("system", `点提交后页面跳到了 Greenhouse 的回执页：${receipt}`);
      } else {
        const outcome = active.executeSent ? "unknown" : "blocked";
        const evidence = cleanMessage(error.message);
        pendingFinish = {outcome, evidence}; notice = evidence;
        enqueue("system", `${active.executeSent ? "执行后失联，不重新执行" : "执行前停下"}：${evidence}`);
      }
    }
    persist(); render();
    await finish();
  }
  async function tick() {
    if (busy || requestBusy) return;
    busy = true;
    try {
      await refreshStatus();
      // A poll that worked again retires the error the failed one left on the page.
      if (pollNotice && notice === pollNotice) notice = "";
      pollNotice = "";
      if (pendingFinish) await finish();
      else if (armed && state.enabled === true && !active) {
        const claim = await api(ROOT + "claim", {worker_id: workerId});
        if (claim.status === "claimed") await execute(claim);
        else if (claim.status === "paused") armed = false;
      }
    } catch (error) { notice = pollNotice = cleanMessage(error.message); connected = false; }
    finally { busy = false; render(); }
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(async () => { await tick(); schedule(); }, 3000);
  }
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type !== "jobscanRunnerEvent" || !active || sender.tab?.id !== active.tabId ||
        sender.id !== chrome.runtime.id) return;
    const event = message.event || {};
    enqueue(event.role, event.text, event.to); sendResponse({ok: true});
  });
  $("start").addEventListener("click", async () => {
    if (busy || requestBusy || active || recoveryBlocked) return;
    requestBusy = true; notice = ""; render();
    try {
      await settings({requireCasey: true}); await api(ROOT + "start", {apply_profile: "casey", max_jobs: 1});
      armed = true; await refreshStatus();
    } catch (error) { armed = false; notice = cleanMessage(error.message); }
    finally { requestBusy = false; render(); }
    await tick(); schedule();
  });
  $("pause").addEventListener("click", async () => {
    armed = false; requestBusy = true; render();
    try {
      await settings(); await api(ROOT + "pause", {});
      notice = active ? "已暂停后续领取。当前申请继续检查并记录结果；请保留申请页。" : "已暂停，后续不会领取岗位。";
      await refreshStatus();
    } catch (error) { notice = cleanMessage(error.message); }
    finally { requestBusy = false; render(); }
  });
  $("refresh").addEventListener("click", async () => {
    if (requestBusy) return;
    requestBusy = true; render();
    try { await refreshStatus(); } catch (error) { connected = false; notice = cleanMessage(error.message); }
    finally { requestBusy = false; render(); }
  });
  $("pair").addEventListener("click", async () => {
    if (busy || active || requestBusy) return;
    const code = $("pair-code").value.trim(); if (!code) { $("pair-state").textContent = "请粘贴连接码。"; return; }
    requestBusy = true; render();
    try {
      const port = await chrome.storage.local.get({jobscanPort: 0});
      if (Number(port.jobscanPort) !== 8787) throw new Error("请先在扩展弹窗选择 8787 工作台。");
      const result = await api("/api/agent-lab/pair", {code}, true);
      if (typeof result.token !== "string" || !result.token) throw new Error("连接响应缺少令牌。");
      const saved = await chrome.storage.local.get({jobscanReviewTokens: {}});
      await chrome.storage.local.set({jobscanReviewTokens: {...saved.jobscanReviewTokens, "8787": result.token}});
      $("pair-code").value = ""; $("pair-state").textContent = "已连接。此操作不会启动投递。";
      notice = ""; await refreshStatus();
    } catch (error) { $("pair-state").textContent = cleanMessage(error.message); }
    finally { requestBusy = false; render(); }
  });
  window.addEventListener("beforeunload", event => {
    if (!active) return;
    event.preventDefault(); event.returnValue = "";
  });
  async function init() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(PENDING_KEY) || "null");
      if (saved?.active) {
        active = saved.active; events = Array.isArray(saved.events) ? saved.events : [];
        runEvents = Array.isArray(saved.runEvents) ? saved.runEvents : events.slice();
        pendingFinish = saved.pendingFinish || {outcome: "unknown", evidence: "执行页曾关闭或刷新，无法确认上一轮结果；保留申请页，未重新执行。"};
        notice = "检测到中断的申请；只恢复结果记录，不重新填表或提交。"; persist();
      }
    } catch (_) { recoveryBlocked = true; notice = "上次执行状态无法恢复；已停止本页自动启动，请先核对保留的申请与 JobScan 记录。"; }
    await tick();
    if (!connected) $("pair-box").open = true;
    schedule();
  }
  void init();
})();
