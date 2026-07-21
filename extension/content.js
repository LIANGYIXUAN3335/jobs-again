// JobScan content script — runs on Greenhouse / Lever / Ashby application pages
// (incl. when embedded as an iframe on a company careers site). It matches the page to
// a role in your local JobScan, then on your click fills the form + attaches the tailored
// résumé PDF. It NEVER submits — you review and submit yourself.

(function () {
  if (window.__jobscanLoaded) return;
  window.__jobscanLoaded = true;

  // Reloading the unpacked extension kills the background context but leaves THIS already-
  // injected script running on the page — every later sendMessage then throws "Extension
  // context invalidated". Catch that and report it as the one thing that fixes it: reload
  // the page. chrome.runtime.id goes undefined the moment the context dies.
  const CONTEXT_DEAD = "context-invalidated";
  const send = (msg) => new Promise((res) => {
    try {
      if (!chrome.runtime || !chrome.runtime.id) return res({ ok: false, error: CONTEXT_DEAD });
      chrome.runtime.sendMessage(msg, (r) => {
        const err = chrome.runtime.lastError;
        if (err) {
          return res({ ok: false, error: /context invalidated|receiving end/i.test(err.message) ? CONTEXT_DEAD : err.message });
        }
        res(r);
      });
    } catch (e) {
      const m = String((e && e.message) || e);
      res({ ok: false, error: /context invalidated|Extension context/i.test(m) ? CONTEXT_DEAD : m });
    }
  });
  const isContextDead = (r) => !!r && r.error === CONTEXT_DEAD;
  // Human-like pacing between field fills so forms don't flag the burst as a bot.
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const humanPause = () => sleep(180 + Math.floor(Math.random() * 140)); // ~0.18–0.32s

  // ---- form helpers ----
  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // Is this input actually a DROPDOWN / typeahead (react-select, Ashby combobox, Workday prompt)
  // rather than a free-text box? Typing into those silently does nothing — the widget only
  // accepts a real option selection. Detect first, then route to the option-picker instead.
  function isDropdownish(el) {
    if (!el) return false;
    if (el.getAttribute("role") === "combobox") return true;
    if (el.getAttribute("aria-haspopup") || el.getAttribute("aria-autocomplete")) return true;
    if (el.hasAttribute("aria-expanded") || el.hasAttribute("aria-controls")) return true;
    if (el.closest('[class*="select__control"],[class*="select__value"],[role=combobox],[role=listbox]')) return true;
    return false;
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
    // FIRST: "How do you pronounce your name?" contains "name", so it would otherwise be
    // caught by one of the name patterns below and get filled with the name itself.
    ["name_pronunciation", /pronounc|phonetic|how (do|would) (you |we )?say your name/],
    // Order matters: "preferred last name" / "preferred first name" both contain "preferred name",
    // so the specific ones must be tested first. A bare "Preferred name" gets the FULL name.
    ["preferred_last_name", /\bpreferred (last|family|sur)\s?name\b/],
    ["preferred_first_name", /\bpreferred first name\b|\bpreferred given name\b/],
    ["preferred_name", /\bpreferred (full )?name\b|\bnickname\b|\bgoes by\b|\bpreferred\b/],
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
  // ("how did you hear" is deliberately NOT here — it's answered as LinkedIn by commonAnswer.)
  const EXCLUDE = /gender|ethnic|hispanic|latin|\brace\b|racial|veteran|disab|pronoun|sexual orientation|lgbt|demographic|self.?identif|equal employment|\beeo\b|salary|compensation|desired pay|referr|sponsor|visa|authoriz|felony|conviction|background check/;

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
      preferred_name: preferredFull,                            // bare "Preferred name" → Casey Example
      preferred_first_name: preferred,                          // "Preferred first name" → Casey
      preferred_last_name: profile.preferred_last_name || profile.last_name,   // → Example
      name_pronunciation: profile.name_pronunciation,           // "How do you pronounce…"
      full_name: preferredFull,                                 // single "Full Name" → Casey Example (résumé)
      legal_name: legalFull,                                    // explicit "Legal name" → Alex Example
      email: profile.email, phone: profile.phone, linkedin: profile.linkedin,
      github: profile.github, website: profile.website,
      location: profile.location, country: profile.country,
    }[key] || "";
  }

  // The phone box always gets the NATIONAL number — never a leading +1. Application forms pair
  // it with a separate country-code field (which we fill with "United States (+1)"), and a "+1"
  // in both is either rejected or stored as a mangled "+1 +1 408…". Note the character class
  // deliberately excludes "(" so "202-555-0142" → "202-555-0142", not "408) 555-0142".
  const nationalPhone = (profile) => String(profile.phone || "").replace(/^\s*\+?\s*1[\s.\-]*/, "").trim();

  // We never overwrite a field that already has a value — except this one. Workday pre-fills
  // the phone from the saved candidate account, country code and all, so the "+1" you see is
  // its value, not ours, and the skip-if-filled rule meant it just stayed. When the box holds
  // a country-coded number AND a separate country-code field exists, rewrite it to the
  // national number. A plain "(408)…" or a local number starting with 1 is left alone.
  function fixPhoneCountryCode(el, profile, log) {
    if (!el) return false;
    const cur = (el.value || "").trim();
    if (!cur) return false;
    if (!/^\s*\+/.test(cur) && !/^\s*1[\s.\-(]/.test(cur)) return false;
    const want = nationalPhone(profile) || cur.replace(/^\s*\+?\s*1[\s.\-]*/, "").trim();
    if (!want || want === cur) return false;
    el.focus();
    setNativeValue(el, want);
    el.blur();
    log("phone: dropped the leading +1 → " + want, "good");
    return true;
  }

  // (kept for forms that show a dial-code chip; the number itself is stripped either way)
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
  const SENSITIVE = /gender|ethnic|hispanic|latin|\brace\b|racial|veteran|disab|pronoun|sexual orientation|lgbt|demographic|self.?identif|equal employment|\beeo\b|salary|compensation|desired pay|felony|conviction|criminal|background check|referr/i;

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
    // acknowledgements / confirmations the form needs a Yes on
    if (/\b(do you |please )?(accept|confirm|acknowledge|agree)\b/.test(t) && !/decline/.test(t)) return YES;
    // "how/where did you hear about this job" — answer LinkedIn (owner's pick)
    if (/(how|where) did you (hear|learn|find)|hear about (us|this)|source of (this )?(job|opportunity|application)/.test(t))
      return { text: "LinkedIn", choice: "LinkedIn" };
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
  // (react-select / combobox dropdowns are handled by pickReactSelect below — the old
  // click-then-type approach never opened react-select, which mounts its menu on mousedown.)
  const _fillComboboxRemoved = true;

  // ---- voluntary EEO / demographic self-ID ----
  // Per-field: each voluntary self-ID question is answered with YOUR configured preference
  // (config `eeo` map, e.g. gender/race/veteran/disability → the exact answer you chose).
  // A field with no preference set defaults to "I don't wish to answer". The extension picks
  // the option matching your preference (or decline) and still stops before submit, so you review.
  // Greenhouse renders these as react-select (custom dropdowns): mousedown on the control opens
  // the menu (aria-controls → its listbox), then mousedown+mouseup+click on the option selects it.
  const DEMOGRAPHIC = /gender|ethnic|hispanic|latin|\brace\b|racial|veteran|disab|self.?identif|demographic/i;
  const DECLINE = /don'?t wish to answer|do not wish to answer|decline to (self.?identif(y|ies)|answer|state)|do not want to answer|prefer not to (say|answer|disclose|respond)|choose not to/i;

  function comboLabel(input) {
    const lbId = (input.getAttribute("aria-labelledby") || "").split(" ")[0];
    if (lbId) { const el = document.getElementById(lbId); if (el && el.innerText.trim()) return clean(el.innerText); }
    return controlLabel(input);
  }

  // Which config `eeo` preference applies to this question (by its label).
  function eeoKey(label) {
    const t = label.toLowerCase();
    if (/gender identity/.test(t)) return "gender_identity";
    if (/gender/.test(t)) return "gender";
    if (/hispanic|latin/.test(t)) return "hispanic_latino";
    if (/\brace\b|racial|ethnic/.test(t)) return "race";
    if (/veteran/.test(t)) return "veteran";
    if (/disab/.test(t)) return "disability";
    return null;
  }

  // Realistic pointer sequence at the element's on-screen CENTER — the only thing that reliably
  // opens/commits Greenhouse's react-select. A bare synthetic `mousedown` is flaky; dispatching
  // the full pointer+mouse+click sequence on the element actually hit at those coords (the child
  // react-select listens on) works every time. Verified live on the MongoDB greenhouse form.
  function coordSeq(el) {
    el.scrollIntoView({ block: "center" });
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const hit = document.elementFromPoint(x, y);
    const target = hit && el.contains(hit) ? hit : el;      // ignore an overlay (e.g. our own panel)
    for (const t of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      const E = t.startsWith("pointer") ? PointerEvent : MouseEvent;
      target.dispatchEvent(new E(t, { bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y, pointerId: 1 }));
    }
  }

  function escapeRe(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

  // Open a react-select (`control` + its combobox `input`) and click the option matching `want`
  // (RegExp), else `fallback`. Leaves an already-answered field untouched. Returns {ok, picked}.
  async function pickReactSelect(control, input, want, fallback) {
    if (!control || !input) return { ok: false };
    const now = () => (control.innerText || "").replace(/\s+/g, " ").trim();
    const cur = now();
    if (want.test(cur)) return { ok: true, picked: cur };            // already the wanted answer
    if (cur && !/^select\.\.\.$/i.test(cur)) return { ok: false, picked: cur }; // already something else → don't override
    if (input.getAttribute("aria-expanded") === "true") {            // a full seq on an OPEN menu toggles it shut
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); await sleep(120);
    }
    coordSeq(control);                                                // open
    let opt = null, options = [];
    for (let i = 0; i < 10 && !opt; i++) {                            // menu renders async — poll ITS listbox
      await sleep(80);
      const menu = document.getElementById(input.getAttribute("aria-controls") || "");
      if (menu) { options = [...menu.querySelectorAll("[role=option]")]; opt = options.find((o) => want.test(o.textContent)); }
    }
    if (!opt && fallback) opt = options.find((o) => fallback.test(o.textContent));
    if (!opt) { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); return { ok: false, picked: cur }; }
    coordSeq(opt);                                                    // select
    for (let i = 0; i < 10 && now() === cur; i++) await sleep(40);    // leave as soon as it commits
    return { ok: true, picked: now() };
  }

  // Async typeahead (location/city pickers): the suggestions are fetched only AFTER you type,
  // and they come back worded differently from what we typed ("San Jose, CA" → "San Jose, CA,
  // USA"), so an exact match would always miss. Type to filter, wait for the fetch, take the
  // FIRST suggestion — which is what a human does here.
  async function pickTypeahead(control, input, text) {
    if (!control || !input || !text) return { ok: false };
    if (input.getAttribute("aria-expanded") === "true") {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await sleep(120);
    }
    coordSeq(control);                                   // open
    await sleep(60);
    input.focus();
    setNativeValue(input, text);                         // filter
    let opts = [];
    for (let i = 0; i < 22 && !opts.length; i++) {        // async fetch — poll fast, bail late
      await sleep(80);
      const menu = document.getElementById(input.getAttribute("aria-controls") || "");
      opts = menu
        ? [...menu.querySelectorAll("[role=option]")]
        : [...document.querySelectorAll('[role=option],[class*="select__option"]')].filter((o) => o.offsetParent);
    }
    if (!opts.length) { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); return { ok: false }; }
    const before = (control.innerText || "").trim();
    coordSeq(opts[0]);                                   // take the FIRST suggestion
    for (let i = 0; i < 10 && (control.innerText || "").trim() === before; i++) await sleep(40);
    return { ok: true, picked: (control.innerText || "").replace(/\s+/g, " ").trim() };
  }

  // EEO: pick the option matching your configured preference; empty/decline pref → the decline
  // option; a preference the form doesn't offer falls back to decline. A Yes/No-style pref
  // ("No, I don't have a disability") matches on its leading Yes/No, since forms word the rest
  // differently ("No, I do not have a disability and have not had one in the past").
  async function selectEeoOption(control, input, wanted) {
    const wantDecline = !wanted || DECLINE.test(wanted);
    let want;
    if (wantDecline) want = DECLINE;
    else {
      const yn = /^(yes|no)\b/i.exec(wanted.trim());
      want = yn ? new RegExp("^\\s*" + yn[1], "i") : new RegExp(escapeRe(wanted), "i");
    }
    return pickReactSelect(control, input, want, wantDecline ? null : DECLINE);
  }

  async function fillDemographics(prefs, log) {
    prefs = prefs || {};
    let acted = false;
    let did = 0;                    // count for untilSettled — some forms reveal race only
    const seen = new Set();         // after Hispanic/Latino is answered
    for (const input of document.querySelectorAll("input[role=combobox]")) {
      const label = comboLabel(input);
      if (!label || !DEMOGRAPHIC.test(label) || seen.has(label)) continue;
      seen.add(label);
      const key = eeoKey(label);
      const wanted = key && prefs[key] != null ? String(prefs[key]).trim() : "";  // "" → decline
      const control = input.closest('[class*="control"]') || input.parentElement;
      if (!control) continue;
      if (acted) await humanPause();                             // pace + let react settle between fields
      acted = true;
      const r = await selectEeoOption(control, input, wanted);
      if (r.ok) did++;
      log(r.ok ? `${short(label)} → ${short(r.picked)}` : `${short(label)} — set manually`, r.ok ? "good" : "warn");
    }
    // every consent / agreement box on the page (demographic consent, terms, privacy…)
    did += tickConsents(log);
    return did;
  }

  // Deterministic pass over selects / radio groups / custom dropdowns / value text-fields.
  // Applies profile-based answers to the common questions and RETURNS the truly-open text
  // questions ({el,label}) that are left for the AI.
  // Open a sentence-option dropdown, read its real options, ask the server (AI) which one fits
  // this candidate, and select it. Returns true if it committed a choice. Used only when the
  // deterministic rules can't answer — the options are worded per-company and can't be hardcoded.
  async function aiPickCombo(input, control, label, role, log) {
    if (input.getAttribute("aria-expanded") === "true") {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); await sleep(120);
    }
    coordSeq(control);
    let optEls = [];
    for (let i = 0; i < 12 && !optEls.length; i++) {
      await sleep(80);
      const menu = document.getElementById(input.getAttribute("aria-controls") || "");
      optEls = menu ? [...menu.querySelectorAll("[role=option]")] : [];
    }
    const options = optEls.map((o) => o.textContent.trim()).filter(Boolean);
    if (!options.length) { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); return false; }
    const res = await send({ type: "pickOption", jobId: role.job_id, question: label, options });
    const idx = res && typeof res.index === "number" ? res.index : -1;
    if (idx < 0 || idx >= optEls.length) { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); return false; }
    coordSeq(optEls[idx]);
    await sleep(200);
    log("AI picked: " + short(label) + " → " + short(options[idx]) + " — verify it", "good");
    return true;
  }

  async function fillCommon(profile, log, role) {
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

    const seenCombo = new Set();
    for (const input of document.querySelectorAll("input[role=combobox]")) {
      const label = comboLabel(input);
      if (!label || seenCombo.has(label)) continue;
      seenCombo.add(label);
      const control = input.closest('[class*="control"]') || input.parentElement;
      const cur = (control.innerText || "").replace(/\s+/g, " ").trim();
      if (cur && !/^select\.\.\.$/i.test(cur)) continue;              // already chosen
      if (DEMOGRAPHIC.test(label)) continue;                          // handled by fillDemographics

      const ans = commonAnswer(label, profile);
      let done = false;
      if (ans && (ans.choice || ans.yesno)) {
        const want = ans.yesno ? (ans.yesno === "yes" ? /^\s*yes\b/i : /^\s*no\b/i)
          : new RegExp(escapeRe(ans.choice), "i");
        await pace();
        const r = await pickReactSelect(control, input, want, null);
        if (r.ok) { log("selected: " + short(label) + " → " + short(r.picked), "good"); done = true; }
      }
      // Deterministic rule missed (or the options are full sentences like Work Authorization /
      // "willing to relocate") → open it, read the real options, let the AI pick from the profile.
      if (!done && role && role.ai_enabled && role.auto_dropdowns !== false) {
        await pace();
        const picked = await aiPickCombo(input, control, label, role, log);
        done = picked;
      }
      if (!done && !SENSITIVE.test(label)) log("dropdown — pick manually: " + short(label), "warn");
    }

    for (const el of document.querySelectorAll('textarea, input[type=text], input[type=search], input:not([type])')) {
      if (el.disabled || el.readOnly || (el.value || "").trim()) continue;
      if (classify(el)) continue;                       // standard profile field — fillFields did it
      const label = questionLabel(el);
      if (!label || label.length < 4 || !/[a-z]/i.test(label)) continue;
      const ans = commonAnswer(label, profile);
      // value answer (country/location) OR a Yes/No question rendered as a free-text box
      // ("Have you previously worked at <company>?" → No; "Authorized to work?" → Yes).
      const val = ans && (ans.text || (ans.yesno ? (ans.yesno === "yes" ? "Yes" : "No") : ""));
      if (val) { await pace(); el.focus(); setNativeValue(el, val); el.blur(); log("filled: " + short(label), "good"); continue; }
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
      // phone: always the national number — the "+1" belongs in the country-code field, and
      // sniffing for a dial-code selector was unreliable (a miss left "+1" in the box)
      if (key === "phone") val = nationalPhone(profile);
      // the one field we DO correct when it's already filled — a pre-filled "+1 …" is wrong
      if (key === "phone" && fixPhoneCountryCode(el, profile, (m, c) => skipped.push(m))) continue;
      if ((el.value || "").trim()) { skipped.push(key + " (already filled)"); continue; }
      if (filled.length) await humanPause();          // pace between fields (skip before the first)
      // A dropdown disguised as a text input (location typeaheads, country pickers…) must be
      // SELECTED, not typed into — otherwise the value looks filled but never registers.
      if (isDropdownish(el)) {
        const control = el.closest('[class*="control"]') || el.parentElement;
        // Match by what the dropdown should CONTAIN, not the exact stored string: a "how do
        // you pronounce your name" dropdown offers "Casey", never the whole spoken sentence,
        // and a gender/pronoun picker offers a word, not a paragraph. Fall back to the first
        // token of the stored value so those still land.
        const want = key === "name_pronunciation"
          ? new RegExp(escapeRe(profile.preferred_name || profile.first_name || "Casey"), "i")
          : new RegExp(escapeRe(val), "i");
        const r = /^(location|country)$/.test(key)
          ? await pickTypeahead(control, el, val)
          : await pickReactSelect(control, el, want, null);
        if (r.ok) { filled.push(key + " ▾"); if (onField) onField(key); }
        else skipped.push(key + " (dropdown — pick manually)");
        continue;
      }
      el.focus();
      setNativeValue(el, val);
      el.blur();
      filled.push(key);
      if (onField) onField(key);
    }
    return { filled, skipped };
  }

  // Greenhouse custom forms (Lyft etc.) have a REPEATABLE Employment + Education section the
  // standard filler ignored. Real field IDs, read live off the Lyft form:
  //   Employment[N]: company-name-N, title-N, start-date-month-N (react-select, full month name),
  //                  start-date-year-N (text), end-date-month/year-N, current-N (checkbox)
  //   Education[N]:  school--N, degree--N, discipline--N  (all react-select / typeahead)
  //   plus an "Add another" button under each section heading.
  const MONTH_NAME = ["", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"];
  const monthName = (mm) => MONTH_NAME[parseInt(mm, 10)] || "";

  function addAnotherFor(sectionRe) {
    const head = [...document.querySelectorAll("h2,h3,h4,legend,label,b")]
      .find((h) => sectionRe.test((h.innerText || "").trim()));
    if (!head) return null;
    let c = head.parentElement;
    for (let i = 0; i < 6 && c; i++, c = c.parentElement) {
      const b = [...c.querySelectorAll("button,a")].find((x) => x.offsetParent && /add another/i.test(x.innerText || ""));
      if (b) return b;
    }
    return null;
  }

  async function ghSetCombo(id, value) {                 // react-select by id → pick matching option
    const el = document.getElementById(id);
    if (!el || !value) return false;
    const control = el.closest('[class*="control"]') || el.parentElement;
    const r = await pickReactSelect(control, el, new RegExp("^\\s*" + escapeRe(value), "i"), null);
    return r.ok;
  }
  async function ghSetTypeahead(id, value) {             // school etc. → type + take first suggestion
    const el = document.getElementById(id);
    if (!el || !value) return false;
    const control = el.closest('[class*="control"]') || el.parentElement;
    const r = await pickTypeahead(control, el, value);
    return r.ok;
  }
  function ghSetText(id, value) {
    const el = document.getElementById(id);
    if (!el || !value || (el.value || "").trim()) return false;
    el.focus(); setNativeValue(el, value); el.blur();
    return true;
  }

  async function fillGreenhouseHistory(role, log) {
    const jobs = role.experience || [];
    const edu = (role.profile || {}).education || [];

    // ---- Employment ----
    for (let i = 0; i < jobs.length; i++) {
      const j = jobs[i];
      if (i > 0 && !document.getElementById(`company-name-${i}`)) {
        const add = addAnotherFor(/^employment/i);
        if (!add) { log(`only room for ${i} job(s) on this form — add the rest by hand`, "warn"); break; }
        add.click();
        for (let t = 0; t < 20 && !document.getElementById(`company-name-${i}`); t++) await sleep(150);
      }
      if (!document.getElementById(`company-name-${i}`)) break;
      ghSetText(`company-name-${i}`, j.company);
      ghSetText(`title-${i}`, j.title);
      ghSetText(`start-date-year-${i}`, j.start_year);
      await ghSetCombo(`start-date-month-${i}`, monthName(j.start_month));
      if (j.current) {
        const c = document.getElementById(`current-${i}`);
        if (c && !c.checked) c.click();
      } else {
        ghSetText(`end-date-year-${i}`, j.end_year);
        await ghSetCombo(`end-date-month-${i}`, monthName(j.end_month));
      }
      log("employment: " + j.company, "good");
      await humanPause();
    }

    // ---- Education ----
    for (let i = 0; i < edu.length; i++) {
      const e = edu[i];
      if (i > 0 && !document.getElementById(`school--${i}`)) {
        const add = addAnotherFor(/^education/i);
        if (!add) break;
        add.click();
        for (let t = 0; t < 20 && !document.getElementById(`school--${i}`); t++) await sleep(150);
      }
      if (!document.getElementById(`school--${i}`)) break;
      await ghSetTypeahead(`school--${i}`, e.school);
      const degWord = /master/i.test(e.degree) ? "Master" : /bachelor/i.test(e.degree) ? "Bachelor" : e.degree;
      await ghSetCombo(`degree--${i}`, degWord);
      await ghSetTypeahead(`discipline--${i}`, e.discipline);
      ghSetText(`start-date-year--${i}`, e.start_year);   // present on some tenants
      ghSetText(`end-date-year--${i}`, e.end_year);
      log("education: " + e.school, "good");
      await humanPause();
    }
  }

  function attachResume(bytes, name, preferred) {
    // prefer an input whose context says "resume"/"cv", not "cover letter"
    let target = preferred;
    if (!target) {
      const files = [...document.querySelectorAll('input[type=file]')];
      target = files.find((f) => /resume|cv/i.test(fieldText(f)) && !/cover/i.test(fieldText(f)));
      if (!target) target = files.find((f) => !f.accept || /pdf/i.test(f.accept));
    }
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

  // ================= Workday (*.myworkdayjobs.com) =================
  // Workday is a 7-8 step wizard. We fill the CURRENT step only; you click "Save and Continue"
  // (and the final Submit) yourself. Hard-won rules baked in below:
  //   1. An OPEN prompt blocks validation and reports a MISLEADING "required" error on a
  //      DIFFERENT field — so we always blur/close prompts when we're done.
  //   2. A prompt needs a LONG beat (~1.5s) between opening it and clicking an option;
  //      doing both back-to-back silently fails.
  //   3. Date sub-fields are role=spinbutton and ignore a plain value-set. We drive them
  //      through their own key handling instead, and say so in the log when it doesn't take.
  const IS_WORKDAY = /(^|\.)myworkdayjobs\.com$/i.test(location.hostname);

  const wdBoxes = () => [...document.querySelectorAll('[data-automation-id^="formField"]')].filter((b) => b.offsetParent);
  const wdBox = (re) => wdBoxes().find((b) => re.test(b.innerText));
  const wdById = (suffix) => [...document.querySelectorAll("input,textarea")]
    .find((e) => e.offsetParent && new RegExp(suffix + "$", "i").test(e.id || ""));

  // Some forms REVEAL a follow-up only after you answer the one before it — the classic being
  // "Are you Hispanic or Latino? → No", which then unhides the race/ethnicity question. A single
  // pass never sees those, so re-run the filler while it keeps finding new things to answer.
  async function untilSettled(fn, passes = 3) {
    let total = 0;
    for (let i = 0; i < passes; i++) {
      await sleep(350);                    // give the newly-revealed field time to render
      const n = (await fn()) || 0;
      total += n;
      if (!n) break;                       // a pass that changed nothing → we're done
    }
    return total;
  }

  // Consent / acknowledgement / terms boxes — tick them all, on every step. Owner's call:
  // she reviews the whole application before submitting. The ONLY thing skipped is a checkbox
  // that belongs to a multi-option group (ethnicity, "check all that apply"): those are
  // ANSWERS, not agreements, and blanket-ticking them would put words in her mouth.
  const CONSENT_RE = /consent|acknowledg|i agree|i accept|i certify|i confirm|i have read|terms|privacy|gdpr|understand and agree|authoriz/i;

  function tickConsents(log) {
    let n = 0;
    for (const c of document.querySelectorAll('input[type=checkbox]')) {
      if (c.disabled || c.checked || !c.offsetParent) continue;
      const group = c.closest('fieldset,[role=group],[data-automation-id*="CheckboxGroup"]');
      if (group && group.querySelectorAll('input[type=checkbox]').length > 1) continue;   // an answer
      let t = `${c.name || ""} ${c.id || ""}`;
      if (c.id) { const l = document.querySelector(`label[for="${CSS.escape(c.id)}"]`); if (l) t += " " + l.innerText; }
      let p = c;
      for (let i = 0; i < 4 && p; i++, p = p.parentElement) t += " " + (p.innerText || "");
      if (!CONSENT_RE.test(t)) continue;
      c.click();
      if (c.checked) n++;
    }
    if (n) log(`checked ${n} consent box(es)`, "good");
    return n;
  }

  function wdBlur() {                                  // rule 1 — never leave a prompt open
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    document.body.click();
  }

  function wdSetText(el, value) {
    if (!el || !value || (el.value || "").trim()) return false;
    if (isDropdownish(el)) return false;   // it's a prompt, not a text box — caller must pick an option
    el.scrollIntoView({ block: "center" });
    el.focus();
    setNativeValue(el, value);
    el.blur();
    return true;
  }

  // Open a "Select One" prompt for the question matching `questionRe` and click the option
  // matching `wantRe`. Returns "ok" | "already" | "no-field" | "no-option".
  async function wdPrompt(questionRe, wantRe) {
    const box = wdBox(questionRe);
    if (!box) return "no-field";
    if (!/select one/i.test(box.innerText)) return "already";   // already answered — don't touch
    const btn = box.querySelector("button");
    if (!btn) return "no-field";
    btn.scrollIntoView({ block: "center" });
    await sleep(80);
    btn.click();
    // rule 2 — the menu is NOT clickable the instant it opens. Poll for it rather than
    // sleeping a flat 1.5s: Workday usually renders in ~200-400ms, so waiting the worst case
    // every time made each dropdown feel sluggish. Same reliability, a fraction of the wait.
    let opts = [];
    for (let i = 0; i < 24 && !opts.length; i++) {
      await sleep(70);
      opts = [...document.querySelectorAll('[role=option],[data-automation-id="promptOption"]')]
        .filter((o) => o.offsetParent && o.textContent.trim());
    }
    const hit = opts.find((o) => wantRe.test(o.textContent.trim()));
    if (!hit) { wdBlur(); return "no-option"; }
    hit.click();
    for (let i = 0; i < 8 && /select one/i.test(box.innerText); i++) await sleep(60);  // confirm it took
    return "ok";
  }

  // Radio / checkbox groups (e.g. "Have you ever worked for X?" on My Information).
  async function wdChoice(questionRe, wantRe) {
    const box = wdBox(questionRe);
    if (!box) return "no-field";
    const inputs = [...box.querySelectorAll('input[type=radio],input[type=checkbox]')];
    if (!inputs.length) return "no-field";
    if (inputs.some((i) => i.checked && wantRe.test(labelFor(i)))) return "already";
    const hit = inputs.find((i) => wantRe.test(labelFor(i)));
    if (!hit) return "no-option";
    hit.scrollIntoView({ block: "center" });
    hit.click();
    await sleep(400);
    return hit.checked ? "ok" : "no-option";
  }
  function labelFor(el) {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText.trim(); }
    const w = el.closest("label,div");
    return w ? w.innerText.trim() : (el.value || "");
  }

  // Date sub-fields (role=spinbutton). A plain value-set does NOT register with Workday, so we
  // type the digits through the field's own key handling. Returns true only if it stuck.
  async function wdDate(idPrefix, parts) {
    let allOk = true;
    for (const [seg, val] of Object.entries(parts)) {
      if (!val) continue;
      // Two traps in these spinbuttons, both learned the hard way:
      //   • They clamp on EVERY keystroke, so a zero-padded "07" goes 0 → clamped up to 1 →
      //     "1"+"7" = 17 → clamped down to 12, and July silently became December. Strip the pad.
      //   • Writing "" to clear first makes the widget re-render and REPLACE the input, leaving
      //     our reference detached — the field then stays visibly empty. So never clear, always
      //     write the absolute prefix (not el.value + ch, which reads back a clamped value),
      //     and re-fetch the element after each keystroke in case it was swapped out.
      const v = String(val).replace(/^0+(?=\d)/, "");
      let el = wdById(`${idPrefix}-dateSection${seg}-input`);
      if (!el) { allOk = false; continue; }
      el.scrollIntoView({ block: "center" });
      el.focus();
      for (let i = 0; i < v.length; i++) {
        const ch = v[i];
        for (const type of ["keydown", "keypress", "keyup"]) {
          el.dispatchEvent(new KeyboardEvent(type, { key: ch, code: "Digit" + ch, bubbles: true }));
        }
        setNativeValue(el, v.slice(0, i + 1));
        await sleep(40);
        el = wdById(`${idPrefix}-dateSection${seg}-input`) || el;   // survive a re-render
      }
      el.blur();
      await sleep(150);
      const got = wdById(`${idPrefix}-dateSection${seg}-input`) || el;
      if (String(got.value).replace(/^0+(?=\d)/, "") !== v) allOk = false;  // verify on the LIVE element
    }
    return allOk;
  }

  // Which wizard step are we on? Read it from the progress bar's own element — NOT from
  // document.innerText, which bleeds into the page body and breaks on the last step.
  function wdStep() {
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      if (!/^\s*current step \d+ of \d+\s*$/i.test(n.nodeValue || "")) continue;
      let el = n.parentElement;
      for (let i = 0; i < 3 && el; i++, el = el.parentElement) {
        const m = /^current step \d+ of \d+\s+(.{2,40})$/i.exec((el.innerText || "").replace(/\s+/g, " ").trim());
        if (m) return m[1].trim();
      }
    }
    return "";
  }

  // Workday's State dropdown lists FULL names ("California"), but the profile stores the
  // postal abbreviation ("San Jose, CA") — map it or the option never matches.
  const US_STATES = {
    AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California", CO: "Colorado",
    CT: "Connecticut", DE: "Delaware", DC: "District of Columbia", FL: "Florida", GA: "Georgia",
    HI: "Hawaii", ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa", KS: "Kansas",
    KY: "Kentucky", LA: "Louisiana", ME: "Maine", MD: "Maryland", MA: "Massachusetts",
    MI: "Michigan", MN: "Minnesota", MS: "Mississippi", MO: "Missouri", MT: "Montana",
    NE: "Nebraska", NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico",
    NY: "New York", NC: "North Carolina", ND: "North Dakota", OH: "Ohio", OK: "Oklahoma",
    OR: "Oregon", PA: "Pennsylvania", RI: "Rhode Island", SC: "South Carolina",
    SD: "South Dakota", TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont", VA: "Virginia",
    WA: "Washington", WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming",
  };
  function stateName(profile) {
    const raw = ((profile.location || "").split(",")[1] || "").trim();
    return US_STATES[raw.toUpperCase()] || raw || "California";
  }

  // "How did you hear about us?" — NOT a plain Select One. Workday renders it as a SEARCHABLE
  // multi-select: clicking an option in the list silently does nothing (confirmed live). What
  // works is type-to-filter, then ArrowDown + Enter. And not every tenant lists LinkedIn —
  // Mastercard's choices are Employee Referral / Internet Search / Job Board / … — so walk a
  // preference chain down to whatever this form actually offers.
  const HEARD_CHAIN = ["LinkedIn", "Job Board", "Internet Search", "Online", "Other"];

  async function wdHowDidYouHear(log) {
    const box = wdBox(/how did you (hear|learn)|where did you (hear|learn)/i);
    if (!box) return 0;
    if (box.querySelector('[data-automation-id="selectedItem"]')) return 0;   // already answered
    const input = box.querySelector('input[type=text],input:not([type])');
    if (!input) return 0;
    for (const term of HEARD_CHAIN) {
      input.scrollIntoView({ block: "center" });
      input.focus();
      input.click();
      setNativeValue(input, term);                       // filter the taxonomy
      let opts = [];
      for (let i = 0; i < 16 && !opts.length; i++) {
        await sleep(90);
        opts = [...document.querySelectorAll('[data-automation-id="promptOption"],[role=option]')]
          .filter((o) => o.offsetParent && o.textContent.trim());
      }
      if (opts.length) {
        for (const key of ["ArrowDown", "Enter"]) {      // the sequence that actually commits
          for (const type of ["keydown", "keyup"]) {
            input.dispatchEvent(new KeyboardEvent(type, { key, code: key, bubbles: true }));
          }
          await sleep(140);
        }
        for (let i = 0; i < 8 && !box.querySelector('[data-automation-id="selectedItem"]'); i++) await sleep(60);
        if (box.querySelector('[data-automation-id="selectedItem"]')) {
          log("How did you hear → " + term, "good");
          return 1;
        }
      }
      setNativeValue(input, "");                         // this tenant doesn't offer it — next
      await sleep(80);
    }
    log("How did you hear — pick manually", "warn");
    return 0;
  }

  // Attach the tailored PDF. Workday hides the real <input type=file> behind a styled button,
  // but it IS a genuine file input, so the same DataTransfer injection the Greenhouse path uses
  // works here. Also covers the "Autofill with Resume" step at the very start of the wizard —
  // hand Workday the PDF and let ITS parser prefill the history for us.
  async function wdAttachResume(role, log) {
    const files = [...document.querySelectorAll('input[type=file]')];
    if (!files.length) return 0;
    if (!role.has_tailored_pdf) { log("no tailored résumé for this role yet", "warn"); return 0; }
    // don't re-attach if something is already there
    if (files.some((f) => f.files && f.files.length)) return 0;
    const res = await send({ type: "resume", jobId: role.job_id });
    if (isContextDead(res)) { log("JobScan was reloaded — refresh this page (⌘R), then click Fill again.", "warn"); return 0; }
    if (!res || !res.ok) { log("résumé fetch failed: " + (res && res.error), "warn"); return 0; }
    const target = files.find((f) => /resume|cv|curriculum/i.test(fieldText(f)) && !/cover/i.test(fieldText(f)))
      || files.find((f) => !f.accept || /pdf/i.test(f.accept))
      || files[0];
    const ok = attachResume(b64ToBytes(res.b64), res.name, target);
    log(ok ? "résumé attached ✓ (" + res.name + ")" : "couldn't attach the résumé — upload it yourself",
      ok ? "good" : "warn");
    return ok ? 1 : 0;
  }

  // ---- per-step fillers ----
  async function wdMyInformation(profile, eeo, log) {
    const street = (profile.address || "").split(",")[0].trim();
    const t = [
      [wdById("legalName--firstName"), profile.legal_first_name || profile.first_name, "first name"],
      [wdById("legalName--lastName"), profile.last_name, "last name"],
      [wdById("address--addressLine1"), street, "address"],
      [wdById("address--city"), (profile.location || "").split(",")[0].trim(), "city"],
      [wdById("address--postalCode"), (profile.address.match(/\b(\d{5})\b/) || [])[1], "postal code"],
      [wdById("phoneNumber--phoneNumber"), nationalPhone(profile), "phone"],   // (see fix below if pre-filled)
    ];
    const filled = t.filter(([el, v]) => wdSetText(el, v)).map(([, , n]) => n);
    if (filled.length) log("filled: " + filled.join(", "), "good");
    // Workday pre-fills the phone from the saved account WITH the country code — correct it.
    fixPhoneCountryCode(wdById("phoneNumber--phoneNumber"), profile, log);

    for (const [q, want, name] of [
      [/^state\b|state\*/i, new RegExp("^" + escapeRe(stateName(profile)) + "$", "i"), "State"],
      [/phone device type/i, /^mobile$/i, "Phone Device Type"],
      [/country phone code/i, /united states of america/i, "Country Phone Code"],
    ]) {
      const r = await wdPrompt(q, want);
      if (r === "ok") log("selected: " + name, "good");
      else if (r === "no-option") log(name + " — pick manually", "warn");
    }
    await wdHowDidYouHear(log);        // searchable multi-select — needs its own handling
    const w = await wdChoice(/ever worked for .*(employee|contingent)|previously.*(employ|work)/i, /^no$/i);
    if (w === "ok") log("answered: worked here before → No", "good");
  }

  async function wdExperience(role, log) {
    const jobs = role.experience || [];
    const edu = (role.profile || {}).education || [];
    const addBtn = (sectionRe) => {
      const head = [...document.querySelectorAll("h2,h3,h4,[role=heading]")].find((h) => sectionRe.test(h.innerText.trim()));
      let c = head && head.parentElement;
      for (let i = 0; i < 5 && c; i++, c = c.parentElement) {
        const b = [...c.querySelectorAll('button[data-automation-id="add-button"]')].find((x) => x.offsetParent);
        if (b) return b;
      }
      return null;
    };
    const newInstance = (before, sel) =>
      [...document.querySelectorAll(sel)].map((e) => (e.id.match(/-(\d+)--/) || [])[1]).find((n) => n && !before.has(n));

    let dateTrouble = false;
    for (const j of jobs) {
      if ([...document.querySelectorAll('[id*="companyName"]')].some((e) => e.value === j.company)) continue; // already there
      const before = new Set([...document.querySelectorAll('[id*="jobTitle"]')].map((e) => (e.id.match(/-(\d+)--/) || [])[1]));
      const b = addBtn(/^work experience$/i); if (!b) break;
      b.click(); await sleep(1200);
      const i = newInstance(before, '[id*="jobTitle"]'); if (!i) break;
      const P = `workExperience-${i}--`;
      wdSetText(document.getElementById(P + "jobTitle"), j.title);
      wdSetText(document.getElementById(P + "companyName"), j.company);
      wdSetText(document.getElementById(P + "location"), j.location);
      // Role Description — the résumé bullets, one per line (recruiters read this box, and
      // Workday's own search indexes it, so it must not be left empty).
      const desc = (j.bullets || []).map((b) => "• " + b).join("\n");
      if (desc) {
        const box = document.getElementById(P + "roleDescription");
        if (!wdSetText(box, desc) && box && !(box.value || "").trim()) {
          log("role description for " + j.company + " — paste it manually", "warn");
        }
      }
      if (j.current) { const c = document.getElementById(P + "currentlyWorkHere"); if (c && !c.checked) c.click(); }
      if (!(await wdDate(P + "startDate", { Month: j.start_month, Year: j.start_year }))) dateTrouble = true;
      if (!j.current && !(await wdDate(P + "endDate", { Month: j.end_month, Year: j.end_year }))) dateTrouble = true;
      log("added job: " + j.company, "good");
      await humanPause();
    }

    for (const e of edu) {
      if ([...document.querySelectorAll('[id*="schoolName"]')].some((x) => x.value === e.school)) continue;
      const before = new Set([...document.querySelectorAll('[id*="schoolName"]')].map((x) => (x.id.match(/-(\d+)--/) || [])[1]));
      const b = addBtn(/^education$/i); if (!b) break;
      b.click(); await sleep(1200);
      const i = newInstance(before, '[id*="schoolName"]'); if (!i) break;
      const P = `education-${i}--`;
      wdSetText(document.getElementById(P + "schoolName"), e.school);
      wdSetText(document.getElementById(P + "fieldOfStudy"), e.discipline);
      wdSetText(document.getElementById(P + "firstYearAttended-dateSectionYear-input"), e.start_year);
      wdSetText(document.getElementById(P + "lastYearAttended-dateSectionYear-input"), e.end_year);
      const degBtn = document.getElementById(P + "degree");
      if (degBtn && /select one/i.test(degBtn.getAttribute("aria-label") || "")) {
        degBtn.click();
        const want = /master/i.test(e.degree) ? /^masters?$/i : /bachelor/i.test(e.degree) ? /^bachelors?$/i : new RegExp(escapeRe(e.degree), "i");
        let dopts = [];
        for (let k = 0; k < 24 && !dopts.length; k++) {                 // poll, don't sleep blind
          await sleep(70);
          dopts = [...document.querySelectorAll('[role=option],[data-automation-id="promptOption"]')].filter((x) => x.offsetParent && x.textContent.trim());
        }
        const o = dopts.find((x) => want.test(x.textContent.trim()));
        if (o) { o.click(); await sleep(150); } else wdBlur();
      }
      log("added school: " + e.school, "good");
      await humanPause();
    }
    await wdSkills(role.skills, log);            // the Skills box lives on this same step
    await wdAttachResume(role, log);             // …and so does the Resume/CV upload
    if (dateTrouble) log("⚠ dates didn't take — open each date dropdown, pick a WRONG month then the right one", "warn");
  }

  // "Type to Add Skills" is a typeahead multi-select matched against WORKDAY'S OWN skill
  // taxonomy — a term it doesn't recognise returns no options at all. So: try each skill,
  // retry a slash-variant ("Go/Golang" → "Go"), and quietly skip whatever it doesn't know.
  async function wdSkills(skills, log) {
    const input = document.getElementById("skills--skills") || wdById("skills--skills");
    if (!input || !(skills || []).length) return;
    const already = new Set([...document.querySelectorAll('[data-automation-id="selectedItem"]')]
      .map((e) => e.textContent.replace(/\s+/g, " ").trim().toLowerCase()));
    let added = 0;
    for (const raw of skills) {
      if (added >= 10) break;                                  // enough signal; keep it human
      for (const term of [raw, ...(raw.includes("/") ? raw.split("/").map((s) => s.trim()) : [])]) {
        if (!term || already.has(term.toLowerCase())) continue;
        input.scrollIntoView({ block: "center" });
        input.focus();
        setNativeValue(input, term);
        let opts = [];
        for (let i = 0; i < 16 && !opts.length; i++) {          // taxonomy lookup is async
          await sleep(90);
          opts = [...document.querySelectorAll('[data-automation-id="promptOption"],[role=option]')]
            .filter((o) => o.offsetParent && o.textContent.trim());
        }
        // Typing alone adds NOTHING — the entry has to be committed. And clicking a Workday
        // promptOption silently fails (confirmed live), so commit with ArrowDown + Enter and
        // only count it once a selected pill actually appears.
        const pills = () => document.querySelectorAll('[data-automation-id="selectedItem"]').length;
        const n0 = pills();
        for (const key of ["ArrowDown", "Enter"]) {
          for (const type of ["keydown", "keyup"]) {
            input.dispatchEvent(new KeyboardEvent(type, { key, code: key, bubbles: true }));
          }
          await sleep(120);
        }
        for (let i = 0; i < 8 && pills() === n0; i++) await sleep(50);
        if (pills() === n0) {                                  // keyboard didn't take — try the click
          const hit = opts.find((o) => o.textContent.trim().toLowerCase() === term.toLowerCase()) || opts[0];
          if (hit) hit.click();
          for (let i = 0; i < 8 && pills() === n0; i++) await sleep(50);
        }
        if (pills() > n0) {                                    // committed for real
          already.add(term.toLowerCase());
          added++;
          break;                                               // got this skill — next one
        }
        setNativeValue(input, "");                             // didn't take — clear and move on
        await sleep(60);
      }
    }
    setNativeValue(input, "");
    log(added ? `added ${added} skill(s)` : "skills — none matched Workday's list, add manually",
      added ? "good" : "warn");
  }

  async function wdQuestions(role, log) {
    // Eligibility / conflict-of-interest questions: answer from the profile, default No.
    const rules = [
      [/legally authorized to work/i, /^yes$/i, "work authorized → Yes"],
      [/require sponsorship|sponsorship for an employment visa/i, /^no$/i, "sponsorship → No"],
      [/restrictive covenant|non-compete|non-disclosure/i, /^no$/i, "restrictive covenant → No"],
      [/ever worked for|previously.*(employ|work)/i, /^no$/i, "worked here before → No"],
      [/engage with .* employees to negotiate|related to anyone|government office or agency|close personal relationship|outside employment/i, /^no$/i, "conflict-of-interest → No"],
    ];
    for (const [q, want, name] of rules) {
      let box;
      // some pages repeat the same shape several times — answer every match
      while ((box = wdBoxes().find((b) => q.test(b.innerText) && /select one/i.test(b.innerText)))) {
        const r = await wdPrompt(new RegExp(escapeRe(box.innerText.slice(0, 40))), want);
        if (r !== "ok") { log(name + " — pick manually", "warn"); break; }
        log("answered: " + name, "good");
      }
    }
    await wdSalary(role, log);
    await fillCoverLetterBox(role, log);
  }

  // "Additional information" / "Anything else you'd like us to know" / a cover-letter box:
  // draft a short paragraph with the same voice method as the cover-letter skill. Only ever
  // fills an EMPTY box, and only a genuinely open one — never a short factual field.
  async function fillCoverLetterBox(role, log) {
    if (!role.ai_enabled) return;
    // Cover-letter drafting is the token-heavy call, so it does NOT fire automatically unless
    // you opted in (config ai.auto_cover_letter). Otherwise we just flag that a box exists —
    // the panel's "✍ cover letter" button drafts it on demand.
    if (role.auto_cover_letter === false) {
      const has = [...document.querySelectorAll("textarea")].some((el) => {
        if (!el.offsetParent || (el.value || "").trim()) return false;
        const t = (questionLabel(el) || fieldText(el) || "").toLowerCase();
        return /cover letter|additional information|anything else|tell us (more|anything)|what else should we know/.test(t);
      });
      if (has) log("cover-letter box here — click ✍ to draft it (saves tokens by not auto-running)", "warn");
      return;
    }
    const box = [...document.querySelectorAll("textarea")].find((el) => {
      if (!el.offsetParent || (el.value || "").trim()) return false;
      const t = (questionLabel(el) || fieldText(el) || "").toLowerCase();
      return /cover letter|additional information|anything else|tell us (more|anything)|why (do you want|are you interested)|what else should we know|other information/.test(t);
    });
    if (!box) return;
    log("drafting a cover-letter paragraph…");
    const r = await send({ type: "coverLetter", jobId: role.job_id, words: 180 });
    if (isContextDead(r)) { log("JobScan was reloaded — refresh this page (⌘R), then click Fill again.", "warn"); return; }
    if (!r || !r.ok || !r.text) { log("cover letter failed: " + ((r && r.error) || "no text"), "warn"); return; }
    box.focus();
    setNativeValue(box, r.text);
    box.blur();
    log("cover letter drafted ✓ — READ IT and make the wording yours before submitting", "good");
  }

  // Desired salary. OPTIONAL → deliberately left blank (no number is better than a guess).
  // REQUIRED → ask the server, which uses the JD's own published range first, then an AI
  // market estimate, then the configured fallback — and always reports which one it used.
  async function wdSalary(role, log) {
    const box = wdBoxes().find((b) => /desired salary|salary expectation|expected salary|compensation expectation/i.test(b.innerText));
    if (!box) return;
    const el = box.querySelector("textarea,input[type=text],input:not([type])");
    if (!el || (el.value || "").trim()) return;
    const required = /\*/.test(box.innerText) || el.getAttribute("aria-required") === "true" || el.required;
    if (!required) { log("Desired Salary is optional — left blank", "good"); return; }
    const r = await send({ type: "salary", jobId: role.job_id });
    if (isContextDead(r)) { log("JobScan was reloaded — refresh this page (⌘R), then click Fill again.", "warn"); return; }
    if (!r || !r.ok || !r.amount) { log("Desired Salary — couldn't determine a figure, fill it yourself", "warn"); return; }
    const shown = "$" + Number(r.amount).toLocaleString();
    if (wdSetText(el, String(r.amount))) log(`Desired Salary ${shown} — ${r.source}`, "good");
    else log(`Desired Salary: type ${shown} yourself (${r.source}) — this box rejects scripted input`, "warn");
  }

  // Signature / "type your name" boxes — the CC-305 form, e-signature panels, and the final
  // review page all ask for one. These are a legal attestation, so they get the LEGAL full
  // name (Alex Example), never the preferred one. Returns how many boxes it filled.
  function wdSignName(profile, log) {
    const legalFull = [profile.legal_first_name || profile.first_name, profile.last_name]
      .filter(Boolean).join(" ");
    if (!legalFull) return 0;
    let n = 0;
    for (const box of wdBoxes()) {
      const t = (box.innerText || "").replace(/\s+/g, " ").trim();
      if (!/^name\b|full name|legal name|your name|signature|please type your name|type your full name/i.test(t)) continue;
      if (/company|school|employer|university|contact name|reference/i.test(t)) continue;   // not a signature
      const el = box.querySelector('input[type=text],input:not([type]),textarea');
      if (!el || (el.value || "").trim() || isDropdownish(el)) continue;
      if (wdSetText(el, legalFull)) { n++; log("signature name: " + legalFull, "good"); }
      else log("signature name — type “" + legalFull + "” yourself", "warn");
    }
    return n;
  }

  async function wdSelfIdentify(profile, eeo, log) {
    let did = 0;                                   // how many fields this pass actually changed
    const decline = /prefer not|don'?t wish|do not wish|decline|not to self.?identify|do not want to answer/i;
    const want = (key, fallback) => {
      const v = (eeo || {})[key];
      if (!v || decline.test(v)) return decline;
      const yn = /^(yes|no)\b/i.exec(v.trim());
      return yn ? new RegExp("^\\s*" + yn[1], "i") : new RegExp(escapeRe(v), "i");
    };
    for (const [q, w, name] of [
      [/what is your gender/i, want("gender"), "Gender"],
      [/hispanic or latino/i, want("hispanic_latino"), "Hispanic/Latino"],
      [/served in the military|veteran/i, /^i am not a veteran$|not a protected veteran/i, "Veteran"],
    ]) {
      const r = await wdPrompt(q, w);
      if (r === "ok") { did++; log("self-ID: " + name, "good"); }
      else if (r === "no-option") log(name + " — pick manually", "warn");
    }
    // Race/ethnicity: a checkbox GROUP on Workday, and on many forms it only APPEARS once
    // "Hispanic or Latino?" is answered — untilSettled() re-runs us so we catch it then.
    const fs = document.querySelector('[data-automation-id="ethnicityMulti-CheckboxGroup"]');
    if (fs) {
      const boxes = [...fs.querySelectorAll("input[type=checkbox]")];
      const eth = (eeo || {}).race;
      const re = !eth || decline.test(eth) ? decline : new RegExp(escapeRe(eth), "i");
      const hit = boxes.find((c) => re.test(labelFor(c)));
      if (hit && !hit.checked) { hit.scrollIntoView({ block: "center" }); hit.click(); did++; log("self-ID: Ethnicity", "good"); }
    } else if (!wdBox(/ethnicity|\brace\b/i)) {
      // not on the page (yet) — a later pass will pick it up if answering Hispanic reveals it
    }
    // a plain prompt-style race question (some tenants use a dropdown, not a checkbox group)
    const raceBox = wdBox(/what is your (race|ethnicity)/i);
    if (raceBox && /select one/i.test(raceBox.innerText)) {
      const eth = (eeo || {}).race;
      const w = !eth || decline.test(eth) ? decline : new RegExp(escapeRe(eth), "i");
      if (await wdPrompt(/what is your (race|ethnicity)/i, w) === "ok") { did++; log("self-ID: Race", "good"); }
    }
    // CC-305 disability form: choice + signature name + today's date
    const dis = wdBoxes().find((b) => /check one of the boxes/i.test(b.innerText));
    if (dis) {
      const v = (eeo || {}).disability || "";
      const re = /^no\b/i.test(v) ? /^no, i do not have a disability/i : decline.test(v) ? /do not want to answer/i : /^no, i do not have a disability/i;
      const hit = [...dis.querySelectorAll("input[type=checkbox]")].find((c) => re.test(labelFor(c)));
      if (hit && !hit.checked) { hit.scrollIntoView({ block: "center" }); hit.click(); did++; log("self-ID: Disability", "good"); }
    }
    if (wdSignName(profile, log)) did++;
    const now = new Date();
    if (wdById("SignedOn-dateSectionMonth-input")) {
      await wdDate("SignedOn", {
        Month: String(now.getMonth() + 1).padStart(2, "0"),
        Day: String(now.getDate()).padStart(2, "0"),
        Year: String(now.getFullYear()),
      });
    }
    did += tickConsents(log);          // every consent/acknowledgement box on this page
    return did;
  }

  // Every unresolved validation error on the page, each tied to the field it belongs to. This
  // is what Workday shows after a failed "Save and Continue": read it, name the field, and —
  // where we can — fix it, instead of leaving the owner to hunt for what's still red.
  function wdErrorList() {
    const out = [];
    const seen = new Set();
    for (const e of document.querySelectorAll('[role=alert],[data-automation-id*="error" i],[class*="error" i]')) {
      if (!e.offsetParent) continue;
      const msg = (e.textContent || "").replace(/\s+/g, " ").replace(/^Errors? Found/i, "").trim();
      if (!msg || !/required|invalid|must|select|valid|enter/i.test(msg) || seen.has(msg)) continue;
      seen.add(msg);
      // the field this error sits in
      const box = e.closest('[data-automation-id^="formField"]');
      const field = box ? (box.innerText || "").replace(/\s+/g, " ").replace(/error[:\-].*/i, "").trim().slice(0, 40) : "";
      out.push({ msg: msg.slice(0, 80), field, box });
    }
    return out;
  }

  // After filling a step, surface anything Workday would still reject on Save. Re-runs the
  // step's own filler once (many "required" errors are just a control that didn't commit), then
  // reports what's genuinely left for the owner — the field name AND why.
  async function wdReportErrors(role, log, repaired) {
    let errs = wdErrorList();
    if (!errs.length) return;
    if (!repaired) {                                   // one repair pass — re-commit the fields
      log(`${errs.length} field(s) flagged — retrying them…`, "warn");
      await fillWorkday(role, log, true);              // silent re-fill
      wdBlur();
      await sleep(400);
      errs = wdErrorList();
      if (!errs.length) { log("all flagged fields fixed ✓", "good"); return; }
    }
    log(`still needs you (${errs.length}):`, "warn");
    for (const e of errs.slice(0, 8)) {
      log("  • " + (e.field ? e.field + " — " : "") + e.msg, "warn");
    }
    // the date widgets are the usual culprit — remind about the trick that works by hand
    if (errs.some((e) => /date|month|year/i.test(e.field + e.msg))) {
      log("  (date stuck? open its dropdown, pick a WRONG month, then the right one)", "warn");
    }
  }

  // Fills the CURRENT step only. Auto-clicking through the wizard was tried and pulled back
  // out: advancing before the fields are reliably committed just carries mistakes forward,
  // and a wrong answer three steps deep is far harder to spot than one on the page in front
  // of you. So: fill, you check, you click Save and Continue.
  async function fillWorkday(role, rawLog, silent) {
    const log = silent ? () => {} : rawLog;    // the repair pass fills quietly
    const profile = role.profile || {};
    const step = wdStep();
    log("Workday step: " + (step || "(unknown)"));
    if (/my information/i.test(step)) await wdMyInformation(profile, role.eeo, log);
    else if (/my experience/i.test(step)) await wdExperience(role, log);
    else if (/application questions/i.test(step)) await wdQuestions(role, log);
    // re-run: answering one self-ID question often REVEALS the next (Hispanic → race)
    else if (/voluntary disclosures|self identify/i.test(step)) await untilSettled(() => wdSelfIdentify(profile, role.eeo, log));
    else if (/review/i.test(step)) {
      wdSignName(profile, log);                 // the final page often wants a typed legal name
      log("Review step — check it over, then Submit yourself.", "warn");
    }
    else {
      // unknown step (incl. the "Autofill with Resume" upload at the very start) — hand
      // Workday the PDF if it's asking for one, then fill whatever standard fields are here
      await wdAttachResume(role, log);
      await wdMyInformation(profile, role.eeo, log);
      await wdQuestions(role, log);
    }
    tickConsents(log);                           // agreements can sit on any step, not just self-ID
    wdBlur();                                    // rule 1 — leave nothing open before you Save
    if (!silent) {
      await wdReportErrors(role, rawLog, false); // read Workday's errors, retry, report leftovers
      log("— review, then click Save and Continue yourself.");
    }
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
        .chip.applied-warn{ display:block; background:#fde2e1; color:#b42318; font-weight:600; margin-bottom:6px; }
        .chip.company-warn{ display:block; background:#fff3cd; color:#7a5c00; margin-bottom:6px; cursor:help; }
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
          <button class="ai cover" hidden>✍ Draft cover letter</button>
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
    // "Did I already apply?" — the panel's most useful glance. THIS role applied → strong red;
    // else other roles at the same company applied → amber heads-up.
    let appliedWarn = "";
    if (role.this_status === "applied" || role.this_status === "interviewing") {
      appliedWarn = `<div class="chip applied-warn">⚠ you already marked THIS role “${role.this_status}”</div>`;
    } else if ((role.company_applied || []).length) {
      const a = role.company_applied;
      const names = a.slice(0, 2).map((x) => x.title).join("; ") + (a.length > 2 ? ` +${a.length - 2} more` : "");
      appliedWarn = `<div class="chip company-warn" title="${a.map((x) => x.title + " · " + x.status + (x.date ? " · " + x.date : "")).join("\n")}">↩ you've applied to ${role.company} before (${a.length}): ${names}</div>`;
    }
    $(".status").innerHTML = appliedWarn + (role.has_tailored_pdf
      ? '<span class="chip ready">tailored résumé ready</span>'
      : '<span class="chip none">no tailored résumé — text only</span>');
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

      // Workday is a different animal (multi-step wizard, its own widgets) — own filler,
      // fills the CURRENT step only; you click Save and Continue / Submit yourself.
      if (IS_WORKDAY) {
        try { await fillWorkday(role, log); }      // fills THIS step only — you click Save yourself
        catch (e) {
          const m = String((e && e.message) || e);
          log(/context invalidated|Extension context/i.test(m)
            ? "JobScan was reloaded — refresh this page (⌘R), then click Fill again."
            : "Workday fill error: " + m, "warn");
        }
        fillBtn.disabled = false; fillBtn.textContent = orig;
        return;
      }

      // 1) standard text fields (name / email / phone / links / location)
      const line = document.createElement("div"); $(".log").appendChild(line);
      const { filled, skipped } = await fillFields(profile, (k) => {
        const cur = line.textContent.replace(/^fields: /, "");
        line.textContent = "fields: " + (cur ? cur + ", " : "") + k;
      });
      if (!filled.length) line.textContent = "no standard text fields matched";
      if (skipped.length) log("left as-is: " + skipped.join(", "));

      // 1.5) repeatable Employment + Education sections (Greenhouse custom forms, e.g. Lyft)
      if (document.getElementById("company-name-0") || document.getElementById("school--0")) {
        await fillGreenhouseHistory(role, log);
      }

      // 2) common eligibility / logistics (dropdowns, Yes/No radios, country, location…)
      const leftover = await fillCommon(profile, log, role);

      // 2.5) voluntary EEO / demographic self-ID → your per-field preference (config `eeo` map),
      //      each field defaulting to "I don't wish to answer". Master switch `eeo_decline`
      //      (default ON — runs unless explicitly false, so it works even against an older server).
      // untilSettled: answering Hispanic/Latino can REVEAL the race question — re-scan for it
      if (role.eeo_decline !== false) await untilSettled(() => fillDemographics(role.eeo, log));

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

      // 5) "Additional information" / cover-letter box
      await fillCoverLetterBox(role, log);

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

    // ✍ cover-letter button — opt-in so the token-heavy draft only runs when you ask.
    if (role.ai_enabled) {
      const cover = $(".cover");
      cover.hidden = false;
      cover.onclick = async () => {
        const box = [...document.querySelectorAll("textarea")].find((el) =>
          el.offsetParent && !((el.value || "").trim()) &&
          /cover letter|additional information|anything else|tell us (more|anything)|what else should we know|why (do you want|are you interested)/.test((questionLabel(el) || fieldText(el) || "").toLowerCase()));
        if (!box) { log("no empty cover-letter / 'anything else' box on this page", "warn"); return; }
        cover.disabled = true; const o = cover.textContent; cover.textContent = "✍ drafting…";
        const r = await send({ type: "coverLetter", jobId: role.job_id, words: 180 });
        cover.disabled = false; cover.textContent = o;
        if (isContextDead(r)) { log("JobScan was reloaded — refresh this page (⌘R).", "warn"); return; }
        if (!r || !r.ok || !r.text) { log("cover letter failed: " + ((r && r.error) || "no text"), "warn"); return; }
        box.focus(); setNativeValue(box, r.text); box.blur();
        log("cover letter drafted ✓ — READ IT and make the wording yours before submitting", "good");
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
    // Workday renders its own widgets (no file input on most steps) — detect its form fields.
    const hasForm = IS_WORKDAY
      ? document.querySelector('[data-automation-id^="formField"], [data-automation-id="add-button"]')
      : document.querySelector('input[type=file], #first_name, [name*="resume" i]');
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
