async function getPort() {
  try { return Number((await chrome.storage.local.get({ jobscanPort: 0 })).jobscanPort) || 0; } catch (_) { return 0; }
}

document.getElementById('review-pair').onclick = async () => {
  const status = document.getElementById('review-pair-status');
  try {
    const base = await check();
    if (!base) throw new Error('请先连接 JobScan 工作台。');
    const r = await fetch(base + '/api/agent-lab/pair', {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code:document.getElementById('review-code').value})});
    const result = await r.json();
    if (!r.ok) throw new Error(result.detail || '连接失败');
    const saved = await chrome.storage.local.get({jobscanReviewTokens:{}});
    saved.jobscanReviewTokens[String(await getPort())] = result.token;
    await chrome.storage.local.set(saved);
    document.getElementById('review-code').value='';
    status.textContent='已连接；刷新申请页后使用 Fill + Review。';
  } catch(e) {status.textContent=e.message;}
};
// 身份（apply profile）：同一个工作台可以用不同名片投（她的小号 alex = 法名 + candidate）。
// 存在这个 Chrome profile 里，background 把它当 identity= 参数带给服务器。
async function getIdentity() {
  try { return String((await chrome.storage.local.get({ jobscanIdentity: "" })).jobscanIdentity || ""); } catch (_) { return ""; }
}
async function bases() { const p = await getPort(); return p ? [`http://127.0.0.1:${p}`, `http://localhost:${p}`] : []; }

async function check() {
  for (const base of await bases()) {
    try {
      const r = await fetch(base + "/health");
      if (r.ok) return base;
    } catch (_) { /* try next */ }
  }
  return null;
}

// 万能兜底（owner 2026-09-10，Waymo 马甲域名引出）：manifest 名单外的招聘页，点一下
// 把 content.js 现场注入当前标签页。activeTab + scripting 权限组合不需要任何新域名授权；
// content.js 自带 __jobscanLoaded 防重复注入护栏，在已注入页面上点了也无害。
document.getElementById('independent-runner').onclick = () => {chrome.tabs.create({url:chrome.runtime.getURL('runner.html')});window.close();};
document.getElementById("inject").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https?:/.test(tab.url || "")) return;
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["page_gate.js", "form_agent.js", "agent_workflow.js", "content.js"] });
    window.close();
  } catch (e) {
    document.getElementById("hint").textContent = "注入失败：" + e.message;
  }
});

// 工作台切换（owner 2026-09-23）：每个候选人一个 JobScan 实例，端口不同；这个下拉记在当前 Chrome
// profile 里，所以「他的 Chrome profile 选 8788」设一次就永远是他的。
const portSel = document.getElementById("port");
Promise.all([getPort(), getIdentity()]).then(([p, id]) => {
  const v = p ? `${p}|${id}` : "";
  portSel.value = v;
  if (v && portSel.value !== v) { const o = document.createElement("option"); o.value = v; o.textContent = `其他 (${p}${id ? " · " + id : ""})`; portSel.appendChild(o); portSel.value = v; }
});
portSel.addEventListener("change", async () => {
  const [port, id] = (portSel.value || "|").split("|");
  const v = Number(port) || 0;
  if (v) await chrome.storage.local.set({ jobscanPort: v, jobscanIdentity: id || "" });
  else await chrome.storage.local.remove(["jobscanPort", "jobscanIdentity"]);
  location.reload();
});

check().then(async (base) => {
  const s = document.getElementById("status");
  const hint = document.getElementById("hint");
  if (!(await getPort())) {
    s.innerHTML = '<span class="dot bad"></span>还没选工作台 — 扩展不会填任何表单';
    hint.textContent = "先在上面选这个 Chrome profile 是谁的资料，再刷新招聘页面。";
    return;
  }
  if (base) {
    let who = "";
    const id = await getIdentity();
    try { const r = await fetch(base + "/api/profile" + (id ? `?identity=${encodeURIComponent(id)}` : "")); const d = await r.json(); who = ((d.profile || {}).first_name || "") + " " + ((d.profile || {}).last_name || ""); } catch (_) { /* fine */ }
    s.innerHTML = '<span class="dot ok"></span>connected to local JobScan' + (who.trim() ? ` · 填表用 <b>${who.trim()}</b>` : "") + (id ? ` <span style="color:#6b7280">(身份 ${id})</span>` : "");
    hint.textContent = base;
  } else {
    s.innerHTML = '<span class="dot bad"></span>server not running';
    hint.innerHTML = "start it: <code>python -m app.web</code>";
  }
});
