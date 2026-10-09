// Service worker: the ONLY part that talks to the local JobScan API.
// Content scripts can't fetch http://localhost from an https page (mixed content),
// but the service worker has its own extension origin + host_permissions, so it can.

// Which JobScan instance this Chrome profile talks to (owner 2026-09-23): each candidate's
// workspace runs its own server (Casey 8787, Taylor 8788, …). The choice lives in
// chrome.storage.local, so a separate Chrome profile per candidate keeps them apart for good.
// Owner 2026-09-23: there is NO default. Until a workspace is picked in the popup, every API
// call fails with NO_WORKSPACE and the content script fills nothing — the one way to make sure
// a form is never filled with the wrong person's data.
async function selectedPort() {
  try { return Number((await chrome.storage.local.get({ jobscanPort: 0 })).jobscanPort) || 0; } catch (_) { return 0; }
}
async function serverBases() {
  const port = await selectedPort();
  return port ? [`http://127.0.0.1:${port}`, `http://localhost:${port}`] : [];
}

// The selected identity (apply profile) rides along on the lookups that decide which name /
// email / résumé header a form gets. The server applies it to the role the way the review page's
// name-card badge does; other endpoints ignore it.
async function selectedIdentity() {
  try { return String((await chrome.storage.local.get({ jobscanIdentity: "" })).jobscanIdentity || ""); } catch (_) { return ""; }
}
async function withIdentity(path) {
  const id = await selectedIdentity();
  if (!id || !/^\/api\/(lookup|top-role|profile)(\?|$)/.test(path)) return path;
  return path + (path.includes("?") ? "&" : "?") + "identity=" + encodeURIComponent(id);
}

async function apiFetch(path, init) {
  const bases = await serverBases();
  if (!bases.length) throw new Error("NO_WORKSPACE");
  path = await withIdentity(path);
  let lastErr;
  for (const base of bases) {
    try {
      const r = await fetch(base + path, init);
      return r;
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr || new Error("JobScan server unreachable");
}

async function lookup(url) {
  const r = await apiFetch(`/api/lookup?url=${encodeURIComponent(url)}`);   // throws NO_WORKSPACE when unset
  if (r.status === 404) return { found: false };
  if (!r.ok) throw new Error(`lookup ${r.status}`);
  return { found: true, role: await r.json() };
}

async function resumeBase64(jobId) {
  const r = await apiFetch(`/api/roles/${encodeURIComponent(jobId)}/resume.pdf`);
  if (!r.ok) throw new Error(`no résumé pdf (${r.status})`);
  const buf = await r.arrayBuffer();
  let binary = "";
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return { b64: btoa(binary), name: r.headers.get("x-filename") || "resume.pdf" };
}

async function answer(jobId, questions) {
  const r = await apiFetch(`/api/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: jobId, questions }),
  });
  if (!r.ok) {
    let detail = `answer ${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (e) { /* keep default */ }
    throw new Error(detail);
  }
  return r.json(); // { answers: {question: text} }
}

async function emailCode() {
  const r = await apiFetch(`/api/email-code`);
  if (!r.ok) {
    let detail = `email-code ${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (e) { /* keep default */ }
    throw new Error(detail);
  }
  return r.json(); // { code, subject, from, date }
}

async function salary(jobId) {
  const r = await apiFetch(`/api/salary?job_id=${encodeURIComponent(jobId)}`);
  if (!r.ok) throw new Error(`salary ${r.status}`);
  return r.json();   // { amount, source }
}

async function coverLetter(jobId, words) {
  const r = await apiFetch(`/api/cover-letter?job_id=${encodeURIComponent(jobId)}&words=${words || 180}`);
  if (!r.ok) {
    let detail = `cover letter ${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (e) { /* keep default */ }
    throw new Error(detail);
  }
  return r.json();   // { text }
}

async function pickOption(jobId, question, options) {
  const r = await apiFetch(`/api/pick-option`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: jobId, question, options }),
  });
  if (!r.ok) return { index: -1 };
  return r.json();   // { index }
}

async function topRole(company) {
  const r = await apiFetch(`/api/top-role?company=${encodeURIComponent(company)}`);
  if (r.status === 404) return { found: false };
  if (!r.ok) throw new Error(`top-role ${r.status}`);
  return { found: true, role: await r.json() };
}

async function pinned() {
  // The role the owner clicked ↗ on in JobScan moments ago. Only consulted after the URL
  // lookup fails: LinkedIn-sourced roles hand off to an employer ATS whose URL shares no id
  // with our li:<id>, so intent is the only join left. The server expires it (45 min).
  const r = await apiFetch(`/api/pinned`);
  if (r.status === 404) return { found: false };
  if (!r.ok) throw new Error(`pinned ${r.status}`);
  return { found: true, role: await r.json() };
}

async function pins() {
  // ALL live pins. The caller matches them against the page it is on — the server can't, it
  // has never seen the page. Falls back to the single-pin endpoint so a new extension still
  // works against a JobScan server that predates /api/pins.
  const r = await apiFetch(`/api/pins`);
  if (r.ok) return { found: true, roles: (await r.json()).pins || [] };
  if (r.status !== 404) throw new Error(`pins ${r.status}`);
  const one = await pinned();
  return { found: true, roles: one.found ? [one.role] : [] };
}

async function unpin(jobId) {
  // Called by the frame that actually rendered a panel off a pin — it has claimed THAT role,
  // so only that row is released; the other tabs' pins must survive. Best-effort: a failed
  // unpin only means the TTL expires it later, which is the behaviour we had all along.
  const q = jobId ? `?job_id=${encodeURIComponent(jobId)}` : "";
  const r = await apiFetch(`/api/pin${q}`, { method: "DELETE" });
  return { cleared: r.ok };
}

async function profileOnly() {
  const r = await apiFetch(`/api/profile`);
  if (!r.ok) throw new Error(`profile ${r.status}`);
  return r.json();
}

async function setApplyProfile(jobId, profile, url) {
  // 切换这个岗位的投递身份（名片）。成功后按 URL 重查一次，把带新名片的 role 还给面板；
  // URL 认不出角色的场景（pin/LinkedIn 中转）拿不到 role，面板自己就地改。
  const r = await apiFetch(`/api/roles/${encodeURIComponent(jobId)}/apply-profile`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ profile }),
  });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch {}
    return { ok: false, error: detail };
  }
  let role = null;
  if (url) {
    try {
      const lk = await lookup(url);
      if (lk.found && lk.role && lk.role.job_id === jobId) role = lk.role;
    } catch {}
  }
  return { ok: true, role };
}

async function markApplied(jobId, variant) {
  const r = await apiFetch(`/api/roles/${encodeURIComponent(jobId)}/status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: "applied", resume_variant: variant || "base" }),
  });
  if (!r.ok) throw new Error(`mark applied ${r.status}`);
  return r.json();
}

// 「这家不让投了」from the panel (owner 2026-09-25): put the company on cooldown for N days or
// until a date, and (per owner) mark the role on screen skip so it leaves the review queue.
async function setCooldown(company, days, until, reason, skipJobId) {
  const r = await apiFetch(`/api/companies/cooldown`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ company, days: days || null, until: until || null, reason: reason || "", skip_job_id: skipJobId || null }),
  });
  if (!r.ok) { let d = `${r.status}`; try { d = (await r.json()).detail || d; } catch {} return { ok: false, error: d }; }
  return { ok: true, ...(await r.json()) };
}
async function clearCooldown(company) {
  const r = await apiFetch(`/api/companies/cooldown/${encodeURIComponent(company)}`, { method: "DELETE" });
  if (!r.ok) { let d = `${r.status}`; try { d = (await r.json()).detail || d; } catch {} return { ok: false, error: d }; }
  return { ok: true };
}

async function capture(payload) {
  const r = await apiFetch(`/api/capture`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) {
    let detail = `capture ${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (e) { /* keep default */ }
    throw new Error(detail);
  }
  return r.json(); // { ok, job_id, company, title, score, verdict }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    try {
      if (msg.type === "lookup") sendResponse(await lookup(msg.url));
      else if (msg.type === "profile") sendResponse({ ok: true, ...(await profileOnly()) });
      else if (msg.type === "topRole") sendResponse(await topRole(msg.company));
      else if (msg.type === "pinned") sendResponse(await pinned());
      else if (msg.type === "pins") sendResponse(await pins());
      else if (msg.type === "unpin") sendResponse({ ok: true, ...(await unpin(msg.jobId)) });
      else if (msg.type === "resume") sendResponse({ ok: true, ...(await resumeBase64(msg.jobId)) });
      else if (msg.type === "answer") sendResponse({ ok: true, ...(await answer(msg.jobId, msg.questions)) });
      else if (msg.type === "emailCode") sendResponse({ ok: true, ...(await emailCode()) });
      else if (msg.type === "salary") sendResponse({ ok: true, ...(await salary(msg.jobId)) });
      else if (msg.type === "coverLetter") sendResponse({ ok: true, ...(await coverLetter(msg.jobId, msg.words)) });
      else if (msg.type === "pickOption") sendResponse({ ok: true, ...(await pickOption(msg.jobId, msg.question, msg.options)) });
      else if (msg.type === "markApplied") sendResponse({ ok: true, ...(await markApplied(msg.jobId, msg.variant)) });
      else if (msg.type === "setProfile") sendResponse(await setApplyProfile(msg.jobId, msg.profile, msg.url));
      else if (msg.type === "cooldown") sendResponse(await setCooldown(msg.company, msg.days, msg.until, msg.reason, msg.skipJobId));
      else if (msg.type === "cooldownClear") sendResponse(await clearCooldown(msg.company));
      else if (msg.type === "capture") sendResponse({ ok: true, ...(await capture(msg.payload)) });
      else sendResponse({ ok: false, error: "unknown message" });
    } catch (e) {
      sendResponse({ ok: false, error: String(e.message || e) });
    }
  })();
  return true; // keep the channel open for the async response
});
