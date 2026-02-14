// JobScan content script — runs on Greenhouse / Lever / Ashby application pages
// (incl. when embedded as an iframe on a company careers site). It matches the page to
// a role in your local JobScan, then on your click fills the form + attaches the tailored
// résumé PDF. It NEVER submits — you review and submit yourself.

(function () {
  if (window.__jobscanLoaded) return;
  window.__jobscanLoaded = true;

  const send = (msg) => new Promise((res) => chrome.runtime.sendMessage(msg, res));

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
    ["first_name", /\bfirst name\b|\bgiven name\b/],
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
    const full = [profile.first_name, profile.last_name].filter(Boolean).join(" ");
    return {
      first_name: profile.first_name, last_name: profile.last_name, full_name: full,
      email: profile.email, phone: profile.phone, linkedin: profile.linkedin,
      github: profile.github, website: profile.website,
      location: profile.location, country: profile.country,
    }[key] || "";
  }

  function fillFields(profile) {
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
      setNativeValue(el, val);
      filled.push(key);
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

  // ---- floating panel ----
  function panel(role) {
    const box = document.createElement("div");
    box.id = "jobscan-panel";
    box.innerHTML = `
      <style>
        #jobscan-panel{position:fixed;right:16px;bottom:16px;z-index:2147483647;width:290px;
          font:13px/1.4 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a1d21;
          background:#fff;border:1px solid #cfd3d8;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.18);overflow:hidden}
        #jobscan-panel .hd{background:#12314f;color:#fff;padding:8px 10px;display:flex;justify-content:space-between;align-items:center}
        #jobscan-panel .hd b{font-size:13px}
        #jobscan-panel .bd{padding:10px}
        #jobscan-panel .role{font-weight:600;margin-bottom:2px}
        #jobscan-panel .muted{color:#6b7280;font-size:12px}
        #jobscan-panel button{font:inherit;cursor:pointer;border:1px solid #cfd3d8;border-radius:6px;background:#f6f8fb;padding:6px 10px;margin-top:8px;width:100%}
        #jobscan-panel button.primary{background:#12314f;color:#fff;border-color:#12314f}
        #jobscan-panel button.ok{background:#14632c;color:#fff;border-color:#14632c}
        #jobscan-panel .x{cursor:pointer;background:none;border:none;color:#fff;width:auto;margin:0;font-size:16px;padding:0 2px}
        #jobscan-panel .log{margin-top:8px;font-size:12px;max-height:120px;overflow:auto;white-space:pre-wrap}
        #jobscan-panel .warn{color:#b3261e}
      </style>
      <div class="hd"><b>JobScan</b><button class="x" title="close">×</button></div>
      <div class="bd">
        <div class="role"></div>
        <div class="muted resume"></div>
        <button class="primary fill">Fill this application</button>
        <button class="ok submitted">✓ I submitted this</button>
        <div class="log"></div>
      </div>`;
    document.body.appendChild(box);
    const $ = (s) => box.querySelector(s);
    $(".role").textContent = `${role.company} — ${role.title}`;
    $(".resume").textContent = role.has_tailored_pdf
      ? "tailored résumé PDF ready to attach"
      : "no tailored résumé yet — text fields only";
    const log = (m, warn) => { const d = document.createElement("div"); if (warn) d.className = "warn"; d.textContent = m; $(".log").appendChild(d); };

    $(".x").onclick = () => box.remove();

    $(".fill").onclick = async () => {
      $(".log").textContent = "";
      const { filled, skipped } = fillFields(role.profile || {});
      log(filled.length ? "filled: " + filled.join(", ") : "no standard fields matched");
      if (skipped.length) log("left as-is: " + skipped.join(", "));
      if (role.has_tailored_pdf) {
        const res = await send({ type: "resume", jobId: role.job_id });
        if (res && res.ok) {
          const ok = attachResume(b64ToBytes(res.b64), res.name);
          log(ok ? "résumé PDF attached ✓" : "couldn't find a résumé upload field", !ok);
        } else {
          log("résumé fetch failed: " + (res && res.error), true);
        }
      }
      log("— review everything, then submit yourself.");
    };

    $(".submitted").onclick = async () => {
      const res = await send({ type: "markApplied", jobId: role.job_id, variant: role.resume_variant });
      if (res && res.ok) { log("marked applied in JobScan ✓ (" + role.resume_variant + ")"); $(".submitted").disabled = true; }
      else log("mark-applied failed: " + (res && res.error), true);
    };
    return box;
  }

  // ---- boot: only act if this frame actually holds an application form ----
  async function boot() {
    const hasForm = document.querySelector('input[type=file], #first_name, [name*="resume" i]');
    if (!hasForm) return;
    const res = await send({ type: "lookup", url: location.href });
    if (!res) { console.warn("[JobScan] no response from extension"); return; }
    if (!res.found) { console.info("[JobScan] no matching role for this page"); return; }
    panel(res.role);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
