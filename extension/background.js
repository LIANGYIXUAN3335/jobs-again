// Service worker: the ONLY part that talks to the local JobScan API.
// Content scripts can't fetch http://localhost from an https page (mixed content),
// but the service worker has its own extension origin + host_permissions, so it can.

const BASES = ["http://127.0.0.1:8787", "http://localhost:8787"];

async function apiFetch(path, init) {
  let lastErr;
  for (const base of BASES) {
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
  const r = await apiFetch(`/api/lookup?url=${encodeURIComponent(url)}`);
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

async function markApplied(jobId, variant) {
  const r = await apiFetch(`/api/roles/${encodeURIComponent(jobId)}/status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: "applied", resume_variant: variant || "base" }),
  });
  if (!r.ok) throw new Error(`mark applied ${r.status}`);
  return r.json();
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    try {
      if (msg.type === "lookup") sendResponse(await lookup(msg.url));
      else if (msg.type === "resume") sendResponse({ ok: true, ...(await resumeBase64(msg.jobId)) });
      else if (msg.type === "answer") sendResponse({ ok: true, ...(await answer(msg.jobId, msg.questions)) });
      else if (msg.type === "emailCode") sendResponse({ ok: true, ...(await emailCode()) });
      else if (msg.type === "markApplied") sendResponse({ ok: true, ...(await markApplied(msg.jobId, msg.variant)) });
      else sendResponse({ ok: false, error: "unknown message" });
    } catch (e) {
      sendResponse({ ok: false, error: String(e.message || e) });
    }
  })();
  return true; // keep the channel open for the async response
});
