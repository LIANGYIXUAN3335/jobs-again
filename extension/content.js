// JobScan content script — runs on Greenhouse / Lever / Ashby application pages
// (incl. when embedded as an iframe on a company careers site). It matches the page to
// a role in your local JobScan, then on your click fills the form + attaches the tailored
// résumé PDF. It NEVER submits — you review and submit yourself.

(function () {
  if (window.__jobscanLoaded) return;
  window.__jobscanLoaded = true;

  const send = (msg) => new Promise((res) => chrome.runtime.sendMessage(msg, res));
  // Human-like pacing between field fills so forms don't flag the burst as a bot.
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const humanPause = () => sleep(450 + Math.floor(Math.random() * 250)); // ~0.45–0.7s

  // ---- form helpers ----
  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function fieldText(el) {
    let s = `${el.name || ""} ${el.id || ""} ${el.placeholder || ""} ${el.getAttribute("aria-label") || ""}`;
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) s += " " + l.innerText;
    }
    const wrapLabel = el.closest("label") || (el.closest(".field, .form-field, [class*=field]") || {}).querySelector?.("label");
    if (wrapLabel) s += " " + wrapLabel.innerText;
    return s.toLowerCase();
  }

  const PATTERNS = [
    // preferred/nickname MUST come before first_name (a "preferred first name" field
    // contains "first name" and would otherwise match legal first_name).
    ["preferred_name", /\bpreferred (first )?name\b|\bnickname\b|\bgoes by\b|\bpreferred\b/],
    ["first_name", /\bfirst name\b|\bgiven name\b|\blegal first\b/],
    ["last_name", /\blast name\b|\bfamily name\b|\bsurname\b/],
    ["full_name", /\bfull name\b|\byour name\b|\blegal name\b|^\s*name\b/],
    ["email", /\be-?mail\b/],
    ["phone", /\bphone\b|\bmobile\b|\btelephone\b/],
    ["linkedin", /\blinkedin\b/],
    ["github", /\bgithub\b/],
    ["website", /\bwebsite\b|\bportfolio\b|personal site/],
    ["location", /\blocation\b|\bcity\b|where are you based|current city/],
    ["country", /\bcountry\b/],
  ];

  // NEVER auto-fill sensitive / voluntary fields — the user handles these by hand.
  const EXCLUDE = /gender|ethnic|hispanic|latin|\brace\b|racial|veteran|disab|pronoun|sexual orientation|lgbt|demographic|self.?identif|equal employment|\beeo\b|salary|compensation|desired pay|referr|how did you hear|sponsor|visa|authoriz|felony|conviction|background check/;

  function classify(el) {
    const t = fieldText(el);
    if (EXCLUDE.test(t)) return null;               // demographics, EEO, comp, work-auth
    for (const [key, re] of PATTERNS) if (re.test(t)) return key;
    return null;
  }

  function valueFor(key, profile) {
    // "First name" / "Full name" / "Legal name" on applications want the LEGAL name (Alex);
    // Casey is the preferred name, used only for an explicit preferred/nickname field.
    const legalFirst = profile.legal_first_name || profile.first_name;
    const preferred = profile.preferred_name || profile.first_name;
    const legalFull = [legalFirst, profile.last_name].filter(Boolean).join(" ");
    return {
      first_name: legalFirst, last_name: profile.last_name,
      preferred_name: preferred, full_name: legalFull,
      email: profile.email, phone: profile.phone, linkedin: profile.linkedin,
      github: profile.github, website: profile.website,
      location: profile.location, country: profile.country,
    }[key] || "";
  }

  // Fills one standard field at a time with a human-like pause between each, so the form's
  // anti-spam heuristics don't flag an instant burst. `onField(key)` reports progress.
  async function fillFields(profile, onField) {
    const filled = [];
    const skipped = [];
    const inputs = document.querySelectorAll(
      'input:not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]), textarea');
    for (const el of inputs) {
      if (el.disabled || el.readOnly) continue;
      const key = classify(el);
      if (!key) continue;
      const val = valueFor(key, profile);
      if (!val) continue;
      if ((el.value || "").trim()) { skipped.push(key + " (already filled)"); continue; }
      if (filled.length) await humanPause();          // pace between fields (skip before the first)
      el.focus();
      setNativeValue(el, val);
      el.blur();
      filled.push(key);
      if (onField) onField(key);
    }
    return { filled, skipped };
  }

  function attachResume(bytes, name) {
    // prefer an input whose context says "resume"/"cv", not "cover letter"
    const files = [...document.querySelectorAll('input[type=file]')];
    let target = files.find((f) => /resume|cv/i.test(fieldText(f)) && !/cover/i.test(fieldText(f)));
    if (!target) target = files.find((f) => !f.accept || /pdf/i.test(f.accept));
    if (!target) return false;
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], name, { type: "application/pdf" }));
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "files").set.call(target, dt.files);
    target.dispatchEvent(new Event("input", { bubbles: true }));
    target.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  function b64ToBytes(b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }

  // ---- open-ended question detection (for AI autofill) ----
  function questionLabel(el) {
    const byFor = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    const wrap = el.closest("label");
    const box = el.closest(".field, .form-field, [class*=field], [class*=question]");
    const boxLabel = box && box.querySelector("label, legend, .label, [class*=label]");
    const cand = [el.getAttribute("aria-label"), byFor && byFor.innerText,
      wrap && wrap.innerText, boxLabel && boxLabel.innerText, el.placeholder];
    for (const c of cand) { const s = (c || "").replace(/\s+/g, " ").trim(); if (s) return s.slice(0, 300); }
    return "";
  }

  function openQuestions() {
    const out = [];
    for (const el of document.querySelectorAll("textarea")) {
      if (el.disabled || el.readOnly || (el.value || "").trim()) continue;
      if (EXCLUDE.test(fieldText(el))) continue;      // never AI-answer EEO / legal / work-auth
      const label = questionLabel(el);
      if (label.length >= 8) out.push({ el, label });  // needs a real question
    }
    return out;
  }

  // ---- floating panel ----
  function panel(role) {
    // Shadow DOM so the host page's CSS can't bleed in and overlap our text.
    const host = document.createElement("div");
    host.id = "jobscan-panel";
    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
      <style>
        :host{ all: initial; position:fixed; right:20px; bottom:20px; z-index:2147483647; }
        *{ box-sizing:border-box; margin:0; padding:0; }
        .wrap{ width:320px; font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
          color:#0f172a; background:#fff; border:1px solid #e2e8f0; border-radius:14px;
          box-shadow:0 12px 32px rgba(15,23,42,.22); overflow:hidden; }
        .hd{ background:linear-gradient(135deg,#1d4470,#0f2942); color:#fff; padding:11px 14px;
          display:flex; justify-content:space-between; align-items:center; cursor:grab; user-select:none; }
        .hd:active{ cursor:grabbing; }
        .brand{ display:flex; align-items:center; gap:8px; font-weight:700; letter-spacing:.2px; font-size:14px; }
        .dot{ width:9px; height:9px; border-radius:50%; background:#4ade80; box-shadow:0 0 0 3px rgba(74,222,128,.25); }
        .ctl{ display:flex; align-items:center; gap:1px; }
        .ctl button{ cursor:pointer; background:transparent; border:0; color:#cbd5e1; font-size:18px; line-height:1;
          padding:2px 7px; margin:0; width:auto; border-radius:6px; font-weight:600; }
        .ctl button:hover{ background:rgba(255,255,255,.14); color:#fff; }
        .wrap.collapsed{ width:auto; }
        .wrap.collapsed .bd{ display:none; }
        .bd{ padding:14px; }
        .role{ font-weight:650; font-size:14px; line-height:1.35; margin-bottom:8px; }
        .chip{ display:inline-block; font-size:11.5px; font-weight:600; padding:3px 9px; border-radius:999px; }
        .chip.ready{ background:#e7f6ec; color:#15803d; }
        .chip.none{ background:#fdecea; color:#b42318; }
        button{ display:flex; align-items:center; justify-content:center; gap:7px; font:inherit; font-weight:600;
          cursor:pointer; border:1px solid #e2e8f0; border-radius:9px; background:#f8fafc; color:#0f172a;
          padding:9px 12px; margin-top:9px; width:100%; transition:filter .12s, background .12s; }
        button:hover{ filter:brightness(.97); }
        button:active{ transform:translateY(.5px); }
        button:disabled{ opacity:.55; cursor:default; }
        button.primary{ background:#12314f; color:#fff; border-color:#12314f; }
        button.ai{ background:linear-gradient(135deg,#6d5efc,#8b5cf6); color:#fff; border:0; }
        button.ok{ background:#15803d; color:#fff; border-color:#15803d; }
        .log{ margin-top:11px; font-size:12px; line-height:1.5; max-height:150px; overflow:auto;
          white-space:pre-wrap; color:#334155; border-top:1px solid #eef2f6; padding-top:9px; }
        .log:empty{ display:none; }
        .log .warn{ color:#b42318; }
        .log .good{ color:#15803d; }
      </style>
      <div class="wrap">
        <div class="hd"><span class="brand"><span class="dot"></span>JobScan</span><span class="ctl"><button class="min" title="minimize">–</button><button class="x" title="close">×</button></span></div>
        <div class="bd">
          <div class="role"></div>
          <div class="status"></div>
          <button class="primary fill">Fill standard fields + résumé</button>
          <button class="ai" hidden>✨ AI-answer open questions</button>
          <button class="ok submitted">✓ I submitted this</button>
          <div class="log"></div>
        </div>
      </div>`;
    document.body.appendChild(host);
    const $ = (s) => shadow.querySelector(s);
    $(".role").textContent = `${role.company} — ${role.title}`;
    $(".status").innerHTML = role.has_tailored_pdf
      ? '<span class="chip ready">tailored résumé ready</span>'
      : '<span class="chip none">no tailored résumé — text only</span>';
    const log = (m, cls) => { const d = document.createElement("div"); if (cls) d.className = cls; d.textContent = m; $(".log").appendChild(d); };

    $(".x").onclick = () => host.remove();

    // minimize / restore — collapse to just the header pill so it never covers the submit button
    const wrap = $(".wrap"), minBtn = $(".min");
    minBtn.onclick = (e) => {
      e.stopPropagation();
      const collapsed = wrap.classList.toggle("collapsed");
      minBtn.textContent = collapsed ? "▢" : "–";
      minBtn.title = collapsed ? "restore" : "minimize";
    };

    // drag the panel by its header (switch from bottom/right anchoring to top/left on first drag)
    $(".hd").addEventListener("mousedown", (e) => {
      if (e.target.closest("button")) return;           // don't start a drag from the ×/– buttons
      const r = host.getBoundingClientRect();
      const dx = e.clientX - r.left, dy = e.clientY - r.top;
      host.style.right = "auto"; host.style.bottom = "auto";
      host.style.left = r.left + "px"; host.style.top = r.top + "px";
      const move = (ev) => {
        host.style.left = Math.max(0, Math.min(innerWidth - 40, ev.clientX - dx)) + "px";
        host.style.top = Math.max(0, Math.min(innerHeight - 30, ev.clientY - dy)) + "px";
      };
      const up = () => { document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
      e.preventDefault();
    });

    const fillBtn = $(".fill");
    fillBtn.onclick = async () => {
      $(".log").textContent = "";
      fillBtn.disabled = true; const orig = fillBtn.textContent; fillBtn.textContent = "filling…";
      const line = document.createElement("div"); $(".log").appendChild(line);
      const { filled, skipped } = await fillFields(role.profile || {}, (k) => {
        line.textContent = "filled: " + (line.textContent.replace(/^filled: /, "") ? line.textContent.replace(/^filled: /, "") + ", " : "") + k;
      });
      if (!filled.length) line.textContent = "no standard fields matched";
      if (skipped.length) log("left as-is: " + skipped.join(", "));
      if (role.has_tailored_pdf) {
        await humanPause();
        const res = await send({ type: "resume", jobId: role.job_id });
        if (res && res.ok) {
          const ok = attachResume(b64ToBytes(res.b64), res.name);
          log(ok ? "résumé PDF attached ✓" : "couldn't find a résumé upload field", ok ? "good" : "warn");
        } else {
          log("résumé fetch failed: " + (res && res.error), "warn");
        }
      }
      fillBtn.disabled = false; fillBtn.textContent = orig;
      log("— review everything, then submit yourself.");
    };

    // AI answer button — only when the local JobScan has AI enabled + a key.
    if (role.ai_enabled) {
      const aiBtn = $(".ai");
      aiBtn.hidden = false;
      aiBtn.onclick = async () => {
        const qs = openQuestions();
        if (!qs.length) { log("no open-ended questions found on this page"); return; }
        aiBtn.disabled = true; const orig = aiBtn.textContent; aiBtn.textContent = "✨ drafting…";
        log(`drafting ${qs.length} answer(s) with AI — legal/EEO questions are skipped…`);
        const res = await send({ type: "answer", jobId: role.job_id, questions: qs.map((q) => q.label) });
        aiBtn.disabled = false; aiBtn.textContent = orig;
        if (!res || !res.ok) { log("AI answer failed: " + (res && res.error), "warn"); return; }
        const answers = res.answers || {};
        let n = 0;
        for (const { el, label } of qs) {
          const a = answers[label];
          if (a && !(el.value || "").trim()) { setNativeValue(el, a); n++; }
        }
        log(n ? `filled ${n} answer(s) ✓ — read & edit each before submitting` : "nothing filled (legal/EEO skipped, or no draft)", n ? "good" : "warn");
      };
    }

    $(".submitted").onclick = async () => {
      const res = await send({ type: "markApplied", jobId: role.job_id, variant: role.resume_variant });
      if (res && res.ok) { log("marked applied in JobScan ✓ (" + role.resume_variant + ")", "good"); $(".submitted").disabled = true; }
      else log("mark-applied failed: " + (res && res.error), "warn");
    };
    return host;
  }

  // ---- boot: the form on Ashby/Greenhouse loads LATE (SPA), so we can't just check
  //      once at load. Poll briefly + watch the DOM + watch for SPA URL changes, and
  //      show the panel as soon as an application form appears for a matching role. ----
  let shownFor = null;   // URL the panel is currently shown for
  let busy = false;      // avoid overlapping lookups while the DOM churns

  async function tryShow() {
    if (busy) return;
    const hasForm = document.querySelector('input[type=file], #first_name, [name*="resume" i]');
    if (!hasForm) return;                                    // not on the application form yet
    if (shownFor === location.href && document.getElementById("jobscan-panel")) return;
    busy = true;
    try {
      const res = await send({ type: "lookup", url: location.href });
      if (!res) { console.warn("[JobScan] no response — is JobScan running on http://127.0.0.1:8787 ?"); return; }
      if (!res.found) { console.info("[JobScan] no matching role for", location.href); return; }
      const old = document.getElementById("jobscan-panel");
      if (old) old.remove();
      shownFor = location.href;
      panel(res.role);
    } finally { busy = false; }
  }

  // 1) poll for ~20s after load (covers the form rendering after a click to "Apply")
  let ticks = 0;
  const poll = setInterval(() => { tryShow(); if (++ticks > 40) clearInterval(poll); }, 500);
  // 2) react to DOM changes (form injected without a reload)
  new MutationObserver(() => tryShow()).observe(document.documentElement, { childList: true, subtree: true });
  // 3) react to SPA navigation (URL changed without a full page load)
  let lastUrl = location.href;
  setInterval(() => {
    if (location.href !== lastUrl) { lastUrl = location.href; shownFor = null; ticks = 0; tryShow(); }
  }, 800);
  tryShow();
})();
