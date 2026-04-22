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
    ["legal_name", /\blegal (full )?name\b/],        // explicit "Legal name" → legal (Alex Example)
    ["first_name", /\bfirst name\b|\bgiven name\b|\blegal first\b/],
    ["last_name", /\blast name\b|\bfamily name\b|\bsurname\b/],
    ["full_name", /\bfull name\b|\byour name\b|^\s*name\b/],
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
    const legalFull = [legalFirst, profile.last_name].filter(Boolean).join(" ");    // Alex Example
    const preferredFull = [preferred, profile.last_name].filter(Boolean).join(" "); // Casey Example
    return {
      first_name: legalFirst, last_name: profile.last_name,     // First name → legal (Alex)
      preferred_name: preferred,                                // Preferred/nickname → Casey
      full_name: preferredFull,                                 // single "Full Name" → Casey Example (résumé)
      legal_name: legalFull,                                    // explicit "Legal name" → Alex Example
      email: profile.email, phone: profile.phone, linkedin: profile.linkedin,
      github: profile.github, website: profile.website,
      location: profile.location, country: profile.country,
    }[key] || "";
  }

  // True when a separate country / dial-code selector sits next to this phone input — then we
  // fill just the national number so we don't duplicate the "+1". A lone phone field keeps "+1".
  function hasDialCodeSelector(el) {
    let n = el.parentElement;
    for (let i = 0; i < 3 && n; i++, n = n.parentElement) {
      if (n.querySelector('select, [class*="iti"], [class*="flag"], [class*="dial"], [class*="country" i]')) return true;
      if (/\+\d{1,3}\b/.test((n.textContent || "").slice(0, 200))) return true;   // a visible "+1" chip
    }
    return false;
  }

  // ---- common eligibility / logistics answers (deterministic, from the profile) ----
  // Unlike EEO/demographic questions (which stay the user's to answer), these are factual
  // questions we CAN answer for her — work authorization, sponsorship, residency, onsite/
  // remote/relocate, "have you worked here before". Answered without the LLM: fast + correct.
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, 300);
  const short = (s) => clean(s).slice(0, 52);

  // Fields that must NEVER be auto-answered — demographics, comp, criminal history. (Work
  // authorization / sponsorship are deliberately NOT here: those we answer from the profile.)
  const SENSITIVE = /gender|ethnic|hispanic|latin|\brace\b|racial|veteran|disab|pronoun|sexual orientation|lgbt|demographic|self.?identif|equal employment|\beeo\b|salary|compensation|desired pay|felony|conviction|criminal|background check|how did you hear|referr/i;

  function fullLoc(p) {
    const city = ((p.location || "").split(",")[0] || "San Jose").trim();
    return `${city}, California, United States`;
  }

  // Returns {yesno:"yes"|"no"} for a Yes/No question, {text, choice} for a value question,
  // or null when we have no confident answer (→ falls through to the AI for open questions).
  function commonAnswer(label, profile) {
    const t = clean(label).toLowerCase();
    if (!t) return null;
    const YES = { yesno: "yes" }, NO = { yesno: "no" };
    const US = profile.country || "United States";
    // eligibility / logistics (Yes/No)
    if (/\bspons/.test(t)) return NO;                                       // need sponsorship? → No
    if (/authori[sz]|legally (allowed|entitled|eligible|authorized)|eligible to work|right to work|permitted to work|work permit|legally.*work/.test(t)) return YES;
    if (/(ever|previously|before|prior|former|formerly).{0,40}(employ|work)/.test(t) || /(employ|work(ed)?)\b.{0,25}(for us|by us|here before|at (this|our) (compan|organi)|our compan|current or former)/.test(t) || /current or former (employee|worker|contractor)/.test(t)) return NO;
    if (/relocat/.test(t)) return YES;
    if (/\bremot/.test(t)) return YES;
    if (/on-?site|onsite|in[- ]office|in person|\bhybrid\b|commut/.test(t)) return YES;
    if (/able to travel|willing.*travel/.test(t)) return YES;
    if (/\b18\b.*(older|age)|at least 18|over 18|of legal age|legally an adult/.test(t)) return YES;
    // residency / country / location (value)
    if (/(which )?country.*(resid|live|based|located|from|in)|(resid|live|based).*country|country of (residence|origin)/.test(t)) return { text: US, choice: US };
    if (/\bcountry\b/.test(t)) return { text: US, choice: US };
    if (/current (city|location)|city\/state|where are you (based|located|currently)|your (current )?location|based in|current residence/.test(t)) return { text: fullLoc(profile) };
    if ((/^\s*state\b|\bstate\/province|\bprovince\b/.test(t)) && !/united states|statement/.test(t)) return { text: "California", choice: "California" };
    if (/able to work (at|in|from)|can you work (at|in|from)|position is (located|based)|role is (in|located|based)|work (at|from) (this|the) (location|office)/.test(t)) return YES;
    return null;
  }

  // The question text for a select / radio group / combobox (fieldset legend, label[for],
  // or the enclosing question container) — richer than fieldText, used for common answers.
  function controlLabel(el) {
    const fs = el.closest("fieldset");
    if (fs) { const lg = fs.querySelector("legend"); if (lg && lg.innerText.trim()) return clean(lg.innerText); }
    const byFor = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (byFor && byFor.innerText.trim()) return clean(byFor.innerText);
    const box = el.closest('.field, .form-field, [class*=field], [class*=question i], [data-qa], [class*=application]');
    if (box) { const lab = box.querySelector('label, legend, .label, [class*=label i]'); if (lab && lab.innerText.trim()) return clean(lab.innerText); }
    return clean(el.getAttribute("aria-label") || questionLabel(el));
  }

  function fillSelect(sel, want) {
    const opts = [...sel.options];
    let target;
    if (want.yesno) {
      const yn = want.yesno === "yes" ? /^\s*y(es)?\b/i : /^\s*no?\b/i;
      target = opts.find((o) => yn.test(o.text));
    }
    if (!target && want.choice) {
      const c = want.choice.toLowerCase();
      target = opts.find((o) => o.text.trim().toLowerCase() === c)
        || opts.find((o) => o.text.toLowerCase().includes(c))
        || (/united states/.test(c) && opts.find((o) => /united states|\bu\.?s\.?a?\.?\b/i.test(o.text)));
    }
    if (!target) return false;
    sel.value = target.value;
    sel.dispatchEvent(new Event("input", { bubbles: true }));
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  function radioLabel(r) {
    const l = r.id && document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
    return ((l && l.innerText) || (r.closest("label") || {}).innerText || r.value || r.getAttribute("aria-label") || "").trim();
  }
  function fillRadio(radios, want) {
    let target;
    if (want.yesno) {
      const yn = want.yesno === "yes" ? /^\s*y(es)?\b/i : /^\s*no?\b/i;
      target = radios.find((r) => yn.test(radioLabel(r)));
    }
    if (!target && want.choice) {
      const c = want.choice.toLowerCase();
      target = radios.find((r) => radioLabel(r).toLowerCase().includes(c));
    }
    if (!target) return false;
    target.click();                                     // click drives framework onChange handlers
    return true;
  }

  // Best-effort for custom dropdowns (Greenhouse react-select, Ashby comboboxes): open it,
  // type the value to filter, then click the matching option. Flaky by nature — logs on miss.
  async function fillCombobox(control, want) {
    control.scrollIntoView({ block: "center" });
    control.click();
    const input = control.querySelector("input") || control.parentElement.querySelector("input");
    if (input) input.focus();
    await sleep(140);
    const wantText = want.choice || (want.yesno === "yes" ? "Yes" : "No");
    if (input) setNativeValue(input, wantText);
    await sleep(240);
    const opts = [...document.querySelectorAll('.select__option, [role=option], [class*="option" i]')]
      .filter((o) => o.offsetParent !== null && o.textContent.trim());
    const c = wantText.toLowerCase();
    const opt = opts.find((o) => o.textContent.trim().toLowerCase() === c)
      || opts.find((o) => o.textContent.toLowerCase().includes(c));
    if (opt) { opt.click(); return true; }
    if (input) input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    return false;
  }

  // Deterministic pass over selects / radio groups / custom dropdowns / value text-fields.
  // Applies profile-based answers to the common questions and RETURNS the truly-open text
  // questions ({el,label}) that are left for the AI.
  async function fillCommon(profile, log) {
    const leftover = [];
    let acted = false;
    const pace = async () => { if (acted) await humanPause(); acted = true; };

    for (const sel of document.querySelectorAll("select")) {
      if (sel.disabled) continue;
      const label = controlLabel(sel);
      if (SENSITIVE.test(label)) continue;
      const ans = commonAnswer(label, profile);
      if (!ans) continue;
      if (sel.selectedIndex > 0 && (sel.value || "").trim()) continue;   // already chosen
      await pace();
      if (fillSelect(sel, ans)) log("selected: " + short(label), "good");
    }

    const seen = new Set();
    for (const r of document.querySelectorAll('input[type=radio]')) {
      if (r.disabled || !r.name || seen.has(r.name)) continue;
      seen.add(r.name);
      const group = [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(r.name)}"]`)];
      if (group.some((x) => x.checked)) continue;                        // already answered
      const label = controlLabel(r);
      if (SENSITIVE.test(label)) continue;
      const ans = commonAnswer(label, profile);
      if (!ans || !ans.yesno) continue;
      await pace();
      if (fillRadio(group, ans)) log("answered: " + short(label), "good");
    }

    for (const ctl of document.querySelectorAll('.select__control, [class*="select__control"], [role=combobox]')) {
      const label = controlLabel(ctl);
      if (!label || SENSITIVE.test(label)) continue;
      const ans = commonAnswer(label, profile);
      if (!ans || !(ans.choice || ans.yesno)) continue;
      if (/\S/.test((ctl.querySelector('[class*="single-value" i], [class*="value" i]') || {}).textContent || "")) continue; // already chosen
      await pace();
      const ok = await fillCombobox(ctl, ans);
      log(ok ? "selected: " + short(label) : "dropdown — pick manually: " + short(label), ok ? "good" : "warn");
    }

    for (const el of document.querySelectorAll('textarea, input[type=text], input[type=search], input:not([type])')) {
      if (el.disabled || el.readOnly || (el.value || "").trim()) continue;
      if (classify(el)) continue;                       // standard profile field — fillFields did it
      const label = questionLabel(el);
      if (!label || label.length < 4 || !/[a-z]/i.test(label)) continue;
      const ans = commonAnswer(label, profile);
      if (ans && ans.text) { await pace(); el.focus(); setNativeValue(el, ans.text); el.blur(); log("filled: " + short(label), "good"); continue; }
      if (SENSITIVE.test(fieldText(el))) continue;      // sensitive & no known answer → user handles
      leftover.push({ el, label });
      if (leftover.length >= 15) break;
    }
    return leftover;
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
      let val = valueFor(key, profile);
      if (!val) continue;
      // phone: drop the +1 country code only when a separate dial-code selector is present
      if (key === "phone" && hasDialCodeSelector(el)) val = val.replace(/^\s*\+?\s*1[\s.\-]+/, "").trim();
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

  // ---- email verification-code field ----
  function codeField() {
    const inputs = document.querySelectorAll(
      'input[type=text], input[type=number], input[type=tel], input[autocomplete="one-time-code"], input:not([type])');
    for (const el of inputs) {
      if (el.disabled || el.readOnly || (el.value || "").trim()) continue;
      const t = fieldText(el);
      if (/\b(zip|postal|area|country|dial|employee|promo|referral|discount|coupon)\b/i.test(t)) continue;
      if (/verification|security code|one[\s-]?time|passcode|confirmation code|\botp\b|verify|access code|\bcode\b/i.test(t))
        return el;
    }
    return null;
  }
  function otpBoxes() {
    const b = [...document.querySelectorAll(
      'input[maxlength="1"], input[autocomplete="one-time-code"]')].filter((el) => !el.disabled && !el.readOnly);
    return b.length >= 4 && b.length <= 8 ? b : null;
  }
  function fillCode(code) {
    const boxes = otpBoxes();
    if (boxes && code.length === boxes.length) {         // split OTP boxes → one digit each
      boxes.forEach((el, i) => { el.focus(); setNativeValue(el, code[i]); });
      boxes[boxes.length - 1].blur();
      return true;
    }
    const el = codeField();
    if (el) { el.focus(); setNativeValue(el, code); el.blur(); return true; }
    return false;
  }

  // ---- floating panel ----
  function panel(role) {
    // Shadow DOM so the host page's CSS can't bleed in and overlap our text.
    const host = document.createElement("div");
    host.id = "jobscan-panel";
    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
      <style>
        :host{ all: initial; display:block; }
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
        button.code{ background:#0e7490; color:#fff; border:0; }
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
          <button class="primary fill">Fill application</button>
          <button class="code" hidden>📧 Get email code</button>
          <button class="ok submitted">✓ I submitted this</button>
          <div class="log"></div>
        </div>
      </div>`;
    // Pin it top-right via inline !important (beats page CSS and survives :host all:initial),
    // and mount on <html> not <body> so a transformed ancestor can't turn `fixed` into
    // scroll-with-page. Top-right keeps it high, visible, and clear of the bottom Submit button.
    host.style.cssText = "position:fixed !important; top:20px !important; right:20px !important;" +
      "left:auto !important; bottom:auto !important; z-index:2147483647 !important; margin:0 !important;";
    document.documentElement.appendChild(host);
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
      const set = (k, v) => host.style.setProperty(k, v, "important"); // must beat the !important pin
      set("right", "auto"); set("bottom", "auto");
      const move = (ev) => {
        set("left", Math.max(0, Math.min(innerWidth - 40, ev.clientX - dx)) + "px");
        set("top", Math.max(0, Math.min(innerHeight - 30, ev.clientY - dy)) + "px");
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
      const profile = role.profile || {};

      // 1) standard text fields (name / email / phone / links / location)
      const line = document.createElement("div"); $(".log").appendChild(line);
      const { filled, skipped } = await fillFields(profile, (k) => {
        const cur = line.textContent.replace(/^fields: /, "");
        line.textContent = "fields: " + (cur ? cur + ", " : "") + k;
      });
      if (!filled.length) line.textContent = "no standard text fields matched";
      if (skipped.length) log("left as-is: " + skipped.join(", "));

      // 2) common eligibility / logistics (dropdowns, Yes/No radios, country, location…)
      const leftover = await fillCommon(profile, log);

      // 3) tailored résumé PDF
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

      // 4) truly-open questions → AI (only if enabled). Common/EEO questions never reach here.
      if (role.ai_enabled && leftover.length) {
        fillBtn.textContent = "✨ drafting…";
        log(`drafting ${leftover.length} open answer(s) with AI…`);
        const res = await send({ type: "answer", jobId: role.job_id, questions: leftover.map((q) => q.label) });
        if (!res || !res.ok) { log("AI answer failed: " + (res && res.error), "warn"); }
        else {
          const answers = res.answers || {}; let n = 0;
          for (const { el, label } of leftover) {
            const a = answers[label];
            if (a && !(el.value || "").trim()) { await humanPause(); el.focus(); setNativeValue(el, a); el.blur(); n++; }
          }
          log(n ? `AI drafted ${n} answer(s) ✓ — read & edit each` : "no AI answers (declined or already filled)", n ? "good" : "warn");
        }
      } else if (leftover.length) {
        log(`${leftover.length} open question(s) left for you (AI is off)`);
      }

      fillBtn.disabled = false; fillBtn.textContent = orig;
      log("— review everything, then submit yourself.");
    };

    // Email verification-code button — only when mailcode is configured (app password present).
    if (role.mailcode_enabled) {
      const codeBtn = $(".code");
      codeBtn.hidden = false;
      codeBtn.onclick = async () => {
        codeBtn.disabled = true; const orig = codeBtn.textContent; codeBtn.textContent = "📧 checking…";
        const res = await send({ type: "emailCode" });
        codeBtn.disabled = false; codeBtn.textContent = orig;
        if (!res || !res.ok) { log("email-code failed: " + (res && res.error), "warn"); return; }
        if (!res.code) { log("no recent verification code found — trigger 'send code', then retry", "warn"); return; }
        const ok = fillCode(res.code);
        log(ok ? `filled code ${res.code} ✓ (${res.subject || "email"})` : `code is ${res.code} — no code field found, paste it`, ok ? "good" : "warn");
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
