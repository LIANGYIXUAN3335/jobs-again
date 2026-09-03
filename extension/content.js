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
  // Stop button: while filling, clicking the button again sets this. humanPause runs between
  // almost every field, so throwing there stops the fill promptly at the next field boundary.
  let abortFill = false;
  const ABORT = "__jobscan_abort__";
  const humanPause = async () => {
    if (abortFill) throw new Error(ABORT);
    await sleep(180 + Math.floor(Math.random() * 140));         // ~0.18–0.32s
    if (abortFill) throw new Error(ABORT);
  };

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
    // ANY react-select subnode (…select__… — control, value, AND the hidden form input react-
    // select renders with no role), an Ashby/MUI select wrapper, or a listbox. The hidden input
    // is why Samsara's "AI Policy" dropdown got a typed paragraph — it slipped past the old
    // `select__control`-only check into the AI text path.
    if (el.closest('[class*="select__"],[class*="_select"],[role=combobox],[role=listbox]')) return true;
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
    // WORK AUTHORIZATION & SPONSORSHIP are ALWAYS eligibility toggles — decide them BEFORE the
    // essay/length guard below can nuke a long-worded one (the box text on Workday runs past 120
    // chars, which used to fall through to a default that answered authorization as "No"). She IS
    // authorized (EAD/AOS) and needs NO sponsorship. Authorization is checked first, so a combined
    // "authorized to work WITHOUT sponsorship?" reads correctly as Yes.
    if (/authori[sz]ed to work|authori[sz]ation to work|legally (authorized|allowed|entitled|eligible)\b.{0,20}\bwork|(right|eligible|permitted|permission) to work|work authoriz/.test(t))
      return { yesno: "yes" };
    if (/\bsponsor/.test(t)) return { yesno: "no" };
    // OPEN-ENDED essay questions must NEVER get a Yes/No or canned value — they go to the AI.
    // "Tell us about your experience working remote…" contains "remote", which used to trigger a
    // stray "Yes". A prompt that asks you to narrate is not an eligibility toggle.
    if (/\b(tell us|describe|walk (us|me)|share (your|a|some)|explain|elaborate|in your own words|give (us |me )?(an )?example)\b/.test(t)
        || /\byour experience\b/.test(t)
        || /\bwhat\b.{0,30}\b(challenge|experience|approach|practice|excite|draw|motivat|interest|learn|proud|accomplish)/.test(t)
        || /\b(what|how|why|when|where)\b.{0,60}\byou\b.{0,40}\?/.test(t)   // "what … do you …?" narrative
        || t.length > 120) return null;
    const YES = { yesno: "yes" }, NO = { yesno: "no" };
    const US = profile.country || "United States";
    // eligibility / logistics (Yes/No)
    // Harmless required opt-ins (marketing / personalization / job recommendations / alerts) —
    // no "right" answer and won't affect the application, so just satisfy the field. Decline
    // (No) is the tidy default: fewer marketing emails. Owner's rule: "just pick one."
    if (/personali[sz]e|job recommendation|recommended content|marketing (email|communication)|promotional|stay up to date|receive (updates|alerts|newsletter)|opt.?in/.test(t)) return NO;
    // Don't let them contact the CURRENT employer (job-search confidentiality) — a past/previous
    // employer is fine. "may we contact your current employer / present employer" → No.
    if (/(may we |can we |okay to )?contact.{0,30}(current|present)\s*employer|current employer.{0,20}contact/.test(t)) return NO;
    // Order matters: a question can mention BOTH "authorized" and "sponsor" ("are you authorized
    // to work WITHOUT sponsorship?" → Yes). Decide the specific "do you NEED sponsorship?" case
    // first (No), THEN "are you authorized?" (Yes), and only a bare stray "sponsor" falls to No.
    if (/(require|need|request|seek).{0,25}sponsor|sponsor.{0,20}(require|need)|will you (now|ever|in the future).{0,30}sponsor/.test(t)) return NO;
    if (/authori[sz]|legally (allowed|entitled|eligible|authorized)|eligible to work|right to work|permitted to work|work permit|legally.*work/.test(t)) return YES;
    if (/\bspons/.test(t)) return NO;                                       // any other sponsorship mention → No
    if (/(ever|previously|before|prior|former|formerly).{0,40}(employ|work)/.test(t) || /(employ|work(ed)?)\b.{0,25}(for us|by us|here before|at (this|our) (compan|organi)|our compan|current or former)/.test(t) || /current or former (employee|worker|contractor)/.test(t)) return NO;
    // Employed by / affiliated with a university, government, or public-sector entity → No (none
    // of these apply to her; this is the standard screening answer). Guarded so it never steps on
    // a work-authorization or "United States" phrasing.
    if ((/(employ\w*|work\w*|affiliat\w*|officer|official|contractor)\b.{0,45}(universit|college|government|federal|state agenc|state government|public sector|public office|regulatory)/.test(t)
         || /\b(government|public)\s+(official|employee|officer|entity|agenc)/.test(t)) && !/authori[sz]|united states/.test(t)) return NO;
    // Citizenship / residency in an export-controlled or sanctioned country → No.
    if (/(citizen|resident|national|permanent resident)\b.{0,80}(cuba|syria|iran|north korea|crimea|donetsk|luhansk|sanction)/.test(t)
        || /\bexport[- ]control|sanctioned (countr|region|nation)/.test(t)) return NO;
    // Related to / knows a current employee of the company → No.
    if (/\brelated to (a |an |any |current |anyone|someone)/.test(t)
        || /(family member|relative|spouse|friend)\b.{0,30}(work|employ)|do you (have|know)\b.{0,30}(relative|family member).{0,20}(work|employ)/.test(t)) return NO;
    if (/relocat/.test(t)) return YES;
    // Do you use / have experience with the company's OWN product or system → Yes (favorable and
    // truthful — she's an engineer; owner's rule: answer toward getting the interview).
    if (/(do you|have you|are you)\b.{0,45}(use|used|using|work(ed|ing)? (on|with)|experience (with|using)|familiar with|hands.?on|knowledge of)\b.{0,55}(workday|servicenow|our (product|platform|software|system|tool|technolog)|the (product|platform|system|software)|this (product|platform|system))/.test(t)) return YES;
    if (/\bremot/.test(t)) return YES;
    if (/on-?site|onsite|in[- ]office|in person|\bhybrid\b|commut/.test(t)) return YES;
    if (/able to travel|willing.*travel/.test(t)) return YES;
    // Generic favorable "are you willing / able to …" → Yes (aim for the interview).
    if (/are you (willing|able|comfortable|prepared|ready|open|available)\b.{0,40}(to|for|work)|can you (start|begin|commit|work|commute|attend)|willing to (start|commit|undergo|travel)/.test(t)) return YES;
    if (/\b18\b.*(older|age)|at least 18|over 18|of legal age|legally an adult/.test(t)) return YES;
    // acknowledgements / confirmations the form needs a Yes on
    if (/\b(do you |please |i )?(accept|confirm|acknowledge|agree|certif(y|ies)|consent)\b/.test(t) && !/decline/.test(t)) return YES;
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
    // race BEFORE hispanic: a "Race" question lists "Hispanic or Latino" as an OPTION, so its
    // block text contains "hispanic" — keying on that would mislabel the Race question. A
    // standalone "Are you Hispanic or Latino?" has no "race"/"ethnic" word, so it still keys right.
    if (/\brace\b|racial|ethnic/.test(t)) return "race";
    if (/hispanic|latin/.test(t)) return "hispanic_latino";
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

  // Pick the FIRST real option of a react-select (skipping the "Select…" placeholder). Used as
  // the last resort for "how did you hear about us?" when the form lists no LinkedIn — owner's
  // rule is "just pick one" for that harmless required field.
  async function pickAnyOption(control, input) {
    if (!control || !input) return { ok: false };
    if (input.getAttribute("aria-expanded") !== "true") { coordSeq(control); }
    let options = [];
    for (let i = 0; i < 10 && !options.length; i++) {
      await sleep(80);
      const menu = document.getElementById(input.getAttribute("aria-controls") || "");
      if (menu) options = [...menu.querySelectorAll("[role=option]")].filter((o) => o.textContent.trim() && !/^\s*select\b|^\s*please|^\s*choose|^\s*—\s*$/i.test(o.textContent));
    }
    if (!options.length) { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); return { ok: false }; }
    coordSeq(options[0]);
    await sleep(200);
    return { ok: true, picked: (control.innerText || "").replace(/\s+/g, " ").trim() };
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

  // EEO rendered as RADIO groups (Ashby: Gender/Race/Veteran/Disability are radio dots, not the
  // react-select dropdowns Greenhouse uses — that's why they weren't detected). One question per
  // block; match the owner's eeo preference to a radio label and click it.
  function radioText(r) {
    if (r.id) { const l = document.querySelector(`label[for="${CSS.escape(r.id)}"]`); if (l && l.innerText.trim()) return l.innerText.replace(/\s+/g, " ").trim(); }
    return (r.closest("label")?.innerText || r.getAttribute("aria-label") || r.value || "").replace(/\s+/g, " ").trim();
  }
  function fillEeoRadios(prefs, log) {
    prefs = prefs || {};
    let did = 0;
    const seenBlocks = new Set();
    for (const block of document.querySelectorAll('[class*="_fieldEntry"],fieldset,[class*="field"]')) {
      if (!block.offsetParent) continue;
      const radios = [...block.querySelectorAll('input[type=radio]')].filter((r) => r.offsetParent);
      if (radios.length < 2) continue;
      const txt = (block.innerText || "").replace(/\s+/g, " ").trim();
      if (!DEMOGRAPHIC.test(txt) || seenBlocks.has(txt.slice(0, 25))) continue;
      seenBlocks.add(txt.slice(0, 25));
      if (radios.some((r) => r.checked)) continue;                 // already answered
      const key = eeoKey(txt);
      const pref = key && prefs[key] != null ? String(prefs[key]).trim() : "";
      const wantDecline = !pref || DECLINE.test(pref);
      let want;
      if (wantDecline) want = DECLINE;
      else { const yn = /^(yes|no)\b/i.exec(pref); want = yn ? new RegExp("^\\s*" + yn[1], "i") : new RegExp(escapeRe(pref), "i"); }
      let target = radios.find((r) => want.test(radioText(r)));
      if (!target && !wantDecline) target = radios.find((r) => DECLINE.test(radioText(r)));  // value absent → decline
      if (target) { target.scrollIntoView({ block: "center" }); target.click(); did++; log("self-ID: " + short(key || txt.slice(0, 20)) + " → " + short(radioText(target)), "good"); }
    }
    return did;
  }

  async function fillDemographics(prefs, log) {
    prefs = prefs || {};
    let acted = false;
    let did = 0;                    // count for untilSettled — some forms reveal race only
    const seen = new Set();         // after Hispanic/Latino is answered
    did += fillEeoRadios(prefs, log);   // radio-based EEO (Ashby et al.) — combobox EEO below
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
      // "How did you hear about us?" — prefer LinkedIn, but many forms don't list it. Owner's
      // rule: if LinkedIn isn't there, just pick any option. Try LinkedIn → common sources →
      // the FIRST real option, so a required field never blocks on a missing LinkedIn.
      if (!done && /how (did|do) you hear|where did you (hear|learn|find)|hear about (us|this)|source of (this )?(job|opportunity|application)|referral source/.test(label.toLowerCase())) {
        await pace();
        let r = await pickReactSelect(control, input, /linkedin/i, /online|internet|company (web)?site|job board|social media|search engine|google|other/i);
        if (!r.ok) r = await pickAnyOption(control, input);   // still nothing → first real option
        if (r.ok) { log("how did you hear → " + short(r.picked), "good"); done = true; }
      }
      if (!done && ans && (ans.choice || ans.yesno)) {
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
      // A react-select's inner box is an input[type=text]; it's a DROPDOWN, not an essay. Never
      // send it to the AI text path (that's what typed a paragraph into Samsara's "follow the AI
      // policy?" dropdown instead of picking Yes). The combobox loop above already handled it.
      if (isDropdownish(el)) continue;
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

  // Which "Add another" button belongs to a section. Lyft's section header is a <div>, not an
  // h2/label, so heading-walking failed — instead use DOM position: the education fields
  // (school--*) split the form. The employment "Add another" sits BEFORE them, education's AFTER.
  function addAnotherFor(section) {
    const btns = [...document.querySelectorAll("button,a")].filter((x) => x.offsetParent && /add another/i.test(x.innerText || ""));
    if (!btns.length) return null;
    const eduAnchor = document.querySelector('[id^="school--"]');
    if (!eduAnchor) return section === "employment" ? btns[0] : btns[btns.length - 1];
    const before = btns.filter((b) => eduAnchor.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_PRECEDING);
    const after = btns.filter((b) => eduAnchor.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    return section === "employment" ? (before[before.length - 1] || btns[0]) : (after[0] || btns[btns.length - 1]);
  }
  // Lyft's "current role" checkbox id is like current-role-0_1 (index + a suffix) — match by prefix.
  const ghCurrentBox = (i) => document.querySelector(`[id^="current-role-${i}_"]`) || document.getElementById(`current-${i}`);

  async function ghSetCombo(id, value, want) {           // react-select by id → pick matching option
    const el = document.getElementById(id);
    if (!el || !value) return false;
    const control = el.closest('[class*="control"]') || el.parentElement;
    const r = await pickReactSelect(control, el, want || new RegExp("^\\s*" + escapeRe(value), "i"), null);
    return r.ok;
  }

  // Degree dropdowns list wildly different labels — "Bachelor's", "B.S.", "B Tech", "BE", etc.
  // (Nvidia offers only "B Tech", no "Bachelor"). Match on ALL common aliases for the level.
  function degreeRegex(degree) {
    const d = (degree || "").toLowerCase();
    if (/master|\bm\.?s\b|graduate/.test(d)) return /master|\bm\.?\s?s\b|\bm\.?\s?tech|\bm\.?\s?eng|\bmba\b|post.?grad|graduate/i;
    if (/bachelor|\bb\.?s\b|undergrad/.test(d)) return /bachelor|\bb\.?\s?s\b|\bb\.?\s?tech|\bb\.?\s?e\b|\bb\.?\s?eng|undergrad/i;
    if (/ph\.?d|doctor/.test(d)) return /ph\.?d|doctor/i;
    return new RegExp("^\\s*" + escapeRe(degree), "i");
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
        const add = addAnotherFor("employment");
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
        const c = ghCurrentBox(i);
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
        const add = addAnotherFor("education");
        if (!add) break;
        add.click();
        for (let t = 0; t < 20 && !document.getElementById(`school--${i}`); t++) await sleep(150);
      }
      if (!document.getElementById(`school--${i}`)) break;
      await ghSetTypeahead(`school--${i}`, e.school);
      await ghSetCombo(`degree--${i}`, e.degree, degreeRegex(e.degree));   // matches "B Tech", "B.S.", etc.
      await ghSetTypeahead(`discipline--${i}`, e.discipline);
      ghSetText(`start-date-year--${i}`, e.start_year);   // present on some tenants
      ghSetText(`end-date-year--${i}`, e.end_year);
      log("education: " + e.school, "good");
      await humanPause();
    }
  }

  // ================= Ashby (jobs.ashbyhq.com) =================
  // Ashby standard fields (Name single, email, phone, LinkedIn/GitHub/Portfolio) already fill via
  // classify(). The gap is the repeatable Education / Work-history block. Real structure, read live
  // off the Plaid form:
  //   _systemfield_education_history-degree / -major : text
  //   -startDate / -endDate : a <div> holding TWO native <select> (month value 1-12, year value)
  //   -isCurrent : checkbox ;  "+ Add Education" button to add more
  const IS_ASHBY = /(^|\.)ashbyhq\.com$/i.test(location.hostname);

  function setSelectValue(sel, value) {           // React-controlled <select>
    if (!sel || value == null || value === "") return false;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value").set;
    // match by value first, else by visible text (month names)
    const byVal = [...sel.options].some((o) => o.value === String(value));
    let v = String(value);
    if (!byVal) {
      const opt = [...sel.options].find((o) => o.text.trim().toLowerCase() === String(value).toLowerCase());
      if (opt) v = opt.value; else return false;
    }
    setter.call(sel, v);
    sel.dispatchEvent(new Event("input", { bubbles: true }));
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  async function ashbyDateInto(div, mm, yyyy) {   // the month/year <select> pair inside a date <div>
    if (!div) return false;                       // pass the ELEMENT — ids repeat, getElementById lies
    // Set month, then year, and VERIFY each landed (a React re-render can otherwise clobber the
    // value and leave a wrong month — that's the "July" the fill sometimes showed). Retry once.
    const setVerify = (want, matchVal) => {
      const sels = [...div.querySelectorAll("select")];  // re-query each time — the node can swap
      const sel = sels[matchVal ? 0 : 1];
      if (!sel) return false;
      for (let a = 0; a < 2; a++) {
        setSelectValue(sel, want);
        if (String(sel.value) === String(want)) return true;
      }
      return false;
    };
    let ok = true;
    if (mm) { ok = setVerify(String(parseInt(mm, 10)), true) && ok; await sleep(70); }   // "04"→"4"
    if (yyyy) { ok = setVerify(String(yyyy), false) && ok; await sleep(50); }
    return ok;
  }

  // Ashby "School" is a typeahead (input[role=combobox], placeholder "Search schools…"): type
  // the name, wait for its listbox to fetch, click the first matching result.
  async function ashbySchoolPick(input, name) {
    if (!input || !name) return false;
    input.scrollIntoView({ block: "center" });
    input.focus();
    setNativeValue(input, name);
    let opts = [];
    for (let i = 0; i < 16 && !opts.length; i++) {
      await sleep(100);
      const lb = document.getElementById(input.getAttribute("aria-controls") || "");
      opts = lb ? [...lb.querySelectorAll('[role=option],li')].filter((o) => o.offsetParent && o.textContent.trim()) : [];
    }
    if (!opts.length) { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); return false; }
    const nn = name.toLowerCase().replace(/[^a-z0-9]/g, "");
    const hit = opts.find((o) => o.textContent.toLowerCase().replace(/[^a-z0-9]/g, "").startsWith(nn)) || opts[0];
    hit.click();
    await sleep(200);
    return true;
  }

  // Ashby renders some Yes/No questions as a PAIR OF BUTTONS (not radios/dropdown): two
  // <button> with text "Yes"/"No". Answer from the profile (sponsorship→No, previously
  // employed→No, authorized→Yes), clicking the matching button in that question's block.
  async function fillAshbyButtons(profile, log) {
    // Group Yes/No buttons by their QUESTION container. NOTE the buttons' own class contains
    // "_container", so closest('[class*=_container]') would return the button itself — walk up to
    // the "_fieldEntry" block (which holds the question label + both buttons), or the smallest
    // ancestor that has the question text AND exactly one Yes + one No.
    const questionBlock = (b) => {
      let n = b.parentElement;
      for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
        if (/_fieldEntry/.test(n.className || "")) return n;
        const yn = [...n.querySelectorAll("button")].filter((x) => /^(yes|no)$/i.test((x.textContent || "").trim()));
        if (yn.length === 2 && (n.innerText || "").replace(/\s+/g, " ").trim().length > 15) return n;
      }
      return null;
    };
    const groups = new Map();
    for (const b of document.querySelectorAll("button")) {
      if (!b.offsetParent || !/^(yes|no)$/i.test((b.textContent || "").trim())) continue;
      const block = questionBlock(b);
      if (!block) continue;
      if (!groups.has(block)) groups.set(block, []);
      groups.get(block).push(b);
    }
    for (const [block, btns] of groups) {
      if (btns.length < 2) continue;
      if (btns.some((b) => /_selected|_active|true/i.test((b.getAttribute("aria-pressed") || "") + " " + b.className))) continue; // answered
      const q = (block.innerText || "").replace(/\s+/g, " ").replace(/\b(yes|no)\b/gi, "").trim();
      if (SENSITIVE.test(q)) continue;
      const ans = commonAnswer(q, profile);
      if (!ans || !ans.yesno) continue;
      const want = ans.yesno === "yes" ? /^yes$/i : /^no$/i;
      const target = btns.find((b) => want.test((b.textContent || "").trim()));
      if (target) { target.click(); await sleep(150); log("answered: " + short(q) + " → " + ans.yesno, "good"); }
    }
  }

  // All Nth-entry fields for one suffix, in DOM order. Ashby gives every education entry the
  // SAME id (…education_history-degree), so container-scoping picked the wrong one and every
  // entry wrote onto the first — indexing parallel arrays by entry number is the reliable way.
  const ashbyEduField = (suffix) => [...document.querySelectorAll(`[id$="education_history-${suffix}"]`)];

  async function fillAshbyHistory(role, log) {
    const edu = (role.profile || {}).education || [];
    if (!edu.length) return;
    const addBtn = () => [...document.querySelectorAll("button")].find((b) => b.offsetParent && /add education/i.test(b.innerText || ""));
    const setText = (el, v) => { if (el && !(el.value || "").trim() && v) { el.focus(); setNativeValue(el, v); el.blur(); } };

    for (let i = 0; i < edu.length; i++) {
      if (i >= ashbyEduField("degree").length) {          // need another entry block
        const add = addBtn();
        if (!add) { if (i) log(`only room for ${i} education entr${i > 1 ? "ies" : "y"} — add the rest by hand`, "warn"); break; }
        add.click();
        for (let t = 0; t < 20 && ashbyEduField("degree").length <= i; t++) await sleep(150);
      }
      const e = edu[i];
      // School = a "Search schools…" typeahead (one per entry, in DOM order)
      const schools = [...document.querySelectorAll('input[role=combobox]')].filter((x) => x.offsetParent && /search schools/i.test(x.placeholder || ""));
      if (schools[i] && !(schools[i].value || "").trim()) await ashbySchoolPick(schools[i], e.school);
      setText(ashbyEduField("degree")[i], e.degree);
      setText(ashbyEduField("major")[i], e.discipline);
      await ashbyDateInto(ashbyEduField("startDate")[i], e.start_month, e.start_year);
      await ashbyDateInto(ashbyEduField("endDate")[i], e.end_month, e.end_year);
      log("education: " + (e.school || e.degree), "good");
      await humanPause();
    }
  }

  // ================= Google Careers (google.com/about/careers) =================
  // Google's own Material form. No job id in the URL → we fill PROFILE fields only (name/email/
  // phone/eligibility/consent), no résumé, no role questions. Fields use jsname, not id:
  //   text inputs jsname=YPqjbf (labelled First name / Last name / Email / Phone)
  //   Material dropdowns div[role=combobox][jsname=oYxtQd] (Suffix, Country calling code)
  //   radiogroups with aria-label ("Have you worked at Alphabet before?")
  const IS_GOOGLE = /(^|\.)google\.com$/i.test(location.hostname) && /\/about\/careers\//i.test(location.pathname);
  const IS_MICROSOFT = /(^|\.)careers\.microsoft\.com$/i.test(location.hostname);

  function gLabel(el) {
    const lb = el.getAttribute("aria-labelledby");
    if (lb) { const t = lb.split(" ").map((id) => (document.getElementById(id) || {}).textContent || "").join(" ").replace(/\s+/g, " ").trim(); if (t) return t; }
    let n = el;
    for (let i = 0; i < 5 && n; i++, n = n.parentElement) {
      const c = [...n.children].map((x) => x.textContent).find((t) => t && t.trim().length > 2 && t.trim().length < 60);
      if (c) return c.replace(/\s+/g, " ").trim();
    }
    return el.getAttribute("aria-label") || "";
  }

  async function gMaterialPick(combo, wantRe) {          // open a Material combobox, click a role=option
    if (combo.getAttribute("aria-expanded") === "true") { combo.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); await sleep(120); }
    coordSeq(combo);
    let opts = [];
    for (let i = 0; i < 12 && !opts.length; i++) { await sleep(80); opts = [...document.querySelectorAll('[role=option]')].filter((o) => o.offsetParent); }
    const hit = opts.find((o) => wantRe.test(o.textContent.trim()));
    if (!hit) { combo.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); return false; }
    coordSeq(hit);
    await sleep(200);
    return true;
  }

  async function fillGoogle(profile, log) {
    const legalFirst = profile.legal_first_name || profile.first_name;
    // text fields by label
    let filled = [];
    for (const el of document.querySelectorAll('input[jsname="YPqjbf"]')) {
      if (!el.offsetParent || (el.value || "").trim()) continue;
      const l = gLabel(el).toLowerCase();
      let v = "";
      if (/first name/.test(l)) v = legalFirst;
      else if (/last name|surname|family/.test(l)) v = profile.last_name;
      else if (/email/.test(l)) v = profile.email;
      // Phone: Google's number box wants PLAIN DIGITS only — the "+1" lives in the separate
      // Country-calling-code dropdown (box 1). A formatted "202-555-0142" is rejected as
      // "Invalid phone number", so strip to bare digits: "4082100678".
      else if (/phone/.test(l)) v = nationalPhone(profile).replace(/\D/g, "");
      else continue;
      if (!v) continue;
      el.focus(); setNativeValue(el, v); el.blur();
      filled.push(l.slice(0, 14));
    }
    if (filled.length) log("filled: " + filled.join(", "), "good");

    // Country calling code Material dropdown → +1
    for (const combo of document.querySelectorAll('[jsname="oYxtQd"][role=combobox]')) {
      if (!combo.offsetParent) continue;
      if (/country|calling code|dial/i.test(gLabel(combo))) {
        const ok = await gMaterialPick(combo, /united states|\+1\b/i);
        log(ok ? "country code → +1" : "country code — pick manually", ok ? "good" : "warn");
      }
    }
    // All Yes/No radio groups → answer from the profile (worked-before → No, personalize/
    // marketing opt-ins → No, work-auth → Yes, etc.). commonAnswer decides; skip demographics.
    for (const g of document.querySelectorAll('[role=radiogroup]')) {
      if (!g.offsetParent) continue;
      const q = g.getAttribute("aria-label") || gLabel(g);
      if (SENSITIVE.test(q)) continue;
      const ans = commonAnswer(q, profile);
      if (!ans || !ans.yesno) continue;
      const want = ans.yesno === "yes" ? /^\s*y(es)?\b/i : /^\s*no?\b/i;
      const opt = [...g.querySelectorAll('[role=radio],input[type=radio]')].find((r) => want.test(r.getAttribute("aria-label") || r.closest("label")?.innerText || r.value || ""));
      if (opt) { opt.click(); log("answered: " + short(q) + " → " + ans.yesno, "good"); }
    }
    // consent checkboxes ("I understand…", "I hereby certify…")
    tickConsents(log);
  }

  // ================= SmartRecruiters (jobs.smartrecruiters.com) =================
  // Its "Easy Apply" form is built from `spl-` web components, so the inputs live inside SHADOW
  // DOM — a plain document.querySelector finds NOTHING (that's why nothing filled). We pierce
  // shadow roots to reach them. Field ids are clean: first-name-input, email-input, linkedin-input…
  const IS_SR = /(^|\.)smartrecruiters\.com$/i.test(location.hostname);

  function deepAll(sel, root = document, out = [], depth = 0) {
    if (depth > 14) return out;
    for (const el of root.querySelectorAll("*")) {
      if (el.matches && el.matches(sel)) out.push(el);
      if (el.shadowRoot) deepAll(sel, el.shadowRoot, out, depth + 1);
    }
    return out;
  }
  const deepById = (id) => deepAll("#" + CSS.escape(id)).find((e) => e.offsetParent) || null;
  const srVis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  // The id-named fields (first-name-input, …) are <spl-input> custom elements that WRAP a real
  // <input> in their shadow root. Setting `.value` on the custom element only updates the display —
  // the component's own validation model doesn't change, so on "Next" the form still reports the
  // field empty. The fix is to drive the INNER native input with the real value setter + a real
  // InputEvent, which is what the component listens to. srInner returns that inner native element.
  const srInner = (id) => {
    const all = deepAll("#" + CSS.escape(id));
    return all.find((e) => (e instanceof HTMLInputElement || e instanceof HTMLTextAreaElement) && srVis(e))
        || all.find((e) => e instanceof HTMLInputElement || e instanceof HTMLTextAreaElement) || null;
  };
  function srNativeSet(el, v) {
    if (!el || !v) return false;
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    el.focus();
    desc.set.call(el, v);
    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: v }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.blur();
    return true;
  }
  // Open an <spl-select>/autocomplete combobox and click the option matching wantRe. These options
  // live in a portal and IGNORE dispatched pointer/keyboard events AND coordinate clicks — only the
  // element's own .click() METHOD selects them. Typing a char opens the list (it does not strictly
  // filter), so any seed works; we try a few so a decline option surfaces whatever its wording.
  async function srPickSelect(input, wantRe, seeds) {
    if (!input) return false;
    const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    for (const seed of seeds) {
      input.focus();
      desc.set.call(input, seed);
      input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: seed }));
      let opt = null;
      for (let i = 0; i < 12 && !opt; i++) {
        await sleep(90);
        opt = deepAll("spl-select-option, [role=option]").find(
          (o) => srVis(o) && wantRe.test((o.textContent || "").replace(/\s+/g, " ").trim()));
      }
      if (opt) { opt.click(); await sleep(120); return true; }
      // clear before trying the next seed
      desc.set.call(input, "");
      input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward" }));
    }
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    return false;
  }

  async function fillSmartRecruiters(role, log) {
    // Page 2 (Preliminary/screening questions) is a whole different form under /screening.
    if (/\/screening/i.test(location.pathname) || deepAll("sr-question-field-radio, sr-question-field-select")[0]) {
      return fillSmartRecruitersScreening(role, log);
    }
    const profile = role.profile || {};
    const filled = [];
    const t = [
      ["first-name-input", profile.legal_first_name || profile.first_name, "first name"],
      ["last-name-input", profile.last_name, "last name"],
      ["email-input", profile.email, "email"],
      ["confirm-email-input", profile.email, "confirm email"],
      ["linkedin-input", profile.linkedin, "LinkedIn"],
      ["website-input", profile.website, "website"],
    ];
    for (const [id, v, name] of t) {
      const el = srInner(id);
      if (el && v && !(el.value || "").trim() && srNativeSet(el, v)) filled.push(name);
    }
    // phone: national number, no +1 (there's a separate country selector)
    const phone = deepAll('input[type=tel]').find(srVis);
    if (phone && !(phone.value || "").trim() && srNativeSet(phone, nationalPhone(profile))) filled.push("phone");
    if (filled.length) log("filled: " + filled.join(", "), "good");

    // City is an <spl-select> autocomplete that FETCHES suggestions — type the city, wait for the
    // list, click the first suggestion (the option's own .click() is the only thing that selects it).
    const city = deepAll('input[role=combobox]').find(
      (e) => srVis(e) && /city|form-element_10/i.test((e.getAttribute("aria-label") || e.id || "")));
    if (city && !(city.value || "").trim()) {
      const cityName = (profile.location || "").split(",")[0].trim();
      const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
      city.focus();
      desc.set.call(city, cityName);
      city.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: cityName }));
      let opt = null;
      for (let i = 0; i < 16 && !opt; i++) {
        await sleep(140);
        opt = deepAll("spl-select-option, [role=option]").find((o) => srVis(o) && new RegExp(cityName, "i").test(o.textContent || ""));
      }
      if (opt) { opt.click(); log("city → " + opt.textContent.replace(/\s+/g, " ").trim().slice(0, 30), "good"); }
      else { city.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); log("city — pick it manually", "warn"); }
    }

    // résumé — the file-input is real (in shadow), so DataTransfer injection works
    const file = deepAll('input[type=file]').find((e) => e.accept ? /pdf|doc/i.test(e.accept) : true);
    if (file && role.has_tailored_pdf && !(file.files && file.files.length)) {
      const res = await send({ type: "resume", jobId: role.job_id });
      if (isContextDead(res)) { log("JobScan was reloaded — refresh this page (⌘R), then click Fill again.", "warn"); return; }
      if (res && res.ok) {
        const ok = attachResume(b64ToBytes(res.b64), res.name, file);
        log(ok ? "résumé attached ✓" : "couldn't attach résumé — upload it yourself", ok ? "good" : "warn");
      }
    }

    // "Let the company know…" message box → cover letter
    const msg = deepAll("textarea").find((e) => srVis(e) && !(e.value || "").trim());
    if (msg && role.ai_enabled) {
      const r = await send({ type: "coverLetter", jobId: role.job_id, words: 160 });
      if (r && r.ok && r.text) { srNativeSet(msg, r.text); log("cover note drafted ✓ — read it before submitting", "good"); }
    }
    log("Experience/Education: use the + Add buttons yourself (their fields are locked in shadow DOM).", "warn");
    log("When you reach the Preliminary-questions page, click Fill again to answer those.", "warn");
  }

  // SmartRecruiters page 2 — "Preliminary questions" (/screening). Each question is an
  // <sr-question-field-radio> (spl-radio options, selected via .click()) or
  // <sr-question-field-select> (an spl-select combobox, driven by srPickSelect). Demographic
  // questions (gender/race/disability/veteran) follow the owner's eeo config: decline unless a
  // concrete answer was chosen. Signature/date attestation fields are filled from the profile.
  async function fillSmartRecruitersScreening(role, log) {
    const eeo = role.eeo || {};
    const declineRe = /prefer not|don'?t wish|do(es)? not (want|wish)|decline|no answer/i;
    const wantsDecline = (v) => role.eeo_decline !== false && (!v || declineRe.test(v));
    let n = 0;

    // 1) radio questions
    for (const w of deepAll("*").filter((e) => e.tagName === "SR-QUESTION-FIELD-RADIO")) {
      const grp = deepAll("spl-radio-group", w)[0];
      const q = ((grp && (grp.getAttribute("label") || grp.textContent)) || "").replace(/\s+/g, " ").trim();
      const radios = deepAll("spl-radio", w);
      if (!radios.length || radios.some((r) => r.getAttribute("aria-checked") === "true")) continue;
      const t = q.toLowerCase();
      let wantRe = null;
      if (/legally authorized to work/.test(t)) wantRe = /^yes/i;
      else if (/visa sponsorship|require .*sponsor/.test(t)) wantRe = /^no/i;
      else if (/customer of /.test(t)) wantRe = /^no/i;
      else if (/federal government|state, local, or municipal|immediate family|suspend|debar/.test(t)) wantRe = /^no/i;
      else if (/worked (at|for)|ever worked/.test(t)) wantRe = /^no/i;
      else if (/relocat/.test(t)) wantRe = /^yes/i;
      else if (/disabilit/.test(t)) wantRe = wantsDecline(eeo.disability) ? declineRe : /^no\b/i;
      else if (/protected veteran/.test(t)) wantRe = wantsDecline(eeo.veteran) ? declineRe : /^no\b/i;
      if (!wantRe) continue;                                   // unknown question → leave for the owner
      const pick = radios.find((r) => wantRe.test((r.getAttribute("label") || "").trim()));
      if (pick) { pick.click(); n++; log("answered: " + q.slice(0, 42), "good"); }
    }

    // 2) select-combobox questions
    for (const cb of deepAll('input[role=combobox]').filter(srVis)) {
      if ((cb.value || "").trim() && !/^select/i.test(cb.value)) continue;   // already answered
      const label = srComboLabel(cb).toLowerCase();
      const ph = (cb.placeholder || "").toLowerCase();
      let want = null, seeds = null;
      if (/_gender/.test(cb.id) || /gender/.test(ph) || /gender/.test(label)) { if (wantsDecline(eeo.gender)) { want = declineRe; seeds = ["P", "D", "I"]; } }
      else if (/_ethnicity/.test(cb.id) || /race|ethnic/.test(ph) || /race|ethnic/.test(label)) { if (wantsDecline(eeo.race)) { want = declineRe; seeds = ["P", "D", "I"]; } }
      else if (/relocat/.test(label)) { want = /^yes/i; seeds = ["Y"]; }
      else if (/worked (at|for)|ever worked/.test(label)) { want = /^no/i; seeds = ["N"]; }
      else if (/employed.*pwc|\bpwc\b/.test(label)) { want = /^no\b/i; seeds = ["N"]; }
      else if (/citizen or lawful permanent resident|cuba, syria|export control/.test(label)) { want = /^no/i; seeds = ["N"]; }
      else if (/riseup|nextgen/.test(label)) { want = /^no/i; seeds = ["N"]; }
      else if (/authorized to work/.test(label)) { want = /^yes/i; seeds = ["Y"]; }
      else if (/sponsor/.test(label)) { want = /^no/i; seeds = ["N"]; }
      if (!want) continue;
      if (await srPickSelect(cb, want, seeds)) { n++; log("selected: " + (label || ph || "question").slice(0, 40) + " → " + (cb.value || "").slice(0, 26), "good"); }
    }

    // 3) attestation text fields (electronic signature + today's date)
    for (const w of deepAll("*").filter((e) => e.tagName === "SR-QUESTION-FIELD-TEXT")) {
      const q = (w.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
      const inp = deepAll("input, textarea", w).find((x) => x.getAttribute("role") !== "combobox");
      if (!inp || (inp.value || "").trim()) continue;
      if (/signature/.test(q)) {
        const nm = ((role.profile || {}).preferred_name || (role.profile || {}).first_name || "") + " " + ((role.profile || {}).last_name || "");
        if (srNativeSet(inp, nm.trim())) { n++; log("signed: " + nm.trim(), "good"); }
      } else if (/today'?s date|^date\b|\bdate\b/.test(q)) {
        const d = new Date();
        const mm = String(d.getMonth() + 1).padStart(2, "0"), dd = String(d.getDate()).padStart(2, "0");
        if (srNativeSet(inp, mm + "/" + dd + "/" + d.getFullYear())) { n++; log("date filled", "good"); }
      }
    }
    if (!n) log("no preliminary questions matched — answer them yourself", "warn");
    else log("Preliminary questions answered — review each, then submit yourself.", "good");
  }

  // Best-effort label for an spl-select combobox: climb its shadow-host chain to the nearest
  // question-looking text (SmartRecruiters puts the label a couple of wrappers up).
  function srComboLabel(inp) {
    let n = inp.getRootNode().host || inp;
    for (let i = 0; i < 7 && n; i++) {
      const txt = (n.textContent || "").replace(/\s+/g, " ").trim();
      if (txt.length > 12 && txt.length < 220 &&
          /\?|worked|relocat|employ|citizen|gender|ethnic|race|pwc|sponsor|riseup|nextgen|authorized/i.test(txt)) return txt;
      n = n.parentElement || (n.getRootNode && n.getRootNode().host);
    }
    return inp.placeholder || "";
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
  // Move focus fully OFF the date widget by clicking a neutral, non-interactive spot — the owner
  // noticed Workday keeps showing "Invalid Date" while a month/year segment still holds focus, and
  // that clicking elsewhere clears it. A bare el.blur() doesn't reproduce it; a real click on the
  // step heading does. Fire the commit events too, so the field's own validation runs.
  async function wdCommitDate(el) {
    try {
      if (el) {
        el.dispatchEvent(new Event("change", { bubbles: true }));
        el.dispatchEvent(new FocusEvent("blur", { bubbles: true }));
        el.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
        if (el.blur) el.blur();
      }
    } catch (e) {}
    const neutral = document.querySelector('[data-automation-id="pageHeaderTitleText"], h1, h2, h3') || document.body;
    for (const t of ["pointerdown", "mousedown", "mouseup", "click"]) neutral.dispatchEvent(new MouseEvent(t, { bubbles: true }));
    await sleep(250);
  }

  async function wdDate(idPrefix, parts) {
    let allOk = true, lastEl = null;
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
      lastEl = el;                                    // don't blur yet — focusing the next segment does that
    }
    // Only NOW, after every segment is typed, "click elsewhere" once to commit + clear the error.
    await wdCommitDate(lastEl);
    for (const [seg, val] of Object.entries(parts)) {          // verify on the LIVE elements
      if (!val) continue;
      const v = String(val).replace(/^0+(?=\d)/, "");
      const got = wdById(`${idPrefix}-dateSection${seg}-input`);
      if (!got || String(got.value).replace(/^0+(?=\d)/, "") !== v) allOk = false;
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
    // ANY single answer is fine — the source doesn't matter (owner's rule). So the instant this
    // field already has a chip, we're done. Detect it broadly (not just the standard
    // selectedItem automation-id) so a re-fill pass never stacks a second, third, … pick on.
    const hasPick = () => !!box.querySelector('[data-automation-id="selectedItem"],[data-automation-id^="DELETE"],button[aria-label*="Delete" i],[aria-label*="Remove" i]');
    if (hasPick()) return 0;
    const input = box.querySelector('input[type=text],input:not([type])');
    if (!input) return 0;
    const stopIf = () => { if (abortFill) throw new Error(ABORT); };
    for (const term of HEARD_CHAIN) {
      stopIf();
      if (hasPick()) return 0;                            // something already took — never add more
      input.scrollIntoView({ block: "center" });
      input.focus();
      input.click();
      setNativeValue(input, term);                       // filter the taxonomy
      let opts = [];
      for (let i = 0; i < 16 && !opts.length; i++) {
        await sleep(90); stopIf();
        opts = [...document.querySelectorAll('[data-automation-id="promptOption"],[role=option]')]
          .filter((o) => o.offsetParent && o.textContent.trim());
      }
      if (!opts.length) { setNativeValue(input, ""); await sleep(80); continue; }  // tenant lacks it → next
      // This term IS offered → commit ONE and STOP. Never fall through to the rest of the chain
      // (that was the "selected N times" bug). Try the two commit paths, then we're done regardless.
      for (const key of ["ArrowDown", "Enter"]) {
        for (const type of ["keydown", "keyup"]) input.dispatchEvent(new KeyboardEvent(type, { key, code: key, bubbles: true }));
        await sleep(120); stopIf();
      }
      if (!hasPick()) { coordSeq(opts[0]); for (let i = 0; i < 8 && !hasPick(); i++) { await sleep(50); stopIf(); } }
      setNativeValue(input, "");
      if (hasPick()) log("How did you hear → " + term, "good");
      else log("How did you hear — options showed but wouldn't commit; pick one manually", "warn");
      return hasPick() ? 1 : 0;                          // one attempt at one option — done either way
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
        const want = degreeRegex(e.degree);          // matches Masters/MS/M Tech, Bachelors/BS/B Tech…
        let dopts = [];
        for (let k = 0; k < 24 && !dopts.length; k++) {                 // poll, don't sleep blind
          await sleep(70);
          dopts = [...document.querySelectorAll('[role=option],[data-automation-id="promptOption"]')].filter((x) => x.offsetParent && x.textContent.trim());
        }
        // loose fallback so a tenant wording it "Master of Science" still matches "Master's"
        const loose = /master/i.test(e.degree) ? /master/i : /bachelor/i.test(e.degree) ? /bachelor/i : null;
        const o = dopts.find((x) => want.test(x.textContent.trim())) || (loose && dopts.find((x) => loose.test(x.textContent.trim())));
        // a plain "Select One" button prompt commits on .click(); if that didn't take, coordSeq.
        const committed = () => !/select one/i.test(degBtn.getAttribute("aria-label") || "");
        if (o) {
          o.click(); await sleep(150);
          if (!committed()) { coordSeq(o); await sleep(150); }
          log(committed() ? "degree → " + o.textContent.trim().slice(0, 24) : "degree — pick it manually",
            committed() ? "good" : "warn");
        } else { log("degree — no matching option, pick it manually", "warn"); }
        // ALWAYS close the menu, whether or not we picked — a left-open degree prompt leaks its
        // options into the very next field (Skills), which then clicks a degree option and the
        // page jumps back to Degree. This was the "keeps bouncing to the degree dropdown" bug.
        wdBlur();
        await sleep(150);
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
    // The id isn't always "skills--skills" across tenants — fall back to any *skill* input, then
    // to the "Type to Add Skills" / "Skills" prompt box, so NVIDIA-style tenants aren't missed.
    const input = document.getElementById("skills--skills")
      || wdById("skills--skills")
      || [...document.querySelectorAll("input")].find((e) => e.offsetParent && /skill/i.test(e.id || ""))
      || (() => { const b = wdBox(/type to add skills|add skills|^\s*skills\b/i); return b && b.querySelector('input[type=text],input:not([type])'); })();
    if (!(skills || []).length) return;
    if (!input) { log("skills box not found on this step — add them manually", "warn"); return; }
    wdBlur();                                       // close any prompt still open from a prior field
    await sleep(150);                               // so its stale options can't hijack our clicks
    const pills = () => document.querySelectorAll('[data-automation-id="selectedItem"]').length;
    // abort-aware wait: Stop must interrupt this multi-second loop, not wait it out. `sleep` alone
    // can't throw, so every wait is followed by an abort check (and we check at each loop boundary).
    const stopIf = () => { if (abortFill) throw new Error(ABORT); };
    const nap = async (ms) => { await sleep(ms); stopIf(); };
    const already = new Set([...document.querySelectorAll('[data-automation-id="selectedItem"]')]
      .map((e) => e.textContent.replace(/\s+/g, " ").trim().toLowerCase()));
    let added = 0;
    for (const raw of skills) {
      stopIf();
      if (added >= 10) break;                                  // enough signal; keep it human
      for (const term of [raw, ...(raw.includes("/") ? raw.split("/").map((s) => s.trim()) : [])]) {
        stopIf();
        if (!term || already.has(term.toLowerCase())) continue;
        input.scrollIntoView({ block: "center" });
        input.focus();
        setNativeValue(input, term);
        let opts = [];
        for (let i = 0; i < 16 && !opts.length; i++) {          // taxonomy lookup is async
          await nap(90);
          opts = [...document.querySelectorAll('[data-automation-id="promptOption"],[role=option]')]
            .filter((o) => o.offsetParent && o.textContent.trim());
        }
        if (!opts.length) { setNativeValue(input, ""); await nap(50); continue; }   // Workday doesn't know it
        // Typing alone adds NOTHING — the entry must be COMMITTED, and tenants differ: some commit
        // on Enter, some only when you click the option. So try each way and keep whichever makes a
        // pill appear. The click uses coordSeq (a real pointer sequence) — a bare .click() on a
        // Workday promptOption is silently ignored.
        const best = opts.find((o) => o.textContent.trim().toLowerCase() === term.toLowerCase()) || opts[0];
        const commits = [
          async () => { for (const key of ["ArrowDown", "Enter"]) { for (const ty of ["keydown", "keyup"]) input.dispatchEvent(new KeyboardEvent(ty, { key, code: key, bubbles: true })); await nap(90); } },
          async () => { coordSeq(best); },                      // click the first / matching option
          async () => { for (const ty of ["keydown", "keyup"]) input.dispatchEvent(new KeyboardEvent(ty, { key: "Enter", code: "Enter", bubbles: true })); },
        ];
        const n0 = pills();
        for (const commit of commits) {
          stopIf();
          await commit();
          for (let i = 0; i < 8 && pills() === n0; i++) await nap(50);
          if (pills() > n0) break;                              // committed for real
        }
        if (pills() > n0) { already.add(term.toLowerCase()); added++; break; }   // got it — next skill
        setNativeValue(input, "");                             // didn't take — clear and move on
        await nap(60);
      }
    }
    setNativeValue(input, "");
    log(added ? `added ${added} skill(s)` : "skills — none matched Workday's list, add manually",
      added ? "good" : "warn");
  }

  // Open-ended TEXT questions on a Workday step ("Describe your interaction with the Workday
  // system", "Why this role?") — a <textarea> or free text box that isn't a known profile field
  // and isn't a Select-One prompt. Route them to the AI so REQUIRED ones never sit blank. The AI
  // is told to reason about what the asked-about tool/process is and answer from her real SWE
  // background, short and spoken, without inventing specifics.
  async function wdOpenQuestions(role, log) {
    if (!role.ai_enabled) return;
    const open = [];
    for (const box of wdBoxes()) {
      const el = box.querySelector("textarea, input[type=text], input:not([type])");
      if (!el || (el.value || "").trim() || isDropdownish(el)) continue;
      const label = clean(box.innerText).replace(/\s*\*\s*$/, "").trim();
      if (!label || label.length < 8) continue;
      if (SENSITIVE.test(label)) continue;                       // demographics/comp → never AI here
      // skip standard/known fields the deterministic layer already owns
      if (/first name|last name|middle|preferred name|^name\b|full name|e-?mail|phone|address|city|state|province|zip|postal|country|linkedin|github|website|\burl\b|salary|compensation|pay|desired|date|school|univers|degree|field of study|company name|job title|employer|supervisor|reference/i.test(label)) continue;
      // a genuine open question: a textarea, or a prompt with '?' / an open verb
      if (el.tagName !== "TEXTAREA" && !/\?|describe|explain|tell us|why|how|what|share|elaborate|interaction|experience (with|using)|walk (us|me)/i.test(label)) continue;
      open.push({ el, label });
    }
    if (!open.length) return;
    log(`drafting ${open.length} open answer(s) with AI…`);
    const res = await send({ type: "answer", jobId: role.job_id, questions: open.map((q) => q.label) });
    if (isContextDead(res)) { log("JobScan was reloaded — refresh this page (⌘R), then click Fill again.", "warn"); return; }
    if (!res || !res.ok) { log("AI answer failed: " + ((res && res.error) || "no response"), "warn"); return; }
    const answers = res.answers || {};
    let n = 0;
    for (const { el, label } of open) {
      const a = answers[label];
      if (a && !(el.value || "").trim()) { await humanPause(); el.focus(); setNativeValue(el, a); el.blur(); n++; }
    }
    log(n ? `AI drafted ${n} answer(s) ✓ — read & edit each before submitting` : "AI left the open questions (declined/empty)", n ? "good" : "warn");
  }

  async function wdQuestions(role, log) {
    // Owner's rule: EVERY required question must get an answer, aimed at getting the interview.
    // Two passes: (1) specific eligibility/conflict phrasings, matched on the LEADING Yes/No (not
    // an exact "Yes"/"No" — NVIDIA words options as full sentences like "Yes, I am authorized to
    // work for any employer"); (2) a sweep over EVERY remaining "Select One" so nothing is blank.
    const YES = /^\s*yes\b/i, NO = /^\s*no\b/i;
    const profile = role.profile || {};
    const rules = [
      // authorization MUST come before sponsorship: a combined "authorized to work WITHOUT
      // sponsorship?" then correctly resolves to Yes (she is authorized), and once answered that
      // box is no longer "select one" so the sponsorship rule can't re-touch it.
      [/authori[sz]ed to work|authori[sz]ation to work|legally (authorized|allowed|entitled|eligible)\b.{0,20}\bwork|(right|eligible|permitted|permission) to work|work authoriz/i, YES, "work authorized → Yes"],
      [/\bsponsor/i, NO, "sponsorship → No"],
      [/restrictive covenant|non-compete|non-disclosure/i, NO, "restrictive covenant → No"],
      [/ever worked for|previously.*(employ|work)|worked (here|for us) before/i, NO, "worked here before → No"],
      [/would you (consider |be willing to )?relocat|open to relocat|willing to relocat/i, YES, "relocate → Yes"],
      [/do you.*(use|work (on|with)|experience (with|using)|familiar with).*(workday|servicenow|our (product|system|platform))/i, YES, "use our product → Yes"],
      [/related to (a|an|any|current|anyone|someone)|family member.*(work|employ)|relative.*(work|employ)/i, NO, "related to employee → No"],
      [/citizen or (lawful )?(permanent )?resident of (cuba|syria|iran|north korea|crimea)|export control|sanctioned (countr|region)/i, NO, "export-control country → No"],
      [/(employ|work|affiliat).{0,40}(university|college|government|federal|public sector)/i, NO, "gov/university affiliation → No"],
      [/engage with .* employees to negotiate|government office or agency|close personal relationship|outside employment|conflict of interest/i, NO, "conflict-of-interest → No"],
    ];
    for (const [q, want, name] of rules) {
      let box, guard = 0;
      // some pages repeat the same shape several times — answer every match
      while ((box = wdBoxes().find((b) => q.test(b.innerText) && /select one/i.test(b.innerText))) && guard++ < 12) {
        const r = await wdPrompt(new RegExp(escapeRe(box.innerText.slice(0, 40))), want);
        if (r !== "ok") { log(name + " — pick manually", "warn"); break; }
        log("answered: " + name, "good");
      }
    }

    // Pass 2 — the catch-all so NO required Select-One is left blank. commonAnswer handles most;
    // an unrecognized REQUIRED one still gets a favorable default (acknowledgement/eligibility →
    // Yes, otherwise the safe No) and is flagged for you to double-check. Demographics are skipped
    // (wdSelfIdentify owns those); an unrecognized OPTIONAL question is left alone.
    for (const box of wdBoxes().filter((b) => /select one/i.test(b.innerText))) {
      const q = clean(box.innerText).replace(/\bselect one\b\s*\*?/i, "").trim();
      if (SENSITIVE.test(q)) continue;
      const ca = commonAnswer(q, profile);
      let want = null, note = "";
      if (ca && ca.yesno) want = ca.yesno === "yes" ? YES : NO;
      else if (ca && (ca.choice || ca.text)) want = new RegExp("^\\s*" + escapeRe(ca.choice || ca.text), "i");
      if (!want) {
        // belt-and-suspenders for the two that MUST be right: authorization → Yes, sponsorship →
        // No, even if commonAnswer somehow missed (auth checked first for the combined phrasing).
        const ql = q.toLowerCase();
        if (/authori[sz]ed to work|(right|eligible|permitted) to work|legally\b.{0,15}\bwork|work authoriz/.test(ql)) want = YES;
        else if (/\bsponsor/.test(ql)) want = NO;
        else {
          const required = /\*/.test(box.innerText) || /\brequired\b/i.test(box.innerText);
          if (!required) continue;
          want = /\b(willing|able|agree|acknowledg|consent|accept|confirm|certif|available|eligible|comply|comfortable|understand)\b/i.test(q) ? YES : NO;
          note = " (defaulted — please verify)";
        }
      }
      const r = await wdPrompt(new RegExp(escapeRe(box.innerText.slice(0, 40))), want);
      if (r === "ok") log("answered: " + short(q) + note, note ? "warn" : "good");
    }

    await wdOpenQuestions(role, log);        // open-ended text questions → AI
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

  // PROACTIVE required-field scan — Workday doesn't flag a required field as an error until you
  // try to Save, so after a fill everything can LOOK done while required fields are still empty.
  // This walks every required formField (a "*" marker or data-automation-id="required") and
  // reports the ones with no value: an unanswered "Select One", an empty text/textarea, or a
  // radio/checkbox group with nothing ticked. Demographics are skipped (owner's own to decide).
  function wdEmptyRequired() {
    const out = [];
    for (const box of wdBoxes()) {
      const txt = (box.innerText || "").replace(/\s+/g, " ").trim();
      const required = !!box.querySelector('[data-automation-id="required"], abbr[title*="required" i]') || /\*/.test(txt);
      if (!required || SENSITIVE.test(txt)) continue;
      if (/desired salary|salary expectation|expected salary|compensation expectation/i.test(txt)) continue;  // handled/optional-by-design
      let empty = false;
      if (/select one/i.test(txt)) empty = true;                          // dropdown not chosen
      else {
        const el = box.querySelector("input, textarea, select");
        if (el && (el.type === "radio" || el.type === "checkbox")) {
          const grp = [...box.querySelectorAll('input[type=radio],input[type=checkbox]')];
          empty = grp.length > 0 && !grp.some((x) => x.checked);
        } else if (el && !isDropdownish(el)) {
          empty = !(el.value || "").trim();
        }
      }
      if (empty) out.push({ box, field: txt.replace(/\s*\*.*/, "").slice(0, 44) });
    }
    return out;
  }

  // After filling a step, surface anything Workday would still reject on Save — both errors it
  // already shows AND required fields still empty (which it hasn't flagged yet). Re-runs the
  // step's own filler once (many are just a control that didn't commit), then reports what's
  // genuinely left — the field name AND why. Confirms out loud when the step is fully filled.
  async function wdReportErrors(role, log, repaired) {
    let errs = wdErrorList();
    let empties = wdEmptyRequired();
    if (!errs.length && !empties.length) { log("all required fields on this step are filled ✓", "good"); return; }
    if (!repaired) {                                   // one repair pass — re-commit / re-fill
      log(`${errs.length + empties.length} required field(s) still need a value — retrying…`, "warn");
      await fillWorkday(role, log, true);              // silent re-fill (re-runs AI for open Qs too)
      wdBlur();
      await sleep(400);
      errs = wdErrorList();
      empties = wdEmptyRequired();
      if (!errs.length && !empties.length) { log("all required fields filled ✓", "good"); return; }
    }
    const lines = [...new Set([
      ...errs.map((e) => (e.field ? e.field + " — " : "") + e.msg),
      ...empties.map((e) => e.field + " — still empty, fill it yourself"),
    ])];
    log(`still needs you (${lines.length}):`, "warn");
    for (const line of lines.slice(0, 10)) log("  • " + line, "warn");
    // the date widgets are the usual culprit — remind about the trick that works by hand
    if (lines.some((l) => /date|month|year/i.test(l))) {
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
        .chip.pin{ display:block; background:#e8eefb; color:#1e3a8a; margin-bottom:6px; cursor:help; }
        button{ display:flex; align-items:center; justify-content:center; gap:7px; font:inherit; font-weight:600;
          cursor:pointer; border:1px solid #e2e8f0; border-radius:9px; background:#f8fafc; color:#0f172a;
          padding:9px 12px; margin-top:9px; width:100%; transition:filter .12s, background .12s; }
        button:hover{ filter:brightness(.97); }
        button:active{ transform:translateY(.5px); }
        button:disabled{ opacity:.55; cursor:default; }
        button.primary{ background:#12314f; color:#fff; border-color:#12314f; }
        button.fill.stop{ background:#b42318; border-color:#b42318; }   /* filling → click to STOP */
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
    // "Did I already apply?" — the panel's most useful glance. THIS role applied → strong red;
    // else other roles at the same company applied → amber heads-up.
    //
    // The wording has to track HOW SURE we are which role this is. Matched from the URL, the
    // role IS this page and "THIS role" is literally true. Guessed (top_role_default) or
    // pinned, it is a different claim — and saying "THIS role" about a guess is exactly the
    // contradiction the owner hit: JobScan showed an un-applied role while the panel insisted
    // the role was already applied, because they were talking about two different rows.
    const guessed = role.top_role_default || role.from_pin;
    let appliedWarn = "";
    if (role.this_status === "applied" || role.this_status === "interviewing") {
      appliedWarn = guessed
        ? `<div class="chip company-warn" title="This role was not matched from the page URL, so this may not be the posting you are on.">⚠ the role shown here (“${role.title}”) is marked “${role.this_status}” — but it was not matched from this URL, so it may not be this posting</div>`
        : `<div class="chip applied-warn">⚠ you already marked THIS role “${role.this_status}”</div>`;
    } else if ((role.company_applied || []).length) {
      const a = role.company_applied;
      const names = a.slice(0, 2).map((x) => x.title).join("; ") + (a.length > 2 ? ` +${a.length - 2} more` : "");
      appliedWarn = `<div class="chip company-warn" title="${a.map((x) => x.title + " · " + x.status + (x.date ? " · " + x.date : "")).join("\n")}">↩ you've applied to ${role.company} before (${a.length}): ${names}</div>`;
    }
    // This page's URL did NOT identify the role — it came from the ↗ you clicked in JobScan.
    // Say so loudly: everything below (résumé, salary, AI answers) is for THAT role, and if you
    // wandered to a different posting in between, this is the moment to notice.
    const pinNote = role.pin_override
      // The owner clicked a rejected-pin button and personally confirmed this page is that
      // job's form — say so, and keep saying the URL never proved it.
      ? `<div class="chip pin" title="You manually confirmed this page is this role's application form. The résumé and answers below belong to it. Close the panel if you picked wrong.">📌 manually chosen by you — URL did not confirm this role</div>`
      : role.no_role_match
      // Worst case for silence: we know the company but NOT which posting, so nothing below is
      // role-specific. Say it in the loudest chip we have, because the failure this guards
      // against (filling a form believing the panel matched it) looks identical to success.
      ? `<div class="chip applied-warn" title="Neither the URL nor any ↗ pin identified this posting, so JobScan is only filling profile-level fields (name, email, links). No role-specific résumé, salary or AI answer is available here.">🚫 JobScan does NOT know which job this is — profile fields only</div>`
      : role.from_pin
      ? `<div class="chip pin" title="This page's URL doesn't match any role in JobScan (normal for LinkedIn roles, which hand off to the employer's own ATS). Using a role you clicked ↗ on whose company also appears on this page. Close the panel if that's not this job.">📌 from the ↗ you clicked in JobScan — not matched from this URL</div>`
      // A company-level guess is weaker than a pin: nobody chose it, the server just took the
      // company's best un-applied role with a résumé. Everything below (résumé file, salary,
      // AI answers) belongs to THAT role, so it has to be as visible as the pin note.
      : role.top_role_default
      ? `<div class="chip pin" title="This page's URL carries no job id (common on Google/Microsoft careers), so JobScan guessed the company's best open role that has a tailored résumé. If this is not the posting you are on, close the panel — the résumé below belongs to the role named above.">🎲 guessed from company — URL had no job id</div>`
      : "";
    // Pins that were live but belong to a different employer. Naming them turns a confusing
    // non-event ("why is there no résumé?") into a readable one ("those are for other tabs").
    const rejectedNames = (role.pins_rejected || []).map((p) => `${p.company} — ${p.title}`);
    const rejectedNote = rejectedNames.length
      ? `<div class="chip company-warn" title="${rejectedNames.join("\n")}">📌 ${rejectedNames.length} other pinned role(s) ignored — they belong to a different company than this page</div>`
      : "";
    // "no tailored résumé" means the upload below is the BASE résumé, not one written for this
    // JD. Spell that out — "text only" read as a formatting note, not as "wrong file".
    const resumeChip = role.has_tailored_pdf
      ? '<span class="chip ready">tailored résumé ready</span>'
      : `<span class="chip none" title="No tailored PDF exists for this role, so nothing role-specific will be attached. Tailor it in JobScan first if this application matters.">⚠ no tailored résumé for this role — base/profile fields only</span>`;
    $(".status").innerHTML = pinNote + rejectedNote + appliedWarn + resumeChip;
    // One-click override for each rejected pin, when this panel has no résumé of its own.
    // The guard's DEFAULT stands (never silently attach across a company mismatch); the
    // override is an explicit human statement — a click on a button that names the role —
    // which is exactly the confidence level the old manual download-and-upload expressed,
    // minus the manual downloading and uploading.
    if (!role.has_tailored_pdf && (role.pins_rejected || []).length) {
      for (const pr of role.pins_rejected.slice(0, 3)) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "chip pin";
        b.style.cssText = "display:block;width:100%;text-align:left;cursor:pointer;" +
          "margin-bottom:6px;border:1px dashed #1e3a8a;";
        b.textContent = `📌 use “${pr.company} — ${pr.title}” here` +
          (pr.has_tailored_pdf ? " (attaches its résumé)" : "");
        b.title = "JobScan did not find this company's name on this page, so it refused to " +
          "auto-attach the role's résumé. Click ONLY if you are sure this page is that " +
          "job's application form.";
        b.onclick = () => {
          claimPin(pr);
          const cur = document.getElementById("jobscan-panel");
          if (cur) cur.remove();                    // panel() builds a fresh host each call
          const chosen = { ...pr, pin_override: true };
          panel(chosen);
          sparkleStart(chosen);                     // ✨ chips need the real job_id too
        };
        $(".status").appendChild(b);
      }
    }
    const log = (m, cls) => { const d = document.createElement("div"); if (cls) d.className = cls; d.textContent = m; $(".log").appendChild(d); };

    $(".x").onclick = () => { abortFill = true; host.remove(); };   // closing the panel also stops a fill

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
    const FILL_LABEL = "Fill application";
    fillBtn.onclick = async () => {
      // Second click while filling = STOP. Sets the abort flag; humanPause throws it and the
      // whole fill unwinds to the finally below, leaving whatever's filled so far in place.
      if (fillBtn.dataset.filling === "1") {
        abortFill = true;
        fillBtn.textContent = "stopping…";
        return;
      }
      abortFill = false;
      fillBtn.dataset.filling = "1";
      fillBtn.classList.add("stop");
      fillBtn.textContent = "■ Stop";
      $(".log").textContent = "";
      const profile = role.profile || {};
      try {
      // Workday is a different animal (multi-step wizard, its own widgets) — own filler,
      // fills the CURRENT step only; you click Save and Continue / Submit yourself.
      if (IS_GOOGLE) {
        await fillGoogle(profile, log);
        log("— profile fields filled; role-specific questions + résumé are yours here.");
        return;
      }
      if (IS_SR) {
        await fillSmartRecruiters(role, log);
        log("— review everything, then submit yourself.");
        return;
      }
      if (IS_WORKDAY) {
        await fillWorkday(role, log);              // fills THIS step only — you click Save yourself
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

      // 1.5) repeatable Employment + Education sections
      if (document.getElementById("company-name-0") || document.getElementById("school--0")) {
        await fillGreenhouseHistory(role, log);           // Greenhouse custom forms (e.g. Lyft)
      }
      if (IS_ASHBY && document.querySelector('[id$="education_history-degree"]')) {
        await fillAshbyHistory(role, log);                // Ashby education block
      }
      if (IS_ASHBY) await fillAshbyButtons(profile, log); // Ashby Yes/No button-pair questions

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
          if (!ok && /jobs\.apple\.com/.test(location.hostname)) {
            // Apple keeps a PROFILE-level resume. While one is attached, the upload input
            // (#attachfile-resume-fileupload) is not rendered at all — and the attached file
            // is whatever the LAST application uploaded, i.e. the wrong résumé for this role.
            // Removing her stored file is her call, not ours (we never overwrite user data):
            // point at the exact fix instead of failing mutely.
            const removeBtn = [...document.querySelectorAll("button")]
              .find((b) => /^remove\b/i.test((b.textContent || "").trim()));
            log(removeBtn
              ? "Apple has a previous résumé attached (probably from your last application). Click its Remove link, then hit Fill again — the right PDF attaches automatically."
              : "couldn't find a résumé upload field", "warn");
          } else {
            log(ok ? "résumé PDF attached ✓" : "couldn't find a résumé upload field", ok ? "good" : "warn");
          }
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

      log("— review everything, then submit yourself.");
      } catch (e) {
        const m = String((e && e.message) || e);
        if (m.includes(ABORT)) log("⏹ stopped — the rest is yours.", "warn");
        else if (/context invalidated|Extension context/i.test(m)) log("JobScan was reloaded — refresh this page (⌘R), then click Fill again.", "warn");
        else log("fill error: " + m, "warn");
      } finally {
        fillBtn.dataset.filling = "";
        fillBtn.classList.remove("stop");
        fillBtn.textContent = FILL_LABEL;
      }
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
      else if (isContextDead(res)) log("JobScan was reloaded — refresh this page (⌘R), then click ✓ again.", "warn");
      else log("mark-applied failed: " + (res && res.error), "warn");
    };
    return host;
  }

  // ---- boot: the form on Ashby/Greenhouse loads LATE (SPA), so we can't just check
  //      once at load. Poll briefly + watch the DOM + watch for SPA URL changes, and
  //      show the panel as soon as an application form appears for a matching role. ----
  let shownFor = null;   // URL the panel is currently shown for
  let noMatchFor = null; // URL the last role-resolution FAILED for — lets the capture pill
                         // appear on scanner-ATS hosts too, exactly and only when this page's
                         // posting is unknown to JobScan (unknown company on greenhouse/lever
                         // was a dead zone: no panel AND no pill — the Precisely trap)
  let busy = false;      // avoid overlapping lookups while the DOM churns

  // ---- the server now keeps one pin PER ROLE (apply_pins), so a newer ↗ no longer evicts an
  //      older tab's role. This claim step still earns its keep: the first frame to resolve a
  //      role off a pin copies it into sessionStorage (inherently per-tab, survives reload +
  //      SPA nav) and releases just that row, so later re-resolves in this tab are free and
  //      the live pin set stays small and honest about what is still un-applied.
  // v2: entries carry claimedAt. The v1 entry had NO expiry, so a claimed pin lived as long as
  // its tab — and Chrome session-restore preserves sessionStorage, so a Temporal role claimed
  // on 2026-07-31 was STILL labelling the same Precisely tab on 08-01. v1 entries are purged on
  // sight and never read; v2 entries die after the same 45 minutes the server gives a pin.
  const PIN_KEY = "jobscan:claimed-pin:v2";
  const CLAIM_TTL_MS = 45 * 60 * 1000;
  function claimedPin() {
    try {
      sessionStorage.removeItem("jobscan:claimed-pin");   // purge immortal pre-v2 entries
      const raw = sessionStorage.getItem(PIN_KEY);
      if (!raw) return null;
      const saved = JSON.parse(raw);
      // Same tab navigated to a different employer: a pin claimed for company A must not
      // follow you to company B. A real URL match always outranks this anyway.
      if (!saved || saved.origin !== location.origin) return null;
      if (!saved.claimedAt || Date.now() - saved.claimedAt > CLAIM_TTL_MS) {
        sessionStorage.removeItem(PIN_KEY);               // no application session lasts 45 min
        return null;
      }
      return saved.role;
    } catch (e) { return null; }        // sandboxed frames throw on sessionStorage access
  }
  function claimPin(role) {
    try {
      sessionStorage.setItem(PIN_KEY,
        JSON.stringify({ origin: location.origin, role, claimedAt: Date.now() }));
    } catch (e) { /* no storage in this frame — worst case we ask the server again */ }
    // Release only THIS role's pin — the other tabs' pins are other roles you still intend to
    // apply to. Fire-and-forget; the TTL is still the backstop.
    send({ type: "unpin", jobId: role && role.job_id });
  }

  // ---- ✨ per-field AI drafts for OPTIONAL open questions (2026-08-01) --------------------
  // Required open questions are auto-drafted by the big Fill pass (owner's rule: every
  // required question gets an answer). OPTIONAL ones get a small ✨ chip at the field's
  // bottom-right instead — click → ONE tailored draft lands in that field, yellow-outlined
  // until the owner edits it. Chips live in a fixed-position overlay: nothing is inserted
  // into the form's own DOM, because Workday's React reconciler strips foreign nodes.
  // Keep in sync with the inline known-fields regex in wdOpenQuestions.
  const KNOWN_PROFILE_RE = /first name|last name|middle|preferred name|^name\b|full name|e-?mail|phone|address|city|state|province|zip|postal|country|linkedin|github|website|\burl\b|salary|compensation|pay|desired|date|school|univers|degree|field of study|company name|job title|employer|supervisor|reference/i;
  let sparkleRole = null;
  let sparkleHost = null;
  let sparkleTimer = null;
  const sparkleChips = new Map();   // field element -> chip element

  function sparkleLabel(el) {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && l.innerText.trim()) return clean(l.innerText);
    }
    const aria = el.getAttribute("aria-label");
    if (aria && aria.trim()) return clean(aria);
    let n = el.closest("div,fieldset,li,td") || el.parentElement;
    for (let hop = 0; n && hop < 4; hop++, n = n.parentElement) {
      const t = clean(n.innerText || "").split("\n")[0];
      if (t && t.length >= 8 && t.length <= 300) return t;
    }
    return "";
  }

  function sparkleEligible(el) {
    if (!(el.tagName === "TEXTAREA"
          || (el.tagName === "INPUT" && (el.type === "text" || !el.getAttribute("type"))))) return false;
    if (el.disabled || el.readOnly) return false;
    // Combobox/typeahead text inputs (Workday selects, "How did you hear about us?") LOOK like
    // free-text but must get an OPTION, not prose — drafting into one corrupts the field.
    if (isDropdownish(el)) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 120 || r.height < 24) return false;          // hidden or too small to be a question
    if (el.dataset.jobscanAiDraft === "1") return true;        // our un-edited draft → keep the ↻ chip
    if ((el.value || "").trim()) return false;                 // already answered → stay out of the way
    // No required-vs-optional gate (2026-08-01, Adyen live diagnosis): real forms mark MOST
    // open questions required, so an optional-only chip almost never appeared — the owner
    // read that as "broken", and she was right. An EMPTY required box just means Fill hasn't
    // run yet; the chip never fights Fill because Fill skips non-empty fields.
    const label = sparkleLabel(el);
    if (!label || label.length < 8) return false;
    if (SENSITIVE.test(label)) return false;                   // demographics/comp — never AI
    // Profile-field words only disqualify SHORT labels ("City", "Desired salary"). On prose
    // they false-positive brutally: Adyen's accessibility question was blocked as a "profile
    // field" because "candidate" contains "date" — same substring pit as the C++ keyword bug.
    // A 40+ char label is a question, not a field name.
    if (label.length < 40 && KNOWN_PROFILE_RE.test(label)) return false;
    // free-form inputs must look like a question; any sizeable textarea qualifies
    if (el.tagName !== "TEXTAREA"
        && !/\?|describe|explain|tell us|why|how|what|share|elaborate|experience (with|using)|anything else|additional|comments|note/i.test(label)) return false;
    return true;
  }

  function sparklePlace(el, chip) {
    const r = el.getBoundingClientRect();
    if (!r.width || r.bottom < 0 || r.top > innerHeight) { chip.style.display = "none"; return; }
    chip.style.display = "block";
    // BELOW the field, right-aligned (Simplify-style, and what the owner asked for) — sitting
    // ON the corner covered the textarea's native resize grip. Fall back to inside-the-corner
    // only when the field's bottom edge is off-screen.
    chip.style.left = Math.round(r.right - chip.offsetWidth) + "px";
    chip.style.top = Math.round(
      r.bottom + 28 <= innerHeight ? r.bottom + 4 : r.bottom - chip.offsetHeight - 6) + "px";
  }

  function sparkleReposition() {
    for (const [el, chip] of sparkleChips) sparklePlace(el, chip);
  }

  function sparkleChipFor(el) {
    // SPA navigations can silently detach the host — chips appended to a detached node render
    // nowhere and the feature looks "dead" with no error. Recreate whenever it's not in the DOM.
    if (!sparkleHost || !document.documentElement.contains(sparkleHost)) {
      sparkleHost = document.createElement("div");
      sparkleHost.id = "jobscan-sparkles";
      sparkleHost.style.cssText = "position:fixed;left:0;top:0;z-index:2147483646;";
      document.documentElement.appendChild(sparkleHost);
    }
    const c = document.createElement("button");
    const drafted = el.dataset.jobscanAiDraft === "1";
    c.type = "button";
    c.textContent = drafted ? "↻" : "✨";
    c.title = drafted
      ? "AI draft in the field — READ & EDIT before submitting. Click to re-draft."
      : "optional question — draft a tailored answer with AI (JobScan)";
    c.style.cssText = "position:fixed;display:none;width:24px;height:24px;padding:0;" +
      "border:1px solid #d0d5dd;border-radius:999px;background:#fff;color:#111;cursor:pointer;" +
      "font-size:13px;line-height:22px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.18);";
    c.onclick = (ev) => { ev.preventDefault(); ev.stopPropagation(); sparkleDraft(el, c); };
    sparkleHost.appendChild(c);
    return c;
  }

  async function sparkleDraft(el, chip) {
    const label = sparkleLabel(el);
    if (!label) return;
    // Greenhouse's paste-toggle cover-letter box is labelled just "Enter manually" — useless
    // as a question. Send a real one so the model knows what it is writing.
    const question = /enter manually|cover letter/i.test(label)
      ? `Cover letter: why are you a great fit for the ${sparkleRole.title} role at ${sparkleRole.company}?`
      : label;
    chip.textContent = "…"; chip.disabled = true;
    try {
      const res = await send({ type: "answer", jobId: sparkleRole.job_id, questions: [question] });
      if (isContextDead(res)) {
        // Reloading the unpacked extension orphans the content script already injected in
        // this tab — same handling as every other call site: the ONE fix is refreshing.
        chip.textContent = "⚠";
        chip.title = "JobScan was reloaded — refresh this page (⌘R), then click ✨ again.";
        return;
      }
      const a = res && res.ok !== false && res.answers && res.answers[question];
      if (!a) {
        chip.textContent = "⚠";
        chip.title = "AI declined or failed — " + ((res && res.error) || "no answer (may be a never-answer topic)");
        return;
      }
      el.focus(); setNativeValue(el, a); el.blur();
      // Yellow outline until SHE edits: an unreviewed AI draft must not look like a filled field.
      el.dataset.jobscanAiDraft = "1";
      el.style.outline = "2px solid #eab308"; el.style.outlineOffset = "1px";
      el.addEventListener("input", function once() {
        el.style.outline = ""; el.style.outlineOffset = "";
        delete el.dataset.jobscanAiDraft;              // edited = yours now; chip leaves next tick
        el.removeEventListener("input", once);
      });
      chip.textContent = "↻";
      chip.title = "AI draft in the field — READ & EDIT before submitting. Click to re-draft.";
    } catch (e) {
      chip.textContent = "⚠"; chip.title = "AI draft failed: " + e;
    } finally {
      chip.disabled = false;
      if (chip.textContent === "…") chip.textContent = "✨";
    }
  }

  let sparkleLastCount = -1;   // console-log only on change — "silent nothing" must be readable

  function sparkleTick() {
    if (!sparkleRole || !document.getElementById("jobscan-panel")) { sparkleStop(); return; }
    const seen = new Set();
    for (const el of document.querySelectorAll("textarea, input[type=text], input:not([type])")) {
      if (!sparkleEligible(el)) continue;
      seen.add(el);
      if (!sparkleChips.has(el)) sparkleChips.set(el, sparkleChipFor(el));
    }
    for (const [el, chip] of [...sparkleChips]) {
      if (!seen.has(el) || !document.contains(el)) { chip.remove(); sparkleChips.delete(el); continue; }
      sparklePlace(el, chip);
    }
    if (sparkleChips.size !== sparkleLastCount) {
      sparkleLastCount = sparkleChips.size;
      console.info("[JobScan] ✨", sparkleChips.size,
                   "optional open question(s) have a draft button on this page",
                   "(Fill still auto-drafts required ones on its own; combobox/profile/sensitive fields never get a chip)");
    }
  }

  function sparkleStart(role) {
    if (!role || !role.ai_enabled || role.field_button === false
        || !role.job_id || role.no_role_match) {
      // Say WHY in the console — an invisible feature that silently declines to start is
      // indistinguishable from a broken one (exactly the report that led to this line).
      console.info("[JobScan] ✨ draft buttons off for this page:",
                   !role ? "no role" : !role.ai_enabled ? "ai disabled (no API key?)"
                   : role.field_button === false ? "ai.field_button=false in config"
                   : role.no_role_match ? "page not matched to a role — profile fill only"
                   : "role has no job_id");
      return;
    }
    sparkleRole = role;
    if (!sparkleTimer) {
      sparkleTimer = setInterval(sparkleTick, 1500);
      addEventListener("scroll", sparkleReposition, { passive: true, capture: true });
      addEventListener("resize", sparkleReposition, { passive: true });
    }
    sparkleTick();
  }

  function sparkleStop() {
    if (sparkleTimer) { clearInterval(sparkleTimer); sparkleTimer = null; }
    removeEventListener("scroll", sparkleReposition, { capture: true });
    removeEventListener("resize", sparkleReposition);
    for (const [, chip] of sparkleChips) chip.remove();
    sparkleChips.clear();
    sparkleRole = null;
  }

  // ---- Backoff for pages that resolve to nothing. ----
  // A miss is usually permanent: the id on this page isn't in the DB and no live pin's company
  // appears on it. But the poll re-arms on every tab focus and every SPA nav, so an orphaned
  // tab re-ran the FULL resolution — /api/lookup plus /api/pins — at 500 ms forever. One tab on
  // 2026-08-09 (jobs.ashbyhq.com/Stealth FinTech/…, a LinkedIn role whose employer is listed as
  // "Confidential", so no pin can ever fit the page) issued 35 lookup+pins pairs across four
  // hours: 70 requests for an answer that could not change on its own, and 70 chances for the
  // panel to flicker while she was working in the tab.
  //
  // Back off rather than give up. Two things genuinely DO change under us: an SPA can still be
  // rendering (that is why we poll at all), and a pin can arrive after the tab is open — she
  // opens a tab, then clicks ↗ in JobScan. So keep the first few retries instant and let the
  // interval widen to a minute. The map is per-tab and per-URL, and any success clears it.
  const MISS_BACKOFF_MS = [0, 0, 0, 1000, 2500, 6000, 20000, 60000];
  const misses = new Map();          // url -> { n, next }
  const missWait = (n) => MISS_BACKOFF_MS[Math.min(n, MISS_BACKOFF_MS.length - 1)];

  async function tryShow() {
    if (busy) return;
    const miss = misses.get(location.href);
    if (miss && Date.now() < miss.next) return;   // still inside this URL's backoff window
    // Workday renders its own widgets (no file input on most steps) — detect its form fields.
    const hasForm = IS_WORKDAY
      ? document.querySelector('[data-automation-id^="formField"], [data-automation-id="add-button"]')
      : (IS_GOOGLE
        ? document.querySelector('input[jsname], [role=radiogroup], [jsname="oYxtQd"]')
        : (IS_SR
          ? (deepAll("input#first-name-input, input#email-input, sr-question-field-radio, sr-question-field-select")[0]
             || (/\/screening/i.test(location.pathname) ? deepAll("input[role=combobox]")[0] : null))  // shadow DOM; page 2 = /screening
          : (document.querySelector('input[type=file], #first_name, [name*="resume" i],'
              + ' #attachfile-resume-fileupload, [id^="attachfile-"]')
             // Generic SPA forms (jobs.gem.com was the miss, 2026-08-01): no visible file
             // input, no known ids — but an email field PLUS a textarea or several text
             // inputs is the signature of a real application form, and never of a lone
             // newsletter box (those are a single email input with nothing else).
             || (document.querySelector('input[type=email]')
                 && (document.querySelector('textarea')
                     || document.querySelectorAll('input[type=text]').length >= 3)
                 ? document.querySelector('input[type=email]') : null))));
    if (!hasForm) return;                                    // not on the application form yet
    if (shownFor === location.href && document.getElementById("jobscan-panel")) return;
    busy = true;
    try {
      const res = await send({ type: "lookup", url: location.href });
      if (!res) { console.warn("[JobScan] no response — is JobScan running on http://127.0.0.1:8787 ?"); return; }
      let role = res.found ? res.role : null;
      // Careers sites whose URLs carry no matchable job id. Owner's rule: default to the
      // company's TOP-scored role (she applies high-score-first, so the first is the best
      // guess) and use ITS tailored résumé; profile-only as the last resort. LinkedIn is
      // deliberately absent — no automation there during an active search.
      const HOST_CO = { tesla: "Tesla", "jobs.apple": "Apple", atlassian: "Atlassian",
                        "wise.jobs": "Wise", circle: "Circle", hashicorp: "HashiCorp",
                        retool: "Retool", deel: "Deel" };
      const hostCompany = () => {
        if (IS_GOOGLE) return "Google";
        if (IS_MICROSOFT) return "Microsoft";
        if (IS_SR) return ((location.pathname.match(/\/company\/([^/]+)/) || [])[1] || "").replace(/[-_]/g, " ");
        const h = location.hostname + location.pathname;
        for (const k in HOST_CO) if (h.includes(k)) return HOST_CO[k];
        return "";
      };
      const company = hostCompany();
      // The pin comes BEFORE the host/company guesses: it is a statement of intent ("I clicked
      // ↗ on this exact role a minute ago"), which beats "the top-scored role at whatever
      // company this host looks like". It is also the ONLY thing that rescues a LinkedIn role —
      // Apply on LinkedIn hands off to the employer's ATS, whose URL carries no id we store, so
      // no amount of URL parsing can reach li:<id>. The server expires the pin, so a stale one
      // can't quietly attach yesterday's résumé.
      // …but a pin is a statement about ONE role, not a licence to label any page. It is
      // company-blind, so within its 45-minute life it would attach itself — and its tailored
      // résumé — to whatever un-matched application form you happened to open next. That
      // really happened: a pin on a Temporal role labelled a Precisely posting
      // (precisely.com carries no id we store and isn't in HOST_CO, so both other paths were
      // silent and the pin won by default). Filling a Precisely form with Temporal's résumé
      // is the worst outcome this panel can produce, so require the page to actually look
      // like the pinned company before believing it.
      const pinFitsPage = (r) => {
        const co = (r && r.company || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
        if (!co || co.length < 3) return false;
        // location.search + referrer matter on embedded ATS forms: inside
        // job-boards.greenhouse.io/embed/job_app?for=<employer>&token=… the employer's name
        // appears ONLY in the query string and in the embedding page's URL (the referrer) —
        // the visible body is just form fields — so excluding them made the RIGHT pin
        // unrecognizable exactly where it was needed most.
        const hay = (location.hostname + location.pathname + location.search + " " +
                     document.referrer + " " + (document.title || "") +
                     " " + (document.body ? document.body.innerText.slice(0, 4000) : ""))
                    .toLowerCase().replace(/[^a-z0-9]+/g, "");
        return hay.includes(co);
      };
      let freshPin = false;
      let pinsRejected = [];       // pins that were live but belong to some other employer
      if (!role) {
        // Same test for the tab's already-claimed pin. Its `origin` guard is too coarse on
        // shared ATS hosts: job-boards.greenhouse.io/temporal and .../precisely are the SAME
        // origin, so a claimed pin would follow you between two unrelated employers.
        const claimed = claimedPin();
        role = claimed && pinFitsPage(claimed) ? claimed : null;
        if (!role) {
          // ALL live pins, not just the newest. Clicking ↗ down a list of tabs is the normal
          // workflow, so the newest pin is routinely NOT the one whose form is open — asking
          // for a single pin meant the other tabs silently got the wrong role's résumé.
          const p = await send({ type: "pins" });
          const live = (p && p.roles) || [];
          const fits = live.filter(pinFitsPage);
          pinsRejected = live.filter((r) => !fits.includes(r));
          if (fits.length) { role = fits[0]; freshPin = true; }   // newest of the ones that fit
          for (const r of pinsRejected) {
            console.info("[JobScan] ignoring pin —", r.company, "does not appear on this page");
          }
        }
      }
      if (!role && company) {
        const top = await send({ type: "topRole", company });
        if (top && top.found) role = top.role;
        else {
          const p = await send({ type: "profile" });
          if (p && p.ok) role = { ...p, company, title: "profile fill (no role match)",
                                  has_tailored_pdf: false, no_role_match: true };
        }
      }
      // Still nothing: stay silent, as before. Showing a panel here would mean inventing a
      // role for a page we can't identify, which is the opposite of what C is for.
      if (!role) {
        noMatchFor = location.href;      // pill may now offer capture even on scanner-ATS hosts
        const n = ((misses.get(location.href) || {}).n || 0) + 1;
        misses.set(location.href, { n, next: Date.now() + missWait(n) });
        if (pinsRejected.length) {
          console.info("[JobScan] no panel — none of the", pinsRejected.length,
                       "live pins appear on this page:",
                       pinsRejected.map((r) => r.company).join(", "),
                       `— retrying in ${Math.round(missWait(n) / 1000)}s`);
        } else {
          console.info("[JobScan] no matching role for", location.href,
                       `— retrying in ${Math.round(missWait(n) / 1000)}s`);
        }
        return;
      }
      misses.delete(location.href);      // resolved — this URL is not a dead end after all
      noMatchFor = null;
      // Keep the FULL payloads: each rejected pin is a complete /api/pins entry (résumé flag,
      // profile, job_id), so the panel can offer it as a one-click manual override — the fix
      // for "the résumé exists in JobScan but the panel won't attach it" on employer pages
      // that never mention the company name the guard looks for.
      role = { ...role, pins_rejected: pinsRejected };
      const old = document.getElementById("jobscan-panel");
      if (old) old.remove();
      shownFor = location.href;
      panel(role);
      sparkleStart(role);             // ✨ chips on optional open questions (needs a real role)
      if (freshPin) claimPin(role);   // only now — a frame that merely probed must not eat it
    } finally { busy = false; }
  }

  // ---- Capture: for job pages that AREN'T in JobScan (Apple/MS/Google/Tesla and any other
  //      careers site the daily scanner can't reach). Reads the posting off the DOM and sends
  //      it to the local server, which scores it + queues it for tailoring. Always available as
  //      a small pill so she can browse any careers page and pull in whatever looks good. ----
  const cleanTxt = (s) => (s || "").replace(/\s+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();

  // Microsoft (an Eightfold board): list + detail-panel SPA. The DOM's biggest text block is
  // the LIST, so reading pixels captures forty job titles and no JD. The selected job's pid is
  // in the URL and the board serves a public same-origin JSON API — use the data, not the pixels.
  async function extractEightfold() {
    const pid = (location.search.match(/[?&]pid=(\d+)/) || [])[1];
    if (!pid) return { hint: "click a job first — the URL needs its pid= so I know which one" };
    try {
      const domain = location.hostname.split(".").slice(-2).join(".");
      const r = await fetch(`${location.origin}/api/apply/v2/jobs/${pid}?domain=${domain}`);
      if (!r.ok) return null;                       // fall through to the generic reader
      const j = await r.json();
      const div = document.createElement("div");
      div.innerHTML = j.job_description || "";
      const jd = cleanTxt(div.innerText || div.textContent || "");
      if (jd.length < 80) return null;
      return { title: (j.name || j.posting_name || "").slice(0, 160), jd: jd.slice(0, 14000),
               location: (j.locations || [])[0] || j.location || "" };
    } catch (e) { return null; }
  }

  // Google careers: the results page keeps the list on screen next to the open job, and every
  // list card has an h1/h3 of its own — "biggest block" grabs the list. The JD pane is the one
  // thing recognizable WITHOUT Google's rotating obfuscated class names: its section headings.
  function extractGoogleCareers() {
    const heads = [...document.querySelectorAll("h3")].filter((h) =>
      /^(about the job|minimum qualifications|preferred qualifications|responsibilities)/i
        .test((h.textContent || "").trim()));
    if (heads.length < 2)
      return { hint: "open a specific job first — this looks like the results list" };
    const parts = [];
    for (const h of heads) {
      let txt = (h.textContent || "").trim() + "\n";
      for (let n = h.nextElementSibling; n && n.tagName !== "H3"; n = n.nextElementSibling)
        txt += (n.innerText || "") + "\n";
      parts.push(txt);
    }
    const title = (((document.querySelector("main h2") || document.querySelector("h2") || {})
      .textContent) || (document.title || "").split(/[|—–-]/)[0] || "").trim().slice(0, 160);
    return { title, jd: cleanTxt(parts.join("\n")).slice(0, 14000) };
  }

  // Site chrome is not a title. Tesla's careers SPA puts its header nav in the FIRST <h1>
  // ("Tesla homepageCareersSkip to main content") — which then poisoned every capture's
  // role-family classification. Walk ALL h1s past the chrome, then og:title, then <title>.
  const chromey = (s) => !s || s.length > 100
    || /skip to (main )?content|homepage|cookie|sign.?in|log.?in|access denied|^careers?$|^menu$/i.test(s.trim());
  function saneTitle() {
    for (const h of document.querySelectorAll("h1, h2[class*='title' i], [class*='job-title' i]")) {
      const t = (h.textContent || "").trim();
      if (!chromey(t)) return t.slice(0, 160);
    }
    const og = document.querySelector('meta[property="og:title"]');
    const cand = (((og && og.content) || document.title || "").split(/[|–—·•]/)[0] || "").trim();
    return chromey(cand) ? "" : cand.slice(0, 160);
  }

  async function extractJob() {
    if (IS_MICROSOFT) { const r = await extractEightfold(); if (r) return r; }
    if (IS_GOOGLE) { const r = extractGoogleCareers(); if (r) return r; }
    const title = saneTitle();   // server re-derives from the URL slug if this comes back empty
    // biggest sensible text block wins — description containers first, then main/article, then body
    const cands = [...document.querySelectorAll(
      '[class*="description" i],[id*="description" i],[class*="job" i][class*="detail" i],'
      + 'main,article,[role="main"]')];
    let best = "", bestLen = 0;
    for (const el of cands) {
      const t = (el.innerText || "").trim();
      if (t.length > bestLen) { best = t; bestLen = t.length; }
    }
    if (bestLen < 300) best = (document.body.innerText || "").trim();
    return { title, jd: cleanTxt(best).slice(0, 14000) };
  }

  function capturePill() {
    if (document.getElementById("jobscan-capture")) return;
    const host = document.createElement("div");
    host.id = "jobscan-capture";
    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
      <style>
        :host{ all:initial; }
        .cap{ font:600 13px/1.3 -apple-system,system-ui,sans-serif; display:flex; align-items:center; gap:7px;
          background:#12314f; color:#fff; border-radius:999px; padding:9px 14px; cursor:pointer;
          box-shadow:0 4px 14px rgba(0,0,0,.22); user-select:none; white-space:nowrap; }
        .cap:hover{ filter:brightness(1.08); }
        .cap.busy{ opacity:.7; cursor:default; }
        .out{ margin-top:7px; font:600 12px/1.4 -apple-system,system-ui,sans-serif; color:#fff;
          background:#0f172a; border-radius:9px; padding:8px 11px; max-width:250px; box-shadow:0 4px 14px rgba(0,0,0,.22); }
        .out.good{ background:#15803d; } .out.warn{ background:#b42318; }
      </style>
      <div class="cap">📥 Capture to JobScan</div>
      <div class="out" hidden></div>`;
    host.style.cssText = "position:fixed !important; bottom:20px !important; right:20px !important;"
      + "left:auto !important; top:auto !important; z-index:2147483646 !important; margin:0 !important;";
    document.documentElement.appendChild(host);
    const cap = shadow.querySelector(".cap"), out = shadow.querySelector(".out");
    cap.onclick = async () => {
      if (cap.classList.contains("busy")) return;
      cap.classList.add("busy"); cap.textContent = "📥 reading…";
      const ex = await extractJob();
      cap.classList.remove("busy"); cap.textContent = "📥 Capture to JobScan";
      if (!ex || ex.hint || !ex.jd || ex.jd.length < 80) {
        out.hidden = false; out.className = "out warn";
        out.textContent = (ex && ex.hint) || "couldn't read a job description on this page";
        return;
      }
      cap.classList.add("busy"); cap.textContent = "📥 capturing…";
      out.hidden = false; out.className = "out"; out.textContent = "scoring…";
      const res = await send({ type: "capture",
        payload: { url: location.href, title: ex.title, jd: ex.jd, location: ex.location || "" } });
      cap.classList.remove("busy"); cap.textContent = "📥 Capture to JobScan";
      if (res && res.ok) {
        out.className = "out good";
        out.textContent = `✓ ${res.company} — ${res.title}\nscore ${Math.round(res.score)} · ${res.verdict}`;
      } else {
        out.className = "out warn";
        out.textContent = "capture failed: " + ((res && res.error) || "no response");
      }
    };
  }
  // The pill belongs on every careers page the daily scanner can NOT reach. Scanner-covered
  // ATS hosts stay pill-free (capturing those would just duplicate scanned roles). Google and
  // Microsoft are exactly the hosts capture EXISTS FOR, yet they were suppressed here as "ATS
  // form pages" — the fill panel only appears once a real application form renders, so their
  // browse/list/detail pages had neither panel nor pill and capture was unreachable. Now the
  // pill shows whenever the panel isn't up, and yields the moment it is.
  const SCANNER_ATS = IS_SR || IS_WORKDAY || IS_ASHBY
    || /greenhouse|lever|ashbyhq|myworkday|smartrecruiters|rippling|avature/i.test(location.hostname);
  function pillTick() {
    // Scanner-covered ATS hosts used to be unconditionally pill-free ("capture would just
    // duplicate scanned roles") — but companies NOT in config live on these hosts too, and
    // for them that rule left NOTHING on screen: no panel (role unknown) and no pill. Offer
    // capture exactly when this page's role resolution failed; a matched page stays pill-free.
    if (SCANNER_ATS && noMatchFor !== location.href) return;
    const panelUp = !!document.getElementById("jobscan-panel");
    const pill = document.getElementById("jobscan-capture");
    if (panelUp && pill) pill.remove();            // the apply form owns the UI
    else if (!panelUp && !pill) capturePill();     // browse/list/detail pages get the pill (back)
  }
  pillTick();
  setInterval(pillTick, 1500);

  // 1) poll for ~20s after load (covers the form rendering after a click to "Apply").
  //    The budget is WALL-CLOCK, not a tick count: Chrome throttles a background tab's
  //    timers to roughly one tick a minute, so counting ticks used to spend the whole
  //    budget while the SPA was still rendering — then clearInterval killed the poll for
  //    good and the panel never appeared on that tab again. That is the "open a second tab
  //    and the first one stops filling" bug.
  const POLL_MS = 20000;
  let deadline = Date.now() + POLL_MS;
  let poll = null;
  function startPoll() {
    if (poll) return;
    poll = setInterval(() => {
      tryShow();
      if (Date.now() > deadline) { clearInterval(poll); poll = null; }
    }, 500);
  }
  startPoll();
  // 2) react to DOM changes (form injected without a reload)
  new MutationObserver(() => tryShow()).observe(document.documentElement, { childList: true, subtree: true });
  // 3) react to SPA navigation (URL changed without a full page load)
  let lastUrl = location.href;
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href; shownFor = null;
      deadline = Date.now() + POLL_MS; startPoll();
      tryShow();
    }
  }, 800);
  // 4) a backgrounded tab coming back into view. Its poll window may have expired while
  //    throttled, and the SPA likely finished rendering while it was frozen — so no
  //    mutation will fire either. Nothing else re-arms it: returning to a tab is not a URL
  //    change. Give it a fresh window whenever it is visible with no panel up.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    if (document.getElementById("jobscan-panel")) return;
    shownFor = null;
    deadline = Date.now() + POLL_MS; startPoll();
    tryShow();
  });
  tryShow();
})();
