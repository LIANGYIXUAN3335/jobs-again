// JobScan content script — runs on Greenhouse / Lever / Ashby application pages
// (incl. when embedded as an iframe on a company careers site). It matches the page to
// a role in your local JobScan. Reviewed submissions require a bound one-use permit.

(function () {
  if (window.__jobscanLoaded) return;
  window.__jobscanLoaded = true;

  // Only our dedicated extension worker may dispatch; employer page scripts cannot call it.
  globalThis.chrome?.runtime?.onMessage?.addListener((message, sender, respond) => {
    if (!['jobscanRunnerReady','jobscanRunnerExecute'].includes(message.type)) return;
    if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL('runner.html') || window !== window.top) return;
    if (message.type === 'jobscanRunnerReady') {
      let gate;
      try { gate = globalThis.JobScanPageGate?.inspect() || {kind:'unknown',evidence:'页面检查组件尚未就绪。'}; }
      catch (error) { gate = {kind:'unknown',evidence:'页面检查出错：'+String(error.message || error).slice(0,200)}; }
      respond({ready:gate.kind === 'ready' && !!globalThis.JobScanRunReview,
        gate:{...gate,url:location.href},job_id:globalThis.JobScanRunnerJobId || null}); return;
    }
    if (!globalThis.JobScanRunReview) {respond({status:'blocked',reason:'申请入口尚未准备好。'});return;}
    globalThis.JobScanRunReview(message).then(respond,()=>respond({status:'unknown',reason:'执行中断；请核对原申请，不自动重试。'}));
    return true;
  });

  // Reloading the unpacked extension kills the background context but leaves THIS already-
  // injected script running on the page — every later sendMessage then throws "Extension
  // context invalidated". Catch that and report it as the one thing that fixes it: reload
  // the page. chrome.runtime.id goes undefined the moment the context dies.
  const CONTEXT_DEAD = "context-invalidated";
  const send = (msg) => new Promise((res) => {
    try {
      if (['localhost','127.0.0.1'].includes(location.hostname) && location.pathname === '/agent-lab/fixture' &&
          typeof globalThis.JobScanDemoTransport === 'function') return res(JobScanDemoTransport(msg));
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
  // Only a person's click counts. The employer's page shares this DOM, and what its scripts dispatch is untrusted.
  const pressed = (action) => (event) => { if (event.isTrusted) return action(event); };
  // Human-like pacing between field fills so forms don't flag the burst as a bot.
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  // Stop button: while filling, clicking the button again sets this. humanPause runs between
  // almost every field, so throwing there stops the fill promptly at the next field boundary.
  let abortFill = false;
  let runnerDeadline = 0;
  const checkDeadline = () => {
    if (runnerDeadline && Date.now() >= runnerDeadline) throw new Error('独立执行时间已结束；原申请保留，不继续填写或提交。');
  };
  const ABORT = "__jobscan_abort__";
  // Owner 2026-09-25 「切到别的标签页就不填了」: Chrome throttles a background tab's timers to
  // one tick per second (and to once a minute after ~5 min), so every 80 ms poll here — waiting
  // for a dropdown menu, a typeahead's suggestions, a Workday step — runs out of its window and
  // the field is reported "pick manually". Rather than fail silently, PAUSE at the next field
  // boundary while the tab is hidden and resume the moment it is visible again.
  let panelLog = null;                                          // set by panel(); humanPause reports through it
  let pausedNotice = false;
  const waitVisible = async () => {
    checkDeadline();
    if (document.visibilityState === "visible") return;
    if (!pausedNotice && panelLog) { panelLog("⏸ 标签页在后台，Chrome 会把计时器降速——已暂停，切回来自动继续", "warn"); pausedNotice = true; }
    // Unattended, a silent pause just looks like a hang: say so on the board too.
    if (runnerDeadline) send({type:'jobscanRunnerEvent',event:{role:'system',text:'⏸ 申请页不在前台，Filler 已暂停。切回这个标签页后自动继续。'}});
    while (document.visibilityState !== "visible") {
      checkDeadline();
      if (abortFill) throw new Error(ABORT);
      await sleep(400);
    }
    if (panelLog) panelLog("▶ 回到前台，继续填", "good");
    if (runnerDeadline) send({type:'jobscanRunnerEvent',event:{role:'system',text:'▶ 申请页回到前台，Filler 继续填写。'}});
    pausedNotice = false;
    checkDeadline();
  };
  const humanPause = async () => {
    checkDeadline();
    if (abortFill) throw new Error(ABORT);
    await waitVisible();
    await sleep(180 + Math.floor(Math.random() * 140));         // ~0.18–0.32s
    checkDeadline();
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

  function fieldLabels(el) {
    const labels = [el.getAttribute("aria-label") || ""];
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) labels.push(labelledBy.split(/\s+/)
      .map((id) => document.getElementById(id)?.textContent || "").join(" "));
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) labels.push(l.innerText);
    }
    const wrapLabel = el.closest("label") || (el.closest(".field, .form-field, [class*=field]") || {}).querySelector?.("label");
    if (wrapLabel) labels.push(wrapLabel.innerText);
    return labels.filter(Boolean);
  }

  function fieldText(el) {
    return `${el.name || ""} ${el.id || ""} ${el.placeholder || ""} ${fieldLabels(el).join(" ")}`.toLowerCase();
  }

  const PATTERNS = [
    // Specific preferred/legal labels must precede ordinary name fields.
    // FIRST: "How do you pronounce your name?" contains "name", so it would otherwise be
    // caught by one of the name patterns below and get filled with the name itself.
    ["name_pronunciation", /pronounc|phonetic|how (do|would) (you |we )?say your name/],
    // "Legal First & Last Name" asks for both, and the patterns below would each hand it one of them.
    ["legal_name", /\blegal\b.{0,12}\b(?:first|given)( name)?\s*(?:&|and|\/|,)\s*(?:last|family|sur)|\b(?:first|given)( name)?\s*(?:&|and|\/|,)\s*(?:last|family|sur)\w*( name)?\b.{0,12}\blegal\b/],
    ["full_name", /\b(?:first|given)( name)?\s*(?:&|and|\/|,)\s*(?:last|family|sur)|\b(?:last|family|sur)\w*( name)?\s*(?:&|and|\/|,)\s*(?:first|given)\b/],
    // Order matters: "preferred last name" / "preferred first name" both contain "preferred name",
    // so the specific ones must be tested first. A bare "Preferred name" gets the FULL name.
    ["preferred_last_name", /\bpreferred (last|family|sur)\s?name\b/],
    ["preferred_first_name", /\bpreferred first name\b|\bpreferred given name\b/],
    // Not a bare "preferred": "What is your preferred coding language?" once got a name typed into it.
    ["preferred_name", /\bpreferred (full )?name\b|\bnickname\b|\bgoes by\b/],
    ["legal_first_name", /\blegal (first|given)( name)?\b|\b(first|given) legal name\b/],
    ["legal_name", /\blegal (full )?name\b/],        // explicit "Legal name" → legal (Alex Example)
    ["first_name", /\bfirst name\b|\bgiven name\b/],
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

  // NEVER auto-fill sensitive, legal, voluntary, or user-choice fields.
  // 2026-09-09 owner rescope: legal/eligibility/EEO fields now auto-fill from recorded facts
  // (she reviews every form before submit). Machine-blank only comp + referral/source.
  // 2026-09-23 owner: the bare `referr` used to match "Preferred Name" too, so preferred-name boxes
  // were never filled; and "how did you hear" is no longer manual — the answer is always "Job board".
  const EXCLUDE = /salary|compensation|desired pay|\breferral\b|\breferred by\b|who referred/i;

  // Asks where she is. "Location" alone also sits in "the office location listed in the job posting".
  const WHERE_SHE_IS = /^\W*(?:(?:your|current|home)\s+)*(?:location|city)\b|\b(?:your|current)\s+(?:location|city)\b|\bcity\s*[,/]\s*state\b|\b(?:what|which)\s+city\b|\bwhere\b.{0,30}\b(?:located|based|live|reside)\b/i;
  // "Country of citizenship" and "City of birth" hold the word and ask something else, and only she can answer those.
  const NOT_WHERE_SHE_IS = /citizen|passport|birth|born|origin|national|native|naturali|visa|sponsor|relocat|previous|former|\blast\b|prior|issu|degree|earned|years|another|\bother\b/i;
  const HER_COUNTRY = /^\W*(?:current\s+)?country(?:\s*(?:\/|or)\s*(?:region|territory))?(?:\s+code)?(?:\s+of\s+(?:current\s+)?residence)?\W*$|\b(?:reside|residing|live|living|located|based)\b/i;
  // A box with no visible question has only its id to go on, and that id cannot be read as anything else.
  function asksHerPlace(el, key) {
    const said = fieldLabels(el).join(" ").replace(/\s+/g, " ").trim();
    if (NOT_WHERE_SHE_IS.test(said || fieldText(el))) return false;
    // An id can say job_application[location]. A question saying "the role" asks about the role.
    if (/\b(office|company|role|job|position|team|employer|school|university)\b/i.test(said)) return false;
    return !said || (key === "country" ? HER_COUNTRY : WHERE_SHE_IS).test(said);
  }

  function classify(el) {
    const t = fieldText(el);
    // "Preferred Pronouns" is not a preferred-name field. Keep this optional
    // self-identification empty unless the candidate supplies it explicitly.
    if (/\bpronouns?\b/i.test(t)) return null;
    if (EXCLUDE.test(t)) return null;               // demographics, EEO, comp, work-auth
    // Ashby prepends random IDs and placeholders to fieldText. Check a bare visible
    // "Name *" label separately so it cannot fall through to AI as an open question.
    // Do not broaden this to arbitrary "name" text (e.g. Current Company Name).
    if (fieldLabels(el).some((label) => /^\s*name\s*[*:]*\s*$/i.test(label))) return "full_name";
    for (const [key, re] of PATTERNS) {
      if (re.test(t)) return (key === "location" || key === "country") && !asksHerPlace(el, key) ? null : key;
    }
    return null;
  }

  function valueFor(key, profile) {
    // Ordinary names follow this role's name card. Only explicit legal-name fields
    // use legal_first_name; never replace an Casey application with the global name.
    const legalFirst = profile.legal_first_name || profile.first_name;
    const preferred = profile.preferred_name || profile.first_name;
    const legalFull = [legalFirst, profile.last_name].filter(Boolean).join(" ");    // Alex Example
    const preferredFull = [preferred, profile.last_name].filter(Boolean).join(" "); // Casey Example
    return {
      first_name: preferred, last_name: profile.last_name,
      legal_first_name: legalFirst,
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

  // ---- common logistics answers (deterministic, from the profile) ----
  // Only compensation and referral/source (SENSITIVE) stay manual. This block answers work
  // authorization / sponsorship from the recorded facts, plus lower-risk logistics like
  // residency/location, onsite/remote/relocate, and "have you worked here before".
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, 300);
  const short = (s) => clean(s).slice(0, 52);

  // Fields that must NEVER be auto-answered. They may be factual, but the candidate should
  // confirm them on the live form because they are sensitive, legal, voluntary, or irreversible.
  const SENSITIVE = EXCLUDE;

  function cityOnly(p) { return ((p.location || "").split(",")[0] || "").trim(); }
  // Profile `state` may be a full name ("Texas") or a code ("TX"); forms want either.
  function stateFull(p) { const raw = (p.state || (p.location || "").split(",")[1] || "California").trim(); return US_STATES[raw.toUpperCase()] || raw; }
  function stateAbbr(p) { const full = stateFull(p); return Object.keys(US_STATES).find((k) => US_STATES[k] === full) || (full.length === 2 ? full.toUpperCase() : ""); }
  function fullLoc(p) {
    const city = ((p.location || "").split(",")[0] || "").trim();
    const state = p.state || "California";
    return city ? `${city}, ${state}, United States` : `${state}, United States`;
  }

  // ---- work authorization / sponsorship (deterministic, from the recorded facts) ----
  // Owner 2026-09-09: legal/eligibility answers auto-fill from config; she reviews before
  // submit. The 2026-08-20 "safety" commit (658c20d) deleted these rules, so every such
  // question fell to the AI picker — whose prompt still refused them → blank — and on Workday
  // the required-field default then answered "authorized to work?" as No. Restored 2026-09-22.
  // Free-text boxes get the canonical spoken sentence (owner 2026-08-07 定稿); it never
  // volunteers the visa type, EAD, I-485 or green card — a form has to ask for the status.
  const LEGAL_CANON = "I'm authorized to work in the US and I don't require sponsorship.";
  const legalText = (p, yn) => (yn === "yes" ? "Yes. " : "No. ") + ((p && p.work_authorization_answer) || LEGAL_CANON);

  // The words an option can use to say which status she holds, on top of the workspace's own list.
  const STATUS_CLAIM = /\b(?:citizen\w*|national(?:s|ity)?|naturali[sz]\w*|permanent(?:ly)?|temporar\w+|green[- ]?cards?|gc|lpr|h-?1(?:-?b\d?)?|tn|opt|cpt|l-?1|o-?1|f-?1|j-?1|e-?3|daca|tps|asyl\w+|refugee\w*|passports?|clearances?|cleared|ts\/sci|(?:non-?)?immigrants?|(?:student|work|tourist) visa|visa holder|restrict\w*|limited|conditional\w*)\b/i;
  // "(such as OPT, CPT)" names a status only as an example, and claims nothing.
  const STATUS_EXAMPLE = /\(?\b(?:such as|e\.g\.|for example)[^)]*\)?/gi;
  // Sentence-style options for those two questions ("I am authorized to work for Lyft and do
  // not require sponsorship" / "I require, or in the future will require, sponsorship…" /
  // "Yes, I have US work authorization but will require sponsorship in the future"): SCORED,
  // not regex-matched, so the option that both affirms authorization and denies a sponsorship
  // need wins, and a plain /^yes/ can never grab the wrong "Yes, … but will require" twin.
  // Plain "Yes"/"No" options score too. Returns the winning index, or -1 when nothing fits
  // (a visa-STATUS list with no EAD option, say) so the caller can fall through to the AI.
  function pickLegalOption(texts, ans) {
    // `ans` is the commonAnswer() legal object: legal = 'auth' | 'spon', wa / ns = the workspace's
    // two facts, okTerms / wrongTerms = status words that are right / wrong for THIS candidate.
    const kind = typeof ans === "string" ? "auth" : (ans.legal === "spon" ? "spon" : "auth");
    const wa = typeof ans === "string" ? true : ans.wa !== false;
    const ns = typeof ans === "string" ? false : ans.ns === true;
    const esc = (x) => String(x).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const mk = (arr, dflt) => new RegExp("\\b(?:" + (arr && arr.length ? arr : dflt).map(esc).join("|") + ")\\b", "i");
    const okRe = mk(ans.okTerms, ["EAD", "employment authorization document", "adjustment of status", "pending"]);
    const wrongRe = mk(ans.wrongTerms, ["citizen", "permanent resident", "green card holder", "LPR", "H-1B", "TN", "OPT", "CPT", "L-1", "O-1", "non-immigrant visa", "student visa", "F-1"]);
    const score = (raw) => {
      const t = (raw || "").replace(/\s+/g, " ").trim();
      if (!t) return -9;
      const claims = t.replace(STATUS_EXAMPLE, " ");
      // a status this candidate does NOT hold — never pick it, even when it also says "authorized";
      // an option naming the real basis (EAD / F-1 OPT …) is exempt and earns a point
      const namesOk = okRe.test(claims);
      if ((wrongRe.test(claims) || STATUS_CLAIM.test(claims)) && !namesOk) return -9;
      let s = 0;
      const authYes = /^yes\b|^i am (legally |currently )?(authori[sz]ed|eligible|permitted)|\b(i am |i'm )?(legally |currently )?authori[sz]ed to work|\bhave (us |u\.s\. )?work authori[sz]ation|\bright to work\b/i.test(t);
      const authNo = /^no\b|\bnot (currently |legally )?(authori[sz]ed|eligible|permitted)|\bno (us |u\.s\. )?work authori[sz]ation|\bunauthori[sz]ed\b/i.test(t);
      const needDenied = /\b(do|does|will|would|should) not (currently |ever )?(need|require)\b|\b(don'?t|won'?t|doesn'?t) (currently |ever )?(need|require)\b|\bnot (need|require|going to (need|require))\b|\bno (visa |employment |immigration )?sponsorship\b|\bwithout (visa |any )?sponsorship\b|\bnever (need|require)\b/i.test(t);
      const needStated = /\b(will|may|might|do|does|would|currently) (need|require)\b|\b(i|we) (need|require)\b|\brequire,? or\b|\bsponsorship (is|will be) (needed|required)\b|\bneeds? (visa )?sponsorship\b|\brequires? (visa |employment |immigration )?sponsorship\b/i.test(t);
      if (namesOk) s += 1;
      if (kind === "auth") {                   // authorization question
        if (authYes) s += wa ? 2 : -3;
        if (authNo) s += wa ? -3 : 2;
        if (needDenied) s += ns ? -2 : 1; else if (needStated) s += ns ? 1 : -2;
      } else {                                 // sponsorship question
        if (needDenied) s += ns ? -3 : 2; else if (needStated) s += ns ? 2 : -3;
        if (/^no\b/i.test(t)) s += ns ? -3 : 2;
        if (/^yes\b/i.test(t)) s += ns ? 2 : -3;
        if (authYes && !needStated && !ns) s += 1;
      }
      return s;
    };
    let best = -1, bestScore = 0;
    texts.forEach((t, i) => { const sc = score(t); if (sc > bestScore) { best = i; bestScore = sc; } });
    return best;
  }

  // Returns {yesno:"yes"|"no"} for a Yes/No question, {text, choice} for a value question,
  // or null when we have no confident answer (→ falls through to the AI for open questions).
  const EXPORT_CONTROL = /\bu\.?s\.? person\b|export[- ](administration|control)|\bitar\b/i;
  // The one export question with a standing answer: being from such a country. Everything else about it turns on status.
  const EXPORT_COUNTRY = /\b(citizen|resident|national)\b.{0,80}\bexport[- ]controlled (countr|nation|region)/i;
  // Facts not on file and agreements only she can make. No rule answers these, in any mode.
  const NOT_ON_FILE = /clearance|background check|\bdrug|proof of|\b(start|begin)\b|\bsign(ing|ed)?\b|\bfriends?\b|non[- ]?compet|non[- ]?solicit|non[- ]?disclosure|\bnda\b|confidentiality|arbitration|convicted|felon/;
  // A claim about her status, whatever the rest of the sentence asks.
  const STATUS_TERM = /\b(citizen\w*|permanent resident|green card|passport|visas?|licen[cs]e[sd]?)\b/;
  // A statement she would be signing. In a batch nobody reads it first.
  const SIGNS = /\b(agree[sd]?|accept(s|ed)?|acknowledge[sd]?|certif(y|ies|ied)|attest(s|ed)?|consent(s|ed)?)\b/;
  // Interview recording, the one consent she has an answer to. "A track record of interviews" is not it.
  const RECORDING = /\binterviews?\b.{0,60}\b(?:be |being |to be )?record(?:ing|ed)\b|\brecord(?:ing|ed)\b.{0,60}\binterview|\b(?:may|can|could|will|would|might|to)\s+(?:we\s+|you\s+)?record\b.{0,60}\binterview|\brecording consent|\bconsent\b.{0,30}\brecord(?:ing|ed)?\b|notetak/;
  // She lives in the Bay Area and will not leave it, so this is the only place she can say Yes to being.
  const BAY_NAMES = "san francisco|sf|the bay|bay area|silicon valley|south bay|east bay|sunnyvale|san jos[eé]|santa clara|mountain view|palo alto|menlo park|redwood city|redwood shores|san mateo|foster city|burlingame|san bruno|san carlos|daly city|cupertino|milpitas|los altos|los gatos|oakland|berkeley|emeryville|fremont|hayward|pleasanton|san ramon|walnut creek|stanford|presidio";
  const BAY_AREA = new RegExp("\\b(?:" + BAY_NAMES + ")(?!\\w)(?:,? (?:ca|california)\\b)?", "i");
  // Places a lower-case label can still name; capitals below catch the rest.
  const ELSEWHERE = /\b(new york|nyc|seattle|bellevue|redmond|kirkland|austin|dallas|plano|houston|boston|denver|chicago|atlanta|charlotte|miami|los angeles|el segundo|santa monica|san diego|las vegas|portland|pittsburgh|washington,? d\.?c|bluffdale|salt lake|phoenix|philadelphia|quebec|québec|montreal|vancouver|toronto|london|riyadh|texas|new jersey|washington|oregon|colorado|florida|virginia|georgia|illinois|massachusetts|north carolina|utah|arizona|nevada|ohio|michigan|minnesota|pennsylvania|tennessee|europe|asia)\b/i;
  // Being somewhere with no name on it. "Other teams" and "other work" are not this.
  const PLACE_WORDS = /\b(?:outside|beyond|elsewhere|anywhere|wherever|another\s+(?:city|state|office|country|location)|different\s+(?:city|state|office|country|location)|other\s+(?:cit(?:y|ies)|states?|offices?|countr\w+|locations?)|any\s+(?:of\s+(?:our|the)\s+)?(?:\w+\s+)?(?:offices?|locations?|sites?|cit(?:y|ies)|states?)|all\s+(?:of\s+)?(?:our\s+)?(?:\w+\s+)?(?:offices|locations|sites)|multiple\s+(?:offices|locations|sites|cities|states)|out[- ]of[- ](?:state|town)|nationwide|east coast|west coast|midwest|northeast|southeast|southwest|northwest)\b/i;
  // The rest of a label after a Bay name: ", Costa Rica" or ", OH" says it is that place's namesake. ", Seattle, or" is a list.
  const NAMESAKE = /^\s*,\s*(?!CA\b|CALIF|Calif|US\b|USA\b|U\.S|United States\b)\p{Lu}[\p{L}.'’-]*(?:\s+\p{Lu}[\p{L}.'’-]*)*(?![\p{L}.'’-])(?!\s+\p{Lu})(?!\s*(?:,|\/|or\b|and\b))/u;
  const NAMESAKE_PLAIN = /^\s*,\s*(?!ca\b|calif|us\b|usa\b|u\.s|united states\b|or\b|and\b|if\b|at\b|in\b|on\b|for\b|are\b|can\b|will\b|do\b|would\b|could\b|should\b|is\b|how\b|what\b|please\b|\d)[\p{L}.'’-]+(?:\s+[\p{L}.'’-]+)?(?![\p{L}.'’-])(?!\s*(?:,|\/|or\b|and\b))/u;
  // The word in front of "Bay Area": "the Bay Area" is hers, "Tampa Bay Area" is not.
  const BAY_FRONT = /^(?:the|greater|sf|francisco|south|east|north|in|our|of|at|to|from|near|around|within|entire|whole|wider|broader|a|this|your|or|and|not|outside|metro|is|are)$/i;
  // A capitalized word in a place's spot that is still not a place. California is here because she lives there.
  const NOT_A_PLACE = /^(our|the|a|an|this|that|these|those|one|any|all|either|each|your|my|their|its|in|at|from|to|near|around|on|of|for|or|and|if|we|so|as|is|be|by|do|it|up|me|i|you|yes|no|ok|okay|please|note|ai|office|offices|hq|headquarters|headquarter|campus|location|locations|site|sites|area|metro|region|onsite|on-site|person|hybrid|remote|work|working|corporate|main|local|nearest|closest|nearby|physical|head|central|new|job|role|posting|assigned|designated|specified|preferred|selected|hiring|employer|bayarea|company|home|current|primary|listed|least|minimum|most|order|alignment|advance|general|total|addition|case|business|week|weeks|day|days|every|per|times|future|fact|hours|us|u\.s|usa|u\.s\.a|united|states|america|california|calif|ca|monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun|tu|th|m|f|january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|eastern|pacific|central|mountain|est|edt|pst|pdt|cst|cdt|mst|mdt|et|pt|ct|mt|gmt|utc|time)$/i;
  // This company's name, with or without the suffix forms drop ("Genius AI" asks about "Genius").
  function companyRe(company) {
    const full = (company || "").trim();
    if (!full) return null;
    const core = full.replace(/[\s,]+(inc|llc|ltd|corp|corporation|co|company|technologies|technology|labs?|ai|hq)\.?$|\.(ai|io|com)$/i, "");
    return new RegExp("(^|\\W)(" + [full, core].filter(Boolean).map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")(?=\\W|$)", "gi");
  }
  // Capitalized, or written the company's own way: "Factory" must not read "worked at a factory" as itself.
  function namesCompany(raw, company) {
    const re = companyRe(company);
    return !!re && [...raw.matchAll(re)].some((m) => /^[A-Z]/.test(m[2]) || m[2] === company.trim());
  }
  // Is the company itself what the label says she worked for? "a company that deployed on AWS Lambda" names it too.
  function workedAtCompany(raw, company) {
    const re = companyRe(company);
    return !!re && [...raw.matchAll(re)].some((m) => (/^[A-Z]/.test(m[2]) || m[2] === company.trim())
      && /\b(?:for|at|by|with|of|within)\s+(?:the\s+)?$/i.test(raw.slice(0, m.index + m[1].length)));
  }
  // Where does the label put her? `bay`: a Bay Area place it asks about. `elsewhere`: anything else, or the Bay Area
  // ruled out. `choice`: the two are offered as a list, so a Bay Area answer is a true Yes. No list holds every place,
  // so anything that could be one counts: a capital mid-sentence, a state code, a word in a place's spot.
  function whereAsked(text, company) {
    const co = companyRe(company);
    let s = (co ? text.replace(co, "$1employer") : text).replace(/\s+/g, " ");
    // ALL CAPS and Every Word Capitalized have no capitals left to read, and a label in lower case has none at all.
    const lettered = s.match(/\p{L}[\p{L}'’-]*/gu) || [];
    const capped = lettered.filter((w) => /^\p{Lu}/u.test(w)).length;
    const plain = capped <= 1 || capped > lettered.length * 0.6;
    if (plain) s = s.toLowerCase();
    let bay = false, other = false;
    s = s.replace(/([\p{L}.'’-]+) (bay(?: area)?)\b/giu, (m, front) => {
      if (BAY_FRONT.test(front) || !(plain || /^\p{Lu}/u.test(front))) return m;
      other = true;
      return front + " elsewhere";
    });
    s = s.replace(new RegExp(BAY_AREA.source, "gi"), (name, at, whole) => {
      const before = whole.slice(0, at);
      if ((plain ? NAMESAKE_PLAIN : NAMESAKE).test(whole.slice(at + name.length))) { other = true; return "elsewhere"; }
      // "if you're based in SF" and "while living in San Jose" say where she may be, not where the work is.
      if (/\b(?:if(?: you| you're| you are)?\s*(?:not|aren'?t|are not|do not|don'?t|outside|based|located|live|living|reside|residing|in|currently)|while)\b[^,?.;]*$/i.test(before)) return " bayarea ";
      if (/\b(?:not|rather than|instead of|outside(?: of)?|beyond|away from|out of|other than|apart from|except|excluding)\s+(?:(?:in|at|from|of|the|our)\s+)*$/i.test(before)) { other = true; return " bayarea "; }
      bay = true;
      return " bayarea ";
    }).replace(/\s+/g, " ");
    const placeWord = (word) => !NOT_A_PLACE.test(word);
    if (ELSEWHERE.test(s) || PLACE_WORDS.test(s)) other = true;
    for (const m of s.matchAll(/,\s*(\p{L}{2})(?![\p{L}\d])/gu)) if ((plain || /^\p{Lu}{2}$/u.test(m[1])) && placeWord(m[1])) other = true;
    if (!plain) {
      for (const m of s.matchAll(/\p{Lu}[\p{L}\p{M}\d.'’&-]*/gu)) {
        const before = s.slice(0, m.index);
        if (/^\s*$/.test(before) || /[.?!]\s+$/.test(before)) continue;     // a sentence's first word is capitalized anyway
        // A possessive is the employer only when the label also says where its office is ("GridCARE's offices in Redwood City").
        if (/['’]s$/.test(m[0]) && /^\s*offices? in bayarea\b/.test(s.slice(m.index + m[0].length))) continue;
        const parts = m[0].replace(/['’](?:s|m|re|ve|ll|d)$/, "").replace(/\.+$/, "").split(/[-/–—]+/);
        if (parts.some((part) => /^\p{Lu}/u.test(part) && placeWord(part))) other = true;
      }
    }
    // "our boulder office", in any case.
    for (const m of s.matchAll(/(?:^|[\s(])(?:our|the|your|this|that|an?)\s+([\p{L}][\p{L}'’-]*)\s+(?:offices?|hq|headquarters|campus|locations?|sites?)\b/giu)) if (placeWord(m[1])) other = true;
    if (plain) {
      for (const m of s.matchAll(/\b(?:(?:in|at|from|near|around|into|within)|out of|(?:commut\w*|relocat\w*|travel\w*|mov\w+|com\w+) to)\s+(?:downtown\s+)?(?:(?:our|the)\s+)?([\p{L}][\p{L}'’-]*)/gu)) if (placeWord(m[1])) other = true;
    } else if (/\bdowntown\s+\p{L}/iu.test(s)) other = true;
    const choice = /\beither\b/i.test(s) || /\bbayarea\b[^?.]{0,20}?(?:, ?| )(?:and\/)?or\b,? (?!relocat|move|work|commut|are\b|can\b|will\b|do\b|would\b|if\b)\S/i.test(s)
      || /\bor\b,? (?:if not,? )?(?:(?:from|in|at) )?(?:(?:our|the) )?bayarea\b/i.test(s);
    return { bay, elsewhere: other, choice };
  }
  // The facts behind a work-rights answer are about the US. Filler's other rules stop at a place; these also stop at
  // "a Swiss work permit" and "our Lisbon office". "for Crusoe" and "employment at Scribe" are who she works for, not where.
  const FOREIGN_WORDS = /\b(swiss|british|german|italian|french|spanish|dutch|irish|polish|portuguese|swedish|norwegian|danish|finnish|austrian|belgian|greek|turkish|israeli|indian|japanese|korean|chinese|taiwanese|singaporean|australian|mexican|brazilian|argentin\w+|colombian|filipino|vietnamese|thai|indonesian|malaysian|emirati|european|philippines|portugal|italy|colombia|emea|apac|latam|asia\w*|africa\w*|nordics?|benelux|dach)\b/;
  function foreignWorkRights(raw, asked, company) {
    if (FOREIGN_WORDS.test(asked)) return true;
    // No list holds every country: "in <place>" has to be the US, California, the Bay Area or the role's own country.
    if (new RegExp("\\bin (?!(the )?((near|foreseeable|immediate|distant|coming|next) )?future\\b|order\\b|which\\b|the country\\b(?! of\\b)|(this|that|your)\\b|the (role|position|location|job|next|coming)\\b|(the )?(u\\.s\\.|u\\.s\\b|us\\b|usa\\b|united states|america\\b)|connection with\\b|a (full|part)[- ]time\\b|any (us\\b|u\\.s|united states|way\\b|capacity\\b|form\\b|state\\b)|california\\b|(the )?(?:" + BAY_NAMES + ")(?!\\w))").test(asked)) return true;
    // "in the US and Portugal". "in the US or Canada" is still true of the US, and "and do not require sponsorship" is the same fact.
    if (/\b(?:in|within) (?:the )?(?:u\.s\.a?\.?|us|usa|united states(?: of america)?)(?!\w)\.?\s*(?:(?:and|&|plus|as well as)\b(?!\s+(?:(?:do|does|will|would)\s+)?(?:not\b|n['’]t\b|don['’]?t\b|won['’]?t\b))|,\s*(?!now\b|for\b|without\b|currently\b|today\b|either\b)\w|or\b(?!\s+canada\b))/.test(asked)) return true;
    // A bare title: "Work eligibility: Philippines", "Sponsorship (Philippines)".
    if (/^\W*(?:[\w.]+\s+){0,2}(?:work authori[sz]ation|work eligibility|sponsorship|right to work|work permit)\s*[-–—:(]\s*(?!(?:the )?(?:us\b|u\.s|usa\b|united states|america\b|california\b|required|optional|status|now\b|future\b|e\.g|i\.e|for\b|if\b|please|yes\b|no\b))\p{L}/u.test(asked)) return true;
    const co = companyRe(company);
    const s = (co ? raw.replace(co, "$1employer") : raw).replace(new RegExp(BAY_AREA.source, "gi"), " bayarea ");
    if (ELSEWHERE.test(s)) return true;
    if (BAY_AREA.test(raw) && whereAsked(raw, company).elsewhere) return true;
    const run = "((?:\\p{Lu}[\\p{L}.'’&-]*,?\\s*)+)";
    const spots = [new RegExp("\\b(?:[Ww]ork|[Ww]orking|[Rr]elocat\\w*|[Mm]ov(?:e|ing)|[Tt]ransfer\\w*)\\s+(?:to|at|within|across|out of|from|near|around)\\s+(?:(?:[Oo]ur|[Tt]he)\\s+)?" + run, "gu"),
                   new RegExp("\\b(?:[Oo]ur|[Tt]he)\\s+" + run + "(?:[Oo]ffices?|HQ|[Hh]eadquarters|[Cc]ampus|[Tt]eams?|[Ll]ocations?|[Ss]ites?)\\b", "gu")];
    // Once this company's name is out of the way, a name after "for" is another employer or a country.
    if (co) spots.push(new RegExp("\\b(?:authori[sz]ation|authori[sz]ed|eligib\\w+|permit|work|working)\\s+(?:\\w+\\s+){0,3}?for\\s+(?:the\\s+)?" + run, "gu"));
    for (const spot of spots) for (const m of s.matchAll(spot)) {
      if (m[1].split(/[\s,/-]+/).some((w) => /^\p{Lu}/u.test(w) && !NOT_A_PLACE.test(w.replace(/\.+$/, "")))) return true;
    }
    return false;
  }
  // She agreed to interview in Go or Java if a company insists, and Python is her own pick, so a question stating that language
  // is a standing Yes. Only those three: she never said so about any other. "Go" has to read as the language, not "go through".
  const INTERVIEW_LANGUAGE = /\b(?:Java|java|JAVA|Golang|golang|GoLang|Python|python)\b|\b(?:in|using|use|with|requires?)\s+Go\b|\bGo(?=\s+(?:coding|interview|assessment)\b)/;
  // "Coding" alone is a question about her skill ("Are you comfortable coding in Java?"), not a statement about an interview.
  const INTERVIEW_WORK = /\b(?:interview\w*|assessments?|coding\s+(?:\w+\s+)?(?:tests?|challenges?|exercises?|rounds?|screens?|sessions?|problems?|questions?))\b/;
  const GOES_ALONG = /\b(?:comfortable|ok|okay|willing|able|agrees?)\b/;
  // Every word such a question may hold, once the language and how long the interview runs are out. Anything else is a
  // second thing to say yes to, however gently it is put ("...and $90k base", "...at our HQ"), so a list of what to refuse
  // can never be complete. A word missing here costs one question to her, and a wrong Yes goes out under her name.
  const INTERVIEW_TALK = new Set(`a all an are at be can comfortable complete completing conducted coding do does doing done for held in interview interviewing interviews
    is it language must of ok okay on our perform please programming required requires role round rounds screen screens session sessions
    technical test tests that the this to use uses using we will willing with would you able agree agrees assessment assessments challenge challenges
    exercise exercises position problem problems question questions`.split(/\s+/));
  function statesInterviewLanguage(raw, t, company) {
    if (!INTERVIEW_LANGUAGE.test(raw) || !INTERVIEW_WORK.test(t) || !GOES_ALONG.test(t)) return false;
    const co = companyRe(company);
    const rest = (co ? t.replace(co, "$1") : t).replace(/\b(?:java|golang|python|go)\b/g, " ").replace(/\b(?:runs?|lasts?|takes?) \d+ (?:minutes?|mins?)\b/g, " ");
    return rest.split(/[\s.,;:!?()]+/).filter(Boolean).every(word => INTERVIEW_TALK.has(word));
  }
  // A rule's bare Yes or No goes into a text box only when the box asks a yes/no question. "Remote work preference" does not.
  const YES_NO_QUESTION = /^\W*(?:are|is|am|do|does|did|can|could|will|would|should|shall|may|might|must|have|has|had|i am|i'm|i have)\b|\?\W*$/i;
  // `asked` is all the form says at this control, the statement under the title too.
  function commonAnswer(label, profile, asked = label) {
    const raw = clean(label), t = raw.toLowerCase();
    if (!t) return null;
    // clean() stops at 300 characters, and what comes after still counts against a Yes.
    const whole = String(label).replace(/\s+/g, " ").trim(), all = whole.toLowerCase();
    // Answers the owner wrote on the 8787 board beat every rule below (app/confirmed_answers.py). One covers only what
    // she answered, so it is held against the whole text, not the cut one.
    // A sentence has no yesno, so a Yes/No control stays blank for the owner instead of guessing.
    const covers = String(asked).replace(/\s+/g, " ").trim().toLowerCase();
    for (const item of profile.confirmed_answers || []) {
      try { if (new RegExp(item.topic, "i").test(covers)) return { text: item.answer, choice: item.answer, yesno: item.yesno || undefined }; } catch (_) { /* bad pattern */ }
    }
    // "How/where did you hear about this job" — answer "Job board" (owner's pick, 2026-09-23; was LinkedIn).
    // Before the sensitive check: "(e.g. LinkedIn, referral)" is an example, not a referral question.
    // Before the essay guard too, which matches "how did you…?" and any label over 120 chars.
    if (/(how|where) did you hear\b|hear about (us|this)|(how|where) did you (learn about|find) (us\b|(this|our|the) (role|job|position|opening|opportunity|company|posting))|source of (this )?(job|opportunity|application)/.test(t)
        && !/\b(why|what)\b/.test(t))   // "How did you hear about us? Why this role?" also asks for an essay
      return { text: "Job board", choice: "Job board" };
    if (SENSITIVE.test(t)) return null;
    // Export rules turn on immigration status, not on work authorization: only the owner can answer.
    if (EXPORT_CONTROL.test(all) && !EXPORT_COUNTRY.test(all)) return null;
    // Living in the US is a fact about her. This comes before the foreign-country guard: "the US or Canada?" is still Yes.
    // Anything else said in that sentence is another claim ("in the US on a visa?"), so it stops here. A note after it is not.
    const inUS = t.match(/^\s*(are you|do you)\b.{0,20}\b(located|reside|residing|live|living|based)\b.{0,12}\bin (the )?(u\.s\.a\.?|u\.s\.?|usa\b|us\b|united states( of america)?)/);
    if (inUS) {
      const rest = t.slice(inUS[0].length), sentence = rest.split(/[?!]|\.(?:\s|$)/)[0];
      return /^( or (in )?canada)?\W*$/.test(sentence) && !rest.slice(sentence.length + 1).includes("?") ? { yesno: "yes" } : null;
    }
    // Work authorization / sponsorship — BEFORE the essay/length guard: Flexport's sponsorship
    // question runs 260+ chars and Workday's box text past 120, and both used to fall through
    // to the AI (→ blank) or to the required-field default (→ No). `legal` routes the answer through
    // pickLegalOption when the options are sentences instead of Yes/No.
    // (attestations about the answer — "I understand my response regarding work authorization
    // must be accurate" — are not the question; the acknowledgement rule below handles them)
    const legalQ = !/\b(i understand|i confirm|i acknowledge|i certify|must be accurate|most recent citizenship|become a permanent resident|dual citizen|country of citizenship)\b/.test(t);
    if (legalQ) {
      // Two facts per workspace (config profile.work_authorized / needs_sponsorship): the owner
      // is authorized and needs none; a candidate on F-1 OPT is authorized and WILL need it.
      const wa = profile.work_authorized !== false;
      const ns = profile.needs_sponsorship === true;
      const L = (yn, kind) => ({ yesno: yn, text: legalText(profile, yn), legal: kind, wa, ns,
                                 okTerms: profile.status_terms_ok, wrongTerms: profile.status_terms_wrong });
      const NO_L = L("no", "spon"), YES_L = L("yes", "spon");
      const AUTH_YES = L("yes", "auth"), AUTH_NO = L("no", "auth");
      // "Can you legally work in the US? Please explain if you require a visa." asks about
      // permission; the follow-up only asks for detail, so it must not flip the answer to No.
      const asked = (t.replace(/\b(?:please\s+)?(?:explain|specify|elaborate|let us know|tell us)\b.*$/, "").trim() || t)
        .replace(/\b(u\.s\.?|us|usa|united states) or canada\b/g, "$1");   // true of the US alone, so the rest does not change it
      // The facts are about the US. "Authorized to work in Canada?" is not; never answer it from them.
      if (/\b(canada|canadian|toronto|iceland|saudi|riyadh|united kingdom|uk|ireland|germany|france|netherlands|spain|poland|india|singapore|japan|australia|israel|mexico|brazil|uae|dubai|eu|european|europe|korea|taiwan|china|argentina|switzerland|sweden)\b/.test(asked)) return null;
      // Who sponsored whom in the past is on no file: only whether she will need it is.
      if (/\b(?:ever|previous(?:ly)?|prior|past|former(?:ly)?)\b[^?]{0,60}\bsponsor|\bsponsor\w*\b[^?]{0,40}\b(?:before|previously|in the past)\b|^\W*(?:have|has|did)\b[^?]*\bsponsor|(?<![-\w])sponsored\b|\bsponsoring\b/.test(asked)) return null;
      // "We do not offer sponsorship. Is that OK?" tells her something and asks nothing about her.
      const offered = /[^.?!]*(?:\b(?:do|does|will|can|could|is|are)(?:n['’]t| not)\b(?:(?!\b(?:require|need)\b)[^.?!]){0,40}\b(?:offer\w*|provid\w*|sponsor\w*|support\w*|hir\w*|availabl\w*)\b|\b(?:cannot|can['’]t|unable to|no longer)\b[^.?!]{0,40}\b(?:offer|provid\w*|sponsor\w*|support|hire)\b|\bsponsor\w*\b[^.?!]{0,30}\b(?:not|un)\s?(?:available|offered|provided|possible)\b)[^.?!]*[.?!]?/g;
      const question = asked.replace(offered, " ");
      // What is the question ABOUT? "Will you require sponsorship … to maintain your work
      // authorization?" asks whether she NEEDS sponsorship (No) even though it contains the
      // authorization wording; "are you authorized to work … without requiring sponsorship?"
      // asks whether she is AUTHORIZED (Yes) even though it mentions sponsorship. The old
      // "authorization first" order got the first kind wrong (18 of 74 logged phrasings), so
      // a require/need-sponsorship construction is recognised first, unless it is negated by
      // "without". "Require our assistance with work authorization" is the same question.
      const sponsorTalk = /\bsponsor/.test(question);
      const withoutSpon = /\bwithout\b[^?]{0,60}\bsponsor/.test(question);
      // "I do not require sponsorship" says the answer for her. Beside "I am authorized" it is that same fact.
      const statesNeed = sponsorTalk && (/\b(?:(?:do|does|will|would)(?:n['’]t| not)|don['’]?t|won['’]?t)\s+(?:\w+\s+)?(?:require|need)\b|\bnot\s+(?:require|need)\b|\bsponsor\w*\b[^.?!]{0,20}\bnot\s+(?:required|needed)\b/.test(question) || /^\W*i\b[^?]*\b(?:require|need)\b[^?]*\bsponsor/.test(question));
      const asksNeed = /\b(will|would|do|does|might|may|could|are)\s+you\b[^?]{0,60}?\b(require|need|going to need)\b[^?]{0,90}?\bsponsor|\brequire\b[^?]{0,20}\bsponsor|\bsponsor(ship)?\b[^?]{0,30}\b(required|needed)|(require|need)\b.{0,40}\b(assistance|support|help)\b.{0,25}(work authori|visa|immigration)|(require|need)\b.{0,30}(work authori[sz]ation|visa|immigration) (assistance|support|help)|(require|need)\b[^?]{0,40}\b(work permit|a visa|visa sponsorship|right to work support)\b|\b(require|need)\b[^?]{0,20}\bauthori[sz]ation\b/.test(question);
      const asksAuth = /authori[sz]ed to (lawfully |legally )?work|authori[sz]ation to work|legally (authorized|allowed|entitled|eligible|able to|permitted)\b.{0,20}\bwork|(legally|lawfully) work|legally eligible|(right|eligible|permitted|permission) to work|work authoriz|eligib(le|ility) to work|eligib(le|ility) to (begin|start) (work|employment)|work (permit|eligibility)/.test(question);
      const workWithout = withoutSpon && /\b(work|employ)/.test(question);
      if (/\bsponsor/.test(asked) && !sponsorTalk && !asksNeed && !asksAuth) return null;
      if ((asksNeed || asksAuth || sponsorTalk) && foreignWorkRights(whole, question, profile.company_name)) return null;
      if (statesNeed && !asksAuth) return null;
      if (asksNeed && !withoutSpon && !statesNeed) return ns ? YES_L : NO_L;          // "do you need sponsorship?"
      if (asksAuth || workWithout) {                                                   // "authorized (without sponsorship)?"
        // "Authorized to work" answers one fact. Whatever else the sentence claims with it is not that fact.
        const rest = all.replace(/eligib\w+ to (?:begin|start) (?:work\w*|employment)/g, " ");
        if (/citizen|permanent|green card|passport|clearance|ts\/sci|background|\b(?:start|begin)\b|immediate|restrict|limitation|\black(?:s|ing)?\b|unauthori|ineligib|\bnational\b|naturali|convict|felon|temporar|indefinite|long[- ]term|\bfor the next\b|\bexpir|\b(?:tied|depend\w*|through|via|based on)\b|\bon an? (?:\w+ )?visa\b|\bvisa (?:holder|status|type)\b|\bh-?1|\bopt\b|\bcpt\b|\bf-?1\b|\btn\b|\bl-?1\b|\bo-?1\b|\bead\b|\band that\b|(?<!\bif )\bnot\s+(?:currently\s+|legally\s+|lawfully\s+)*(?:authori|eligible|permitted|allowed|entitled)|\band (?:are|do you|have|hold|will|can|would|willing|able)\b/.test(rest)) return null;
        if (profile.unattended && SIGNS.test(rest)) return null;
        return (withoutSpon || statesNeed ? (wa && !ns) : wa) ? AUTH_YES : AUTH_NO;
      }
      // Anything else mentioning sponsorship has to ask whether she needs it, or be that bare topic.
      if (sponsorTalk) return /\b(?:requir|need)\w*\b[^?]{0,90}\bsponsor|^\W*(?:(?:visa|immigration|employment|employer|work|company)\s+){0,3}sponsorship\W*(?:status\W*)?$/.test(question) ? (ns ? YES_L : NO_L) : null;
    }
    // Recording an interview is optional however the form words it, and the owner opts out.
    if (RECORDING.test(t) && !/^\W*(?:have|did) you\b/.test(t))
      return {text:"Opt out of recording", choice:"Opt out", yesno:"no"};
    // "Where are you located?" names a place; the narrative guard below would take it for an essay.
    if (/^\s*where are you (currently )?(based|located)\b/.test(t)) return { text: fullLoc(profile) };
    // Office-days questions run long, and a long label is otherwise taken for an essay.
    // "Hybrid cloud" and "Office 365" hold the words and are about technology.
    const techWords = /\bhybrid\s+(?:cloud|search|retrieval|deployment|infrastructure|architecture|kubernetes|encryption|storage|database|rag|mobile|app|compute|network|transactional)|\boffice\s*365\b|\b(?:microsoft|ms) office\b|\boffice suite\b/.test(t);
    const officeDays = !techWords && /\b(in[- ]office|in the office|offices?|on-?site|in[- ]person|hybrid)\b/.test(t)
      && /\b(comfortable|able to|willing|would you|can you|are you|i am|i'm)\b/.test(t) && !/\b(based|located|reside|live in|which)\b/.test(t);
    // OPEN-ENDED essay questions must NEVER get a Yes/No or canned value — they go to the AI.
    // "Tell us about your experience working remote…" contains "remote", which used to trigger a
    // stray "Yes". A prompt that asks you to narrate is not an eligibility toggle.
    if (/\b(tell us|describe|walk (us|me)|share (your|a|some)|explain|elaborate|in your own words|give (us |me )?(an )?example)\b/.test(t)
        || /\byour experience\b/.test(t)
        || /\bwhat\b.{0,30}\b(challenge|experience|approach|practice|excite|draw|motivat|interest|learn|proud|accomplish)/.test(t)
        || /\b(what|how|why|when|where)\b.{0,60}\byou\b.{0,40}\?/.test(t)   // "what … do you …?" narrative
        || (t.length > 120 && !officeDays)) return null;
    const YES = { yesno: "yes" }, NO = { yesno: "no" };
    // Acceptable only because she reads the form before submitting. A batch has no such reader.
    const SOFT_YES = profile.unattended ? null : YES;
    const US = profile.country || "United States";
    // Citizenship / residency in an export-controlled or sanctioned country → No. Before the status check below:
    // this one names a citizenship only to deny it.
    if (/(citizen|resident|national|permanent resident)\b.{0,80}(cuba|syria|iran|north korea|crimea|donetsk|luhansk|sanction)/.test(t)
        || EXPORT_COUNTRY.test(t) || /sanctioned (countr|region|nation)/.test(t)) return NO;
    if (NOT_ON_FILE.test(all) || STATUS_TERM.test(all)) return null;
    // In every mode, and before a signed statement stops it: "agree" here is only to the language.
    if (statesInterviewLanguage(raw, t, profile.company_name)) return YES;
    // Soft rules below say Yes to working arrangements. Being somewhere else is not one: she will not leave the
    // Bay Area, so a place outside it, unless the Bay Area is one of the choices, goes to the AI.
    const where = whereAsked(whole, profile.company_name);
    const away = where.elsewhere && !(where.bay && where.choice);
    if (away && /office|on-?site|in[- ]person|relocat|commut|hybrid|remot|\blive\b|\bliving\b|\bresid|work(ing)? (at|in|from)|\blocat|\bbased\b|(role|position|job) is (in|at)\b/.test(t)) return null;
    // She cannot travel abroad, and no list tells a foreign city from a US one, so only unnamed travel or travel within the US stays a Yes.
    if (/\btravel/.test(t) && (where.elsewhere || /\b(international|abroad|overseas|globally|worldwide|foreign|outside|countr|border|across(?! the (?:us\b|u\.s|united states))|global|world|apac|emea|latam|asia)/.test(t))) return null;
    // eligibility / logistics (Yes/No)
    // Harmless required opt-ins (marketing / personalization / job recommendations / alerts) —
    // no "right" answer and won't affect the application, so just satisfy the field. Decline
    // (No) is the tidy default: fewer marketing emails. Owner's rule: "just pick one."
    if (/personali[sz]e|job recommendation|recommended content|marketing (email|communication)|promotional|stay up to date|receive (updates|alerts|newsletter)|\bopt[- ]?in\b/.test(t)) return NO;
    // Don't let them contact the CURRENT employer (job-search confidentiality) — a past/previous
    // employer is fine. "may we contact your current employer / present employer" → No.
    if (/(may we |can we |okay to )?contact.{0,30}(current|present)\s*employer|current employer.{0,20}contact/.test(t)) return NO;
    // Her own employers are the exception; "Have you ever worked with Kubernetes?" is about experience.
    const ownEmployer = /\b(tiktok|bytedance|meta|facebook)\b/.test(t);
    // "our company's competitors" and "a company such as Acme" ask about someone else she may well have worked for.
    const someoneElse = /\b(compet\w*|customers?|partners?|clients?|uses|using|such as|like)\b/.test(t);
    // She has worked elsewhere ("ever worked at a FAANG company?"), so No needs THIS company, as who she worked for.
    // "for us" reads the original case: lower-cased, "worked for US companies" would look like it.
    const usHere = /\b(for|by|with|of|at) us\b/.test(raw) || /\b(works?|worked|working|employed) here\b|\bhere before\b/.test(t);
    const thisCompany = workedAtCompany(raw, profile.company_name) || usHere || /\b(for|at|by|of|with|within) (this|our|the) (compan|organi[sz]ation|firm)|\b(current|former|previous|past)( or (current|former|previous|past))? (employee|worker|contractor)s?\W*$/.test(t);
    if (!ownEmployer && !someoneElse && thisCompany && (/(ever|previously|before|prior|former|formerly)\b.{0,40}\b(employed|employee|worked? (for|at|by|here|with us)|working (for|at))\b/.test(t)
        || /\bworked (for|at)\b.{0,40}\b(in the past|before)\b/.test(t) || /work on behalf of|contractor, agency|third[- ]party vendor/.test(t)
        || /(employ|work(ed)?)\b.{0,25}(for us|by us|here before|at (this|our) (compan|organi)|our compan|current or former)/.test(t) || /current or former (employee|worker|contractor)/.test(t))) return NO;
    // Related to / knows a current employee of THIS company → No. Family anywhere else is on no file.
    // "I have no relatives working here" is a statement, and a No to it says the opposite of what it seems to.
    if (/\brelated to\b|\b(family members?|relatives?|spouse|sibling|parent|partner)\b.{0,30}\b(work|employ)|do you (have|know)\b.{0,30}(relative|family member)/.test(t))
      return /\bi (?:have|am|do)\b|\bno (?:family|relatives?)\b/.test(t) ? null : namesCompany(raw, profile.company_name) || usHere || /\b(our|this|the) (compan|organi[sz]ation|firm)|\b(current|former) (employee|worker)s?\W*$/.test(t) ? NO : null;
    // Employed by / affiliated with a university, government, or public-sector entity → No (none
    // of these apply to her; this is the standard screening answer).
    if ((/(employ\w*|work\w*|affiliat\w*|officer|official|contractor)\b.{0,45}(universit|college|government|federal|state agenc|state government|public sector|public office|regulatory)/.test(t)
         || /\b(government|public)\s+(official|employee|officer|entity|agenc)/.test(t)) && !/authori[sz]|united states|\b(family|relatives?|spouse|sibling|parent|partner|anyone|someone)\b/.test(t)) return NO;
    // A statement to sign, however gently it is worded. Every Yes below would sign it in a batch.
    const signs = SIGNS.test(t) && !/decline/.test(t);
    if (signs && profile.unattended) return null;
    const moving = /relocat|\bmov(?:e|ing)\b(?! forward| on\b| fast| quickly)/;
    if (moving.test(t)) {
      if (!where.elsewhere && /\b(require|need)\b.{0,20}\brelocat/.test(t)) return NO;     // she lives here: "Will you require relocation?"
      // Yes only to a Bay Area place, read where the label says she would move to: "If not located
      // in SF, are you willing to relocate to New York?" names SF, and moving to SF is not the question.
      const at = all.search(moving), before = whole.slice(0, at), after = whole.slice(at);
      if (/\b(away|out of|outside|from|anywhere)\b/i.test(after)) return null;
      const to = (after.match(/^(?:relocat|mov)\w*\b(?:(?!\b(?:if|unless|when|while)\b)[^?.;])*?\b(?:to|near|in|around|within)\b([^?.;]*)/i) || [])[1] || "";
      const target = to.split(/\b(?:if|unless|when|while)\b/i)[0];
      if (target.trim()) {
        const there = whereAsked(target, profile.company_name);
        return there.bay && (!there.elsewhere || there.choice) ? YES : null;
      }
      // No target: "located in SF, or willing to relocate" means moving there, so the Bay Area has to be named first.
      const named = whereAsked(before.replace(/\bif not\b/gi, " "), profile.company_name);
      return named.bay && !where.elsewhere ? YES : null;
    }
    // Do you use / have experience with the company's OWN product or system → Yes (favorable and
    // truthful — she's an engineer; owner's rule: answer toward getting the interview).
    if (/(do you|have you|are you)\b.{0,45}(use|used|using|work(ed|ing)? (on|with)|experience (with|using)|familiar with|hands.?on|knowledge of)\b.{0,55}(workday|servicenow|our (product|platform|software|system|tool|technolog)|the (product|platform|system|software)(?=\W*$)|this (product|platform|system))/.test(t)) return YES;
    // Everything below says Yes to something she would do, so the label has to ask that. "Have you built remote
    // attestation?" and "Do you object to on-site work?" hold the same words and ask the opposite of a standing answer.
    const didIt = /\b(have you|did you|experience|built|certified|led|managed|implemented)\b/.test(t);
    const refuses = /\b(object\w*|cannot|can['’]t|can not|unable|unwilling|uncomfortable|prevent\w*|restrict\w*|limitations?|limit(?:s|ed)?|problems?|issues?|accommodat\w*|concerns?|difficult\w*|hardship|conflicts?|obstacles?|barriers?|impossible|refuse\w*|reason)\b|\bnot (?:able|willing|open|comfortable|available)\b/.test(t);
    const willing = !didIt && !refuses && !/^\W*which\b/.test(t) && /\b(?:(?:are you|i am|i'm|you're|you are|would you|will you)\b[^?.]{0,60}\b(?:able|willing|comfortable|open|ok|okay|available|excited|happy|prepared|ready|commutable|fine|flexible)\b|can you|could you|you can)/.test(t);
    const remote = /\bremot/.test(t);
    // Days in office, on-site, commuting and on-call are a standing Yes in every mode: the queue only holds Bay Area or remote roles.
    const arrangement = !techWords && (/on[- ]?site|in[- ]office|in person|\bhybrid\b|\bcommut(?:e|es|ing|able)\b|\bon[- ]?call\b/.test(t) || officeDays || (where.bay && /work(ing)? (at|in|from)/.test(t)));
    if (techWords) return null;
    if (remote && !arrangement) {
      // Open to remote work is a Yes. A remote role with a residency rule, or a need for remote work, is neither her answer nor on file.
      return willing && !/\b(only|require[sd]?|needs?|must|states?|qualify)\b[^.?!]{0,30}\bremot|\bremot[^.?!]{0,30}\b(only|require[sd]?|needs?|must|states?|qualify)\b/.test(t)
        && /\bwork(?:ing)? (?:\w+ )?remote(?:ly)?\b|\bremote(?:ly)?[- ](?:work|role|position|job|basis|setting|arrangement|environment|schedule|first)\b|\b(?:be|being|stay|go|fully) remote\b|\bremote(?:ly)?\W*$/.test(t) ? YES : null;
    }
    if (arrangement) return willing ? YES : null;
    if (/\btravel/.test(t) && !willing) return null;
    if (/able to travel|willing.*travel/.test(t)) return YES;
    // Generic favorable "are you willing / able to …" → Yes (aim for the interview).
    if (!refuses && !away && /are you (willing|able|comfortable|prepared|ready|open|available)\b.{0,40}(to|for|work)|can you (commit|work|commute|attend)|willing to (commit|undergo|travel)/.test(t)) return SOFT_YES;
    if (/\b18\b.*(older|age)|at least 18|over 18|of legal age|legally an adult/.test(t) && !/\band\b/.test(t)) return YES;
    // acknowledgements / confirmations the form needs a Yes on.
    if (/\b(do you |please |i )?(accept|confirm|acknowledge|agree|certif(y|ies)|consent)\b/.test(t) && !/decline/.test(t)) return SOFT_YES;
    // residency / country / location (value)
    // Only where she lives now. A country she was born in, holds a passport from or once lived in is not that one.
    if (/\bcountry\b/.test(t)) {
      if (/\b(citizen|passport|birth|born|origin|national|native|naturali)|home country|are you from\b/.test(t)) return null;
      const bareCountry = /^\W*(?:current\s+)?country(?:\s*(?:\/|or)\s*(?:region|territory))?(?:\s+code)?\W*(?:required\W*)?$|^\W*(?:your\s+|current\s+)*country of (?:current )?residence\b/.test(t);
      const livesThere = /\b(reside|residing|live|living|located|based)\b/.test(t);
      return (bareCountry || livesThere) && !/\b(previous|former|last|prior|past|other|another|ever|years?|family|have you|did you|were you|office|company|role|job|position|team|employer|school|university)\b/.test(t) ? { text: US, choice: US } : null;
    }
    if (/current (city|location)|city\/state|where are you (based|located|currently)|your (current )?location|based in|current residence/.test(t)) return { text: fullLoc(profile) };
    if (/^\W*state(?:\s*(?:[/,]|or)\s*province)?(?:\s*(?:[/,]|or)\s*region)?\W*$|^\W*province\W*$|\bstate(?:\/province)? of residence\b|\bstate\s*[/,]\s*province\b.*\b(?:live|reside|located|based)\b/.test(t)) {
      // owner 2026-09-23: the state comes from the profile (Texas for Taylor, California for her),
      // and a dropdown may list either the full name or the two-letter code — match both.
      const st = stateFull(profile), ab = stateAbbr(profile);
      return { text: st, choice: st, choiceAlt: ab };
    }
    if (willing && /able to work (at|in|from)|can you work (at|in|from)|position is (located|based)|role is (in|located|based)|work (at|from) (this|the) (location|office)/.test(t)) return YES;
    return null;
  }

  // The question text for a select / radio group / combobox (fieldset legend, label[for],
  // or the enclosing question container) — richer than fieldText, used for common answers.
  function controlLabel(el) {
    if (el.type === "radio") return globalThis.JobScanFormAgent?.radioQuestion(el) || "";
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
    if (want.legal) {                                   // sentence options: score, don't regex
      const i = pickLegalOption(opts.map((o) => o.text), want);
      if (i >= 0) target = opts[i];
    }
    // A legal answer has only the score: a first-word click would take "Yes - I have a green card".
    if (!target && want.yesno && !want.legal) {
      const yn = want.yesno === "yes" ? /^\s*y(es)?\b/i : /^\s*no?\b/i;
      target = opts.find((o) => yn.test(o.text));
    }
    if (!target && want.choice) {
      const c = want.choice.toLowerCase(), alt = (want.choiceAlt || "").toLowerCase();
      target = opts.find((o) => o.text.trim().toLowerCase() === c)
        || (alt && opts.find((o) => o.text.trim().toLowerCase() === alt || o.text.trim().toLowerCase().startsWith(alt + " ")))
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
    if (want.legal) {
      const i = pickLegalOption(radios.map(radioLabel), want);
      if (i >= 0) target = radios[i];
    }
    if (!target && want.yesno && !want.legal) {
      const yn = want.yesno === "yes" ? /^\s*y(es)?\b/i : /^\s*no?\b/i;
      target = radios.find((r) => yn.test(radioLabel(r)));
    }
    if (!target && want.choice) {
      const c = want.choice.toLowerCase(), alt = (want.choiceAlt || "").toLowerCase();
      target = radios.find((r) => radioLabel(r).toLowerCase().includes(c))
        || (alt && radios.find((r) => radioLabel(r).trim().toLowerCase() === alt));
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
      if (menu) {
        options = [...menu.querySelectorAll("[role=option]")];
        // a FUNCTION fallback is a scorer over the rendered options (pickLegalOption). It knows the
        // sentence twins and false statuses apart, so when it picks nothing, nothing gets clicked.
        if (options.length && typeof fallback === "function") opt = fallback(options) || null;
        else if (!opt) opt = options.find((o) => want.test(o.textContent));
      }
    }
    if (!opt && fallback && typeof fallback !== "function") opt = options.find((o) => fallback.test(o.textContent));
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
  // FIRST suggestion — which is what a human does here. A Yes/No answer is different: a filter
  // that matches anywhere lists "Yes, now" first for a typed "No", so only an option starting with
  // that word counts. `choose` (the options → one of them, or nothing) replaces both rules.
  async function pickTypeahead(control, input, text, choose) {
    if (!control || !input || !text) return { ok: false };
    const word = /^\s*(yes|no)\b/i.exec(text);
    const pick = choose || (word
      ? (els) => els.find((o) => new RegExp("^\\s*" + word[1] + "\\b", "i").test(o.textContent))
      : (els) => els[0]);
    if (input.getAttribute("aria-expanded") === "true") {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await sleep(120);
    }
    coordSeq(control);                                   // open
    await sleep(60);
    input.focus();
    setNativeValue(input, text);                         // filter
    let target = null;
    for (let i = 0; i < 22 && !target; i++) {             // async fetch — poll fast, bail late
      await sleep(80);
      const menu = document.getElementById(input.getAttribute("aria-controls") || "");
      const opts = menu
        ? [...menu.querySelectorAll("[role=option]")]
        : [...document.querySelectorAll('[role=option],[class*="select__option"]')].filter((o) => o.offsetParent);
      if (opts.length) target = pick(opts) || null;
    }
    if (!target) {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      // Blank for Reviewer to raise. Letters left in the box would read as her answer.
      if (choose || word) setNativeValue(input, "");
      return { ok: false };
    }
    const before = (control.innerText || "").trim();
    coordSeq(target);
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

  // 2026-09-15 restore: an 08-20 "safety" commit stubbed this out to warn-only, which
  // contradicted the 2026-09-09 owner rescope (EEO/self-ID auto-fills from her recorded
  // `eeo` config; she reviews before submit). Restored verbatim from pre-stub version.
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
  // A required Ashby choice no rule can answer: the model answers soft questions yes, hard ones from
  // the facts, and -1 leaves it blank so Reviewer stops and the owner is asked.
  const mayAskAI = (el, role) => {
    const entry = el.closest('[class*="_fieldEntry"], .ashby-application-form-field-entry');
    const label = entry && entry.querySelector("label");
    return !!(IS_ASHBY && role && role.ai_enabled && role.auto_dropdowns !== false && label && /(?:^|\s)_required/.test(label.className || "") &&
              !EXPORT_CONTROL.test(label.textContent || ""));
  };
  async function aiPickIndex(role, question, options) {
    const res = await send({ type: "pickOption", jobId: role.job_id, question, options });
    const index = res && typeof res.index === "number" ? res.index : -1;
    return index < options.length ? index : -1;
  }

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

  // An Ashby autocomplete no rule can answer goes to the model as a radio does, so it needs the options.
  // It may list nothing until you type, and only every first letter together is the whole list, whether
  // it filters by prefix or anywhere: the model must not choose from a part of the choices.
  async function aiPickTypeahead(input, control, label, role, log) {
    const escape = () => input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    const listed = () => {
      const menu = document.getElementById(input.getAttribute("aria-controls") || "");
      return menu ? [...menu.querySelectorAll("[role=option]")].map((o) => o.textContent.trim()).filter(Boolean) : [];
    };
    if (input.getAttribute("aria-expanded") === "true") { escape(); await sleep(120); }
    coordSeq(control);
    let options = [];
    for (let i = 0; i < 6 && !options.length; i++) { await sleep(80); options = listed(); }
    if (!options.length) {
      const seen = new Set();
      for (const letter of "abcdefghijklmnopqrstuvwxyz0123456789") {
        checkDeadline();
        setNativeValue(input, letter);
        await sleep(100);
        for (const option of listed()) seen.add(option);
      }
      setNativeValue(input, "");
      options = [...seen];
    }
    if (!options.length) { escape(); return false; }
    const index = await aiPickIndex(role, label, options);
    if (index < 0) { escape(); return false; }
    const r = await pickTypeahead(control, input, options[index], (els) => els.find((o) => o.textContent.trim() === options[index]));
    if (r.ok) log("AI picked: " + short(label) + " → " + short(options[index]) + " — verify it", "good");
    return r.ok;
  }

  // The form's own mark, as capture reads it. Ashby marks the question's label, not the box.
  const isRequired = (el) => {
    const entry = el.closest('[class*="_fieldEntry"], .ashby-application-form-field-entry');
    const label = entry && entry.querySelector("label");
    return !!(el.required || el.getAttribute("aria-required") === "true" || (label && /(?:^|\s)_required/.test(label.className || "")));
  };
  // A batch has nobody reading model text before Submit, and preflight only reads required questions.
  // So optional boxes wait for the board's essay switch.
  const essaysHeld = (profile) => !!runnerDeadline && !(profile && profile.allow_unread_essays);

  async function fillCommon(profile, log, role) {
    const leftover = [];
    // All the form says at this control. A confirmed answer has to cover the statement under the title as well.
    const askedAt = (el, label) => {
      const statement = globalThis.JobScanFormAgent?.statementOf?.(el) || "";
      return statement ? `${label} ${statement}` : label;
    };
    let acted = false;
    const pace = async () => { if (acted) await humanPause(); acted = true; };

    for (const sel of document.querySelectorAll("select")) {
      if (sel.disabled) continue;
      const label = controlLabel(sel);
      if (SENSITIVE.test(label)) continue;
      const ans = commonAnswer(label, profile, askedAt(sel, label));
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
      const ans = commonAnswer(label, profile, askedAt(r, label));
      if (ans && (ans.yesno || ans.choice)) {
        await pace();
        if (fillRadio(group, ans)) { log("answered: " + short(label), "good"); continue; }
      }
      // A rule that answered Yes/No or in words a radio cannot hold stays blank for Reviewer to raise.
      if (ans && !ans.choice) continue;
      if (DEMOGRAPHIC.test(label) || !mayAskAI(r, role)) continue;
      await pace();
      const index = await aiPickIndex(role, label, group.map(radioLabel));
      if (index >= 0) { group[index].click(); log("AI picked: " + short(label) + " → " + short(radioLabel(group[index])) + " — verify it", "good"); }
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
      // Ashby's autocomplete keeps its value in the input and lists nothing until you type.
      const autocomplete = IS_ASHBY && input.getAttribute("aria-autocomplete") === "list";
      if (autocomplete && ((input.value || "").trim() || /search schools/i.test(input.placeholder || ""))) continue;

      const ans = commonAnswer(label, profile, askedAt(input, label));
      // legal Yes/No: score the real option texts (a "Yes, … but will require sponsorship"
      // twin must lose to "Yes, … and do not require"); no score, no click
      const legalPick = ans && ans.legal
        ? (els) => { const i = pickLegalOption(els.map((o) => o.textContent), ans); return i >= 0 ? els[i] : null; }
        : null;
      let done = false;
      if (autocomplete) {
        const source = /how (did|do) you hear|where did you (hear|learn|find)|hear about (us|this)|source of (this )?(job|opportunity|application)|referral source/.test(label.toLowerCase());
        const said = ans && !ans.legal && ans.text;
        const yesNo = ans && ans.yesno ? (ans.yesno === "yes" ? "Yes" : "No") : "";
        // A place question wants the state as asked, or the city, which matches more than the full address.
        // A question answered Yes or No is not one, whatever it mentions ("based in or open to relocating…?").
        const place = !yesNo && (ans ? ans.text === fullLoc(profile) : WHERE_SHE_IS.test(label));
        const wording = source ? ["Job board", "LinkedIn", "Online", "Website", "Other"]
          : /\bstate\b|province|region/i.test(label) ? [said || yesNo]
          : place ? [cityOnly(profile) || said]
          : [yesNo || said, ans && !ans.legal && ans.choice];
        for (const text of new Set(wording.filter(Boolean))) {
          await pace();
          const r = await pickTypeahead(control, input, text, legalPick);
          if (r.ok) { log("selected: " + short(label) + " → " + short(input.value || text), "good"); done = true; break; }
          setNativeValue(input, "");
        }
        // As with a radio: a rule that could only say Yes or No stays blank for Reviewer to raise.
        if (!done && (!ans || ans.choice) && !SENSITIVE.test(label) && mayAskAI(input, role)) {
          await pace();
          done = await aiPickTypeahead(input, control, label, role, log);
        }
        if (!done && !SENSITIVE.test(label)) log("dropdown — pick manually: " + short(label), "warn");
        continue;
      }
      // "How did you hear about us?" — prefer "Job board" (owner 2026-09-23), then LinkedIn and the
      // other common sources, then the FIRST real option, so a required field never blocks.
      if (!done && /how (did|do) you hear|where did you (hear|learn|find)|hear about (us|this)|source of (this )?(job|opportunity|application)|referral source/.test(label.toLowerCase())) {
        await pace();
        // A role found on a job board is not one found on LinkedIn: xAI's list put LinkedIn first, and the Reviewer
        // refused it against her standing "Job board" (2026-11-08). So a site like a board comes first, LinkedIn last.
        let r = await pickReactSelect(control, input, /job\s?board/i, /job\s?(?:site|search|posting)|careers? (?:page|site|website)|company (?:web)?site|online|internet|search engine|google/i);
        if (!r.ok) r = await pickReactSelect(control, input, /^\s*other\b/i, /linkedin|social media/i);
        if (!r.ok) r = await pickAnyOption(control, input);   // still nothing → first real option
        if (r.ok) { log("how did you hear → " + short(r.picked), "good"); done = true; }
      }
      if (!done && ans && (ans.choice || ans.yesno)) {
        const want = ans.yesno ? (ans.yesno === "yes" ? /^\s*yes\b/i : /^\s*no\b/i)
          : new RegExp(escapeRe(ans.choice) + (ans.choiceAlt ? "|^\\s*" + escapeRe(ans.choiceAlt) + "\\b" : ""), "i");
        await pace();
        const r = await pickReactSelect(control, input, want, legalPick);
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
      const ans = commonAnswer(label, profile, askedAt(el, label));
      // value answer (country/location) OR a Yes/No question rendered as a free-text box
      // ("Have you previously worked at <company>?" → No; "Authorized to work?" → Yes).
      const val = ans && (ans.text || (ans.yesno && YES_NO_QUESTION.test(label) ? (ans.yesno === "yes" ? "Yes" : "No") : ""));
      if (val) { await pace(); el.focus(); setNativeValue(el, val); el.blur(); log("filled: " + short(label), "good"); continue; }
      if (SENSITIVE.test(fieldText(el))) continue;      // sensitive & no known answer → user handles
      if (essaysHeld(profile) && !isRequired(el)) continue;
      leftover.push({ el, label });
      if (leftover.length >= 15) break;
    }
    return leftover;
  }

  // Fills one standard field at a time with a human-like pause between each, so the form's
  // anti-spam heuristics don't flag an instant burst. `onField(key)` reports progress.
  // 「旧身份残值」判定（2026-09-09）：框里已有的值若正好是某张投递名片的已知值（名字/
  // 全名/邮箱/电话 —— Chrome 自带 autofill 或切换身份前的一次 Fill 留下的），且不等于
  // 本次要填的值，就允许改写。除名片值以外的任何已有内容（用户手填的）照旧不碰。
  function staleIdentityValue(key, cur, want, profile) {
    const cards = profile._cards;
    if (!cards) return false;
    const norm = (s) => String(s || "").trim().toLowerCase();
    const digits = (s) => String(s || "").replace(/\D/g, "").replace(/^1(?=\d{10}$)/, "");
    if (norm(cur) === norm(want)) return false;               // 已经是目标值，不用动
    for (const c of Object.values(cards)) {
      if (/^(first_name|preferred_first_name)$/.test(key) && norm(cur) === norm(c.first_name)) return true;
      if (/^(full_name|preferred_name|legal_name)$/.test(key) && norm(cur) === norm(c.display_name)) return true;
      if (key === "email" && norm(cur) === norm(c.email)) return true;
      if (key === "phone" && digits(cur) && digits(cur) === digits(c.phone_national)) return true;
    }
    return false;
  }

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
      if ((el.value || "").trim()) {
        // 旧身份的名字/邮箱/电话残值 → 改写成当前名片的（其他已填内容一律不碰）
        if (staleIdentityValue(key, el.value, val, profile)) {
          el.focus(); setNativeValue(el, val); el.blur();
          filled.push(key + " ⇄");
          if (onField) onField(key + " ⇄");
          continue;
        }
        skipped.push(key + " (already filled)"); continue;
      }
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
        // owner 2026-09-23: for a location picker type just the city ("Dallas") and take the first
        // suggestion — the full "City, State, Country" string often matches nothing (no "TX" option).
        const r = /^(location|country)$/.test(key)
          ? await pickTypeahead(control, el, key === "location" ? cityOnly(profile) || val : val)
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
    // A business master's is a different degree: Glean's list puts "Master of Business Administration (M.B.A.)" before
    // "Master's Degree", and the first match used to win (2026-11-08 dry run).
    if (/master|\bm\.?s\b|graduate/.test(d) && !/business|\bm\.?b\.?a\b/.test(d))
      return /^(?!.*(?:business|\bm\.?\s?b\.?\s?a\b)).*(?:master|\bm\.?\s?s\b|\bm\.?\s?tech|\bm\.?\s?eng|post.?grad|graduate)/i;
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
      // Discipline lists often lack the exact major ("Mechanical Engineering" missing is the
      // norm, not the exception) — owner 2026-09-15: fall back to the broad one instead of
      // leaving it for her to hunt every time.
      if (!(await ghSetTypeahead(`discipline--${i}`, e.discipline))) {
        const fb = (role.profile || {}).discipline_fallback;
        if (fb && await ghSetTypeahead(`discipline--${i}`, fb))
          log(`discipline: no "${e.discipline}" in the list → ${fb}`, "good");
      }
      // Dates are on some tenants only, under either name (Glean: start-year--0, required).
      for (const id of [`start-date-year--${i}`, `start-year--${i}`]) ghSetText(id, e.start_year);
      for (const id of [`end-date-year--${i}`, `end-year--${i}`]) ghSetText(id, e.end_year);
      for (const [id, month] of [[`start-month--${i}`, e.start_month], [`end-month--${i}`, e.end_month]])
        if (month && document.getElementById(id)) await ghSetCombo(id, /^\d+$/.test(month) ? monthName(month) : month);
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

  // The select's values are 1-12. The profile may say "January" or "01".
  function monthNumber(mm) {
    const named = "janfebmaraprmayjunjulaugsepoctnovdec".indexOf(String(mm).trim().slice(0, 3).toLowerCase());
    return String(named >= 0 && named % 3 === 0 ? named / 3 + 1 : parseInt(mm, 10));
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
    if (mm) { ok = setVerify(monthNumber(mm), true) && ok; await sleep(70); }   // "04" or "April" → "4"
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
  async function fillAshbyButtons(profile, log, role) {
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
      // Only the buttons' own words come off. A "no" inside a statement is the difference between two statements.
      let q = (block.innerText || "").replace(/\s+/g, " ").trim();
      for (const b of [...btns].reverse()) {
        const word = (b.textContent || "").trim();
        if (q.toLowerCase().endsWith(word.toLowerCase())) q = q.slice(0, -word.length).trimEnd();
      }
      if (SENSITIVE.test(q)) continue;
      const ans = commonAnswer(q, profile);
      if (!ans) {
        if (!mayAskAI(btns[0], role)) continue;
        const index = await aiPickIndex(role, q, btns.map((b) => (b.textContent || "").trim()));
        if (index >= 0) { btns[index].click(); await sleep(150); log("AI picked: " + short(q) + " → " + btns[index].textContent.trim() + " — verify it", "good"); }
        continue;
      }
      if (!ans.yesno) continue;
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
    // text fields by label
    let filled = [];
    for (const el of document.querySelectorAll('input[jsname="YPqjbf"]')) {
      if (!el.offsetParent || (el.value || "").trim()) continue;
      const l = gLabel(el).toLowerCase();
      let v = "";
      if (/first name/.test(l)) v = valueFor(/\blegal\b/.test(l) ? "legal_first_name" : "first_name", profile);
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
      ["first-name-input", valueFor("first_name", profile), "first name"],
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
    // "Confirm your email" (owner 2026-09-23, ServiceNow): it must equal whatever the Email box
    // holds — including an address the résumé-autofill or the owner typed — and the spl-input
    // sometimes re-renders after the first pass, so verify and retry once before giving up.
    {
      const emailEl = srInner("email-input");
      const want = ((emailEl && emailEl.value) || profile.email || "").trim();
      for (let attempt = 0; attempt < 2 && want; attempt++) {
        const c = srInner("confirm-email-input");
        if (!c) break;
        if ((c.value || "").trim() === want) break;
        srNativeSet(c, want);
        await sleep(300);
        const c2 = srInner("confirm-email-input");
        if (c2 && (c2.value || "").trim() === want) { if (!filled.includes("confirm email")) filled.push("confirm email"); break; }
        if (attempt === 1) log("Confirm email did not stick — type it yourself", "warn");
      }
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
  // <sr-question-field-select> (an spl-select combobox, driven by srPickSelect). Sensitive,
  // legal, voluntary, and attestation fields are logged for manual confirmation.
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
      if (SENSITIVE.test(t)) { log("manual confirmation needed: " + q.slice(0, 42), "warn"); continue; }
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
      if (SENSITIVE.test(label) || SENSITIVE.test(ph)) { log("manual confirmation needed: " + (label || ph).slice(0, 40), "warn"); continue; }
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
      else if (/how (did|do) you (hear|learn|find)|hear about|source/.test(label + " " + ph)) {
        // owner 2026-09-23: "Job board" first; if this tenant does not list it, LinkedIn / online / other
        if (await srPickSelect(cb, /job\s?board/i, ["J", "B"]) || await srPickSelect(cb, /linkedin|online|internet|other/i, ["L", "O"])) {
          n++; log("how did you hear → " + (cb.value || "").slice(0, 26), "good");
        }
        continue;
      }
      if (!want) continue;
      if (await srPickSelect(cb, want, seeds)) { n++; log("selected: " + (label || ph || "question").slice(0, 40) + " → " + (cb.value || "").slice(0, 26), "good"); }
    }

    // 3) attestation text fields (electronic signature + today's date)
    for (const w of deepAll("*").filter((e) => e.tagName === "SR-QUESTION-FIELD-TEXT")) {
      const q = (w.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
      const inp = deepAll("input, textarea", w).find((x) => x.getAttribute("role") !== "combobox");
      if (!inp || (inp.value || "").trim()) continue;
      if (SENSITIVE.test(q)) { log("manual confirmation needed: " + q.slice(0, 42), "warn"); continue; }
      if (/signature/.test(q)) {
        const nm = valueFor("legal_name", role.profile || {});
        if (srNativeSet(inp, nm.trim())) { n++; log("signed: " + nm.trim(), "good"); }
      } else if (/preferred.*name|nickname|goes by|what should we call you/.test(q)) {
        // owner 2026-09-23: a bare "Preferred name" question gets the preferred full name
        const nm = ((role.profile || {}).preferred_name || (role.profile || {}).first_name || "") + " " + ((role.profile || {}).last_name || "");
        if (srNativeSet(inp, nm.trim())) { n++; log("preferred name: " + nm.trim(), "good"); }
      } else if (/how (did|do) you (hear|learn|find)|hear about|source of/.test(q)) {
        if (srNativeSet(inp, "Job board")) { n++; log("how did you hear → Job board", "good"); }
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
          /\?|worked|relocat|employ|citizen|gender|ethnic|race|pwc|sponsor|riseup|nextgen|authorized|hear|source|preferred/i.test(txt)) return txt;
      n = n.parentElement || (n.getRootNode && n.getRootNode().host);
    }
    return inp.placeholder || "";
  }

  function attachResume(bytes, name, preferred) {
    // prefer an input whose context says "resume"/"cv", not "cover letter"
    let target = preferred;
    if (!target) {
      // Ashby's autofill uploader only parses a file; the answer belongs in the Resume field.
      const files = [...document.querySelectorAll('input[type=file]')]
        .filter((f) => !f.closest('.ashby-application-form-autofill-input-root'));
      target = files.find((f) => /resume|cv/i.test(fieldText(f)) && !/cover/i.test(fieldText(f)));
      if (!target) target = files.find((f) => !f.accept || /pdf/i.test(f.accept));
    }
    if (!target) return false;
    const dt = new DataTransfer();
    const file = new File([bytes], name, { type: "application/pdf" });
    dt.items.add(file);
    // Greenhouse takes the input away once the file is uploaded; the Reviewer then checks these bytes.
    globalThis.JobScanFormAgent?.rememberPicked?.(file);
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
    // Before anything is written: a snapshot must never carry this to the Reviewer model, even if she pastes it in herself.
    const written = (...elements) => globalThis.JobScanFormAgent?.codeWritten?.(code, elements);
    written();
    const boxes = otpBoxes();
    if (boxes && code.length === boxes.length) {         // split OTP boxes → one digit each
      written(...boxes);
      boxes.forEach((el, i) => { el.focus(); setNativeValue(el, code[i]); });
      boxes[boxes.length - 1].blur();
      return true;
    }
    const el = codeField();
    if (el) { written(el); el.focus(); setNativeValue(el, code); el.blur(); return true; }
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
    // Her review is what makes ticking these acceptable, and a batch has none. An agreement stays hers to sign.
    if (runnerDeadline) return 0;
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
  const HEARD_CHAIN = ["Job Board", "LinkedIn", "Internet Search", "Online", "Other"];   // owner 2026-09-23: Job Board first

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
    // Fill required questions from the recorded facts (work auth / sponsorship included);
    // only compensation and referral/source (SENSITIVE) are skipped for manual confirmation.
    const YES = /^\s*yes\b/i, NO = /^\s*no\b/i;
    const profile = role.profile || {};
    const rules = [
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
        if (SENSITIVE.test(box.innerText)) {
          log("manual confirmation needed: " + short(clean(box.innerText)), "warn");
          break;
        }
        const r = await wdPrompt(new RegExp(escapeRe(box.innerText.slice(0, 40))), want);
        if (r !== "ok") { log(name + " — pick manually", "warn"); break; }
        log("answered: " + name, "good");
      }
    }

    // Pass 2 — catch-all for non-sensitive required Select-One fields. An unrecognized OPTIONAL
    // question is left alone.
    for (const box of wdBoxes().filter((b) => /select one/i.test(b.innerText))) {
      const q = clean(box.innerText).replace(/\bselect one\b\s*\*?/i, "").trim();
      if (SENSITIVE.test(q)) continue;
      const ca = commonAnswer(q, profile);
      let want = null, note = "";
      // A legal answer never lands on "Yes - I have a green card" here either.
      if (ca && ca.yesno) want = ca.legal ? new RegExp("^\\s*" + ca.yesno + "\\b(?!.*" + STATUS_CLAIM.source + ")", "i") : ca.yesno === "yes" ? YES : NO;
      else if (ca && (ca.choice || ca.text)) want = new RegExp("^\\s*" + escapeRe(ca.choice || ca.text), "i");
      if (!want) {
        const required = /\*/.test(box.innerText) || /\brequired\b/i.test(box.innerText);
        if (!required) continue;
        want = /\b(willing|able|available|comply|comfortable)\b/i.test(q) ? YES : NO;
        note = " (defaulted — please verify)";
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
    if (!role.ai_enabled || essaysHeld(role.profile)) return;
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
    if (r && !r.ok && /^MISMATCH:/i.test(r.error || "")) {
      // mismatch fuse: different-field specialist role — no letter is better than a letter
      // that opens with what she lacks. Box stays empty on purpose.
      log("⚠️ " + (r.error || "").replace(/^MISMATCH:\s*/i, "mismatch fuse: ") + " — box left empty on purpose", "warn");
      return;
    }
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
    log("Desired Salary is required — fill it yourself after confirming the number", "warn");
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
      log("signature / legal attestation left for you to type: " + legalFull, "warn");
    }
    return n;
  }

  // 2026-09-15 restore: same 08-20 stub-out as fillDemographics — see note there.
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
  // What a refusal means for her, on the panel and after a hand submit alike.
  const EMPLOYER_SAID_NO = '雇主说不收这份。这家公司先不投了，它写了多久就停多久。不用再提交。';
  // form_agent keeps its success check to itself. Its preflight runs that check first and answers in these words.
  const RECEIPT_SEEN = /投递成功回执/;
  let endHandWatch = null;   // a panel rebuilt for this page takes over the last one's watch
  function panel(role) {
    // Shadow DOM so the host page's CSS can't bleed in and overlap our text.
    const host = document.createElement("div");
    host.id = "jobscan-panel";
    // Closed: an open root would let the employer's page read this log and reach these buttons.
    const shadow = host.attachShadow({ mode: "closed" });
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
        .chip.idcard{ display:block; background:#f1f5f9; color:#475569; margin-bottom:6px; cursor:help; }
        .chip.idcard.alt{ background:#1a73e8; color:#fff; }
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
          <button class="ok agent-review">Fill + Review</button>
          <button class="ok agent-review-only">Review current form</button>
          <button class="ok agent-submit">Fill → Review → Submit（此份）</button>
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
    // Titles and company names come from the employer's posting, and this string goes in as markup.
    const text = (x) => String(x == null ? "" : x).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
    let appliedWarn = "";
    if (role.this_status === "applied" || role.this_status === "interviewing") {
      appliedWarn = guessed
        ? `<div class="chip company-warn" title="This role was not matched from the page URL, so this may not be the posting you are on.">⚠ the role shown here (“${text(role.title)}”) is marked “${text(role.this_status)}” — but it was not matched from this URL, so it may not be this posting</div>`
        : `<div class="chip applied-warn">⚠ you already marked THIS role “${text(role.this_status)}”</div>`;
    } else if ((role.company_applied || []).length) {
      const a = role.company_applied;
      const names = a.slice(0, 2).map((x) => text(x.title)).join("; ") + (a.length > 2 ? ` +${a.length - 2} more` : "");
      appliedWarn = `<div class="chip company-warn" title="${text(a.map((x) => x.title + " · " + x.status + (x.date ? " · " + x.date : "")).join("\n"))}">↩ you've applied to ${text(role.company)} before (${a.length}): ${names}</div>`;
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
      ? `<div class="chip company-warn" title="${text(rejectedNames.join("\n"))}">📌 ${rejectedNames.length} other pinned role(s) ignored — they belong to a different company than this page</div>`
      : "";
    // "no tailored résumé" means the upload below is the BASE résumé, not one written for this
    // JD. Spell that out — "text only" read as a formatting note, not as "wrong file".
    const resumeChip = role.has_tailored_pdf
      ? '<span class="chip ready">tailored résumé ready</span>'
      : `<span class="chip none" title="No tailored PDF exists for this role, so nothing role-specific will be attached. Tailor it in JobScan first if this application matters.">⚠ no tailored résumé for this role — base/profile fields only</span>`;
    // 投递身份行：这份表单会用哪张名片填 —— 名字/邮箱/电话/简历抬头全跟着它走。
    // 默认名片灰底；小号（法名 Alex）蓝底，一眼能看出"这份不是常规身份"。
    const ap = role.apply_profiles || {};
    const apKeys = Object.keys(ap.profiles || {});
    const apKey = role.apply_profile || ap.default;
    const apCard = (ap.profiles || {})[apKey];
    const idChip = apCard
      ? `<div class="chip idcard${apKey !== ap.default ? " alt" : ""}" title="自动填表用这张名片：first name、email、电话都按它填，附的简历 PDF 抬头也是它。想换身份用下面的 ⇄ 按钮。">📇 ${text(apCard.label)} ${text(apCard.display_name)} · ${text(apCard.email)}</div>`
      : "";
    $(".status").innerHTML = pinNote + rejectedNote + appliedWarn + idChip + resumeChip;
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
        b.onclick = pressed(() => {
          claimPin(pr);
          const cur = document.getElementById("jobscan-panel");
          if (cur) cur.remove();                    // panel() builds a fresh host each call
          const chosen = { ...pr, pin_override: true };
          panel(chosen);
          sparkleStart(chosen);                     // ✨ chips need the real job_id too
        });
        $(".status").appendChild(b);
      }
    }
    const log = (m, cls) => { const d = document.createElement("div"); if (cls) d.className = cls; d.textContent = m; $(".log").appendChild(d); };
    panelLog = log;                                              // let humanPause's pause/resume notices reach this panel

    // ⇄ 身份切换按钮：已投/面试中的岗位不显示（服务端也会 409 拒绝 —— 申请已经用旧抬头
    // 发出去了，事后换名片只会让记录和事实对不上）。切换成功后整个面板用新数据重建，
    // 下面的 Fill 就会按新名片填。
    const idLocked = role.this_status === "applied" || role.this_status === "interviewing";
    if (apCard && apKeys.length > 1 && role.job_id && !idLocked) {
      const next = apKeys[(apKeys.indexOf(apKey) + 1) % apKeys.length];
      const nc = ap.profiles[next];
      const sb = document.createElement("button");
      sb.type = "button";
      sb.style.cssText = "display:block;width:100%;text-align:left;cursor:pointer;" +
        "margin-top:0;margin-bottom:6px;border:1px dashed #64748b;background:#f8fafc;font-weight:600;";
      sb.textContent = `⇄ 这份改用 ${nc.label} ${nc.display_name} 投（${nc.email}）`;
      sb.title = "切换这个岗位的投递身份：简历立刻换抬头重打（内容不变），自动填表也换成这张名片的名字/邮箱/电话。";
      sb.onclick = pressed(async () => {
        sb.disabled = true; sb.textContent = "切换中…";
        const res = await send({ type: "setProfile", jobId: role.job_id, profile: next, url: location.href });
        if (res && res.ok) {
          let updated;
          if (res.role) {
            updated = { ...role, ...res.role };          // 保留 from_pin/pin_override 等本地标记
          } else {
            // URL 认不出角色（pin/LinkedIn 中转页）拿不到服务端全量 —— 就地覆盖名片字段
            updated = { ...role, apply_profile: next, profile: { ...role.profile } };
            for (const [src, dst] of [["first_name", "first_name"], ["preferred_name", "preferred_name"],
                                      ["email", "email"], ["phone_national", "phone"]]) {
              if (nc[src]) updated.profile[dst] = nc[src];
            }
          }
          const cur = document.getElementById("jobscan-panel");
          if (cur) cur.remove();
          panel(updated);
          sparkleStart(updated);
        } else {
          sb.disabled = false;
          sb.textContent = `⇄ 这份改用 ${nc.label} ${nc.display_name} 投（${nc.email}）`;
          log(`身份切换失败：${(res && res.error) || "unknown"}`, "warn");
        }
      });
      $(".status").appendChild(sb);
    }

    // 🚫 这家不让投了 (owner 2026-09-25): she learns about an application limit ON the form, so
    // the cooldown is set right here — no trip back to the review page. Company-level, per
    // workspace (same table the review page's 🚫 writes), expires by itself; also marks the
    // role on screen skip (never an applied/interviewing one). Shows the current cooldown first.
    if (role.company) {
      // Owner 2026-09-25: one line, tiny pills — the panel's generic `button` rule (flex, full
      // width, bold, big padding) must be overridden property by property here.
      const CD_BTN = "display:inline-block;width:auto;flex:0 0 auto;margin:0;padding:0 6px;height:18px;line-height:16px;" +
        "font-size:11px;font-weight:500;border:1px solid #cbd5e1;border-radius:999px;background:#fff;color:#334155;cursor:pointer;";
      const cdRow = document.createElement("div");
      cdRow.style.cssText = "margin:4px 0 6px;";
      const canSkip = role.job_id && !(role.this_status === "applied" || role.this_status === "interviewing");
      const renderActive = (cd) => {
        cdRow.innerHTML = "";
        const chip = document.createElement("div");
        chip.className = "chip company-warn";
        chip.style.cssText = "display:flex;justify-content:space-between;align-items:center;gap:6px;";
        const left = (cd.days_left != null) ? `（还剩 ${cd.days_left} 天）` : "";
        // Text, never markup: the reason can hold an employer's own words, and a script from that page would see this closed panel.
        const said = document.createElement("span");
        said.textContent = `❄️ ${role.company} 冷却中 → 到 ${cd.until}${left}${cd.reason ? " · " + cd.reason : ""}`;
        chip.appendChild(said);
        chip.title = "这家公司在冷却期：扫描跳过、review 页隐藏、自动排队不排。到期自动解除。";
        if (cd.source !== "config") {
          const undo = document.createElement("button"); undo.type = "button"; undo.textContent = "撤销";
          undo.style.cssText = CD_BTN;
          undo.onclick = pressed(async () => {
            undo.disabled = true;
            const res = await send({ type: "cooldownClear", company: role.company });
            if (res && res.ok) { log(`已解除 ${role.company} 的冷却`, "good"); renderPicker(); }
            else { undo.disabled = false; log(`解除失败：${(res && res.error) || "unknown"}`, "warn"); }
          });
          chip.appendChild(undo);
        } else {
          const note = document.createElement("span"); note.style.cssText = "font-size:11px;color:#6b7280;"; note.textContent = "（写在 config.yaml 里）"; chip.appendChild(note);
        }
        cdRow.appendChild(chip);
      };
      const renderPicker = () => {
        cdRow.innerHTML = "";
        const line = document.createElement("div");
        line.style.cssText = "display:flex;flex-wrap:nowrap;align-items:center;gap:3px;font-size:11.5px;white-space:nowrap;overflow-x:auto;";
        const lab = document.createElement("span"); lab.textContent = "不让投了？冷却："; lab.style.cssText = "color:#6b7280;font-size:11px;"; lab.title = "ATS 说投递次数到上限 / 一次只能投一个？点天数把这家公司整体冷却，不用回 review 页找。"; line.appendChild(lab);
        const setIt = async (days, until, label) => {
          for (const b of line.querySelectorAll("button")) b.disabled = true;
          const today = new Date().toISOString().slice(0, 10);
          const res = await send({ type: "cooldown", company: role.company, days, until,
            reason: `ATS 投递上限，${today} 在申请页发现（${label}）`, skipJobId: canSkip ? role.job_id : null });
          if (res && res.ok) {
            log(`${role.company} 已冷却到 ${res.until}` + (res.hidden_roles ? `，review 里 ${res.hidden_roles} 个岗位隐藏` : "") + (res.skipped_role ? "，这个岗位已标 skip" : ""), "good");
            renderActive({ until: res.until, days_left: res.days_left, reason: res.reason, source: "manual" });
          } else {
            for (const b of line.querySelectorAll("button")) b.disabled = false;
            log(`冷却失败：${(res && res.error) || "unknown"}`, "warn");
          }
        };
        for (const d of [30, 60, 90, 180]) {
          const b = document.createElement("button"); b.type = "button"; b.textContent = `${d}天`;
          b.style.cssText = CD_BTN;
          b.onclick = pressed(() => setIt(d, null, `${d} 天`));
          line.appendChild(b);
        }
        const c = document.createElement("button"); c.type = "button"; c.textContent = "自定义";
        c.style.cssText = CD_BTN;
        c.onclick = pressed(() => {
          const v = (prompt(`冷却 ${role.company} 多久？填天数（如 45）或日期（如 2027-01-01）`, "") || "").trim();
          if (!v) return;
          if (/^\d{1,3}$/.test(v)) setIt(Number(v), null, `${v} 天`);
          else if (/^\d{4}-\d{2}-\d{2}$/.test(v)) setIt(null, v, `到 ${v}`);
          else log("没听懂：请填 45 这样的天数，或 2027-01-01 这样的日期", "warn");
        });
        line.appendChild(c);
        cdRow.appendChild(line);
      };
      if (role.cooldown && role.cooldown.until) renderActive(role.cooldown); else renderPicker();
      $(".status").appendChild(cdRow);
    }

    $(".x").onclick = pressed(() => { abortFill = true; host.remove(); });   // closing the panel also stops a fill

    // minimize / restore — collapse to just the header pill so it never covers the submit button
    const wrap = $(".wrap"), minBtn = $(".min");
    minBtn.onclick = pressed((e) => {
      e.stopPropagation();
      const collapsed = wrap.classList.toggle("collapsed");
      minBtn.textContent = collapsed ? "▢" : "–";
      minBtn.title = collapsed ? "restore" : "minimize";
    });

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
    const runFill = async () => {
      // Second click while filling = STOP. Sets the abort flag; humanPause throws it and the
      // whole fill unwinds to the finally below, leaving whatever's filled so far in place.
      if (fillBtn.dataset.filling === "1") {
        abortFill = true;
        fillBtn.textContent = "stopping…";
        return;
      }
      let fillOutcome = { status: "completed" };
      abortFill = false;
      fillBtn.dataset.filling = "1";
      fillBtn.classList.add("stop");
      fillBtn.textContent = "■ Stop";
      $(".log").textContent = "";
      const profile = role.profile = role.profile || {};
      profile._cards = (role.apply_profiles || {}).profiles;   // staleIdentityValue 用来认旧身份残值
      // 2026-09-15: the panel caches the role payload from INJECTION time. A tab opened while
      // the server was serving an empty profile (the stale-config incident) fills everything
      // EXCEPT identity fields — silently, because valueFor("") skips without a log line. So
      // a nameless card is refreshed from the live server at CLICK time, never filled from.
      if (!profile.first_name && !profile.email) {
        const fresh = await send({ type: "profile" });
        if (isContextDead(fresh)) {
          log("JobScan was reloaded — refresh this page (⌘R), then click Fill again.", "warn");
        } else if (fresh && fresh.ok !== false && fresh.profile
                   && (fresh.profile.first_name || fresh.profile.email)) {
          Object.assign(profile, fresh.profile);
          if (!(role.eeo && Object.keys(role.eeo).length) && fresh.eeo) role.eeo = fresh.eeo;
          log("profile was empty in this tab — refreshed from the server ✓", "good");
        } else {
          log("profile is EMPTY — name/email can't fill. Refresh this page (⌘R); if it "
              + "persists, restart the JobScan web server.", "warn");
        }
      }
      // commonAnswer reads these. A batch has nobody reading the form before Submit, and whether a
      // label means this employer ("Are you a former employee of Aurora?") needs the employer's name.
      profile.unattended = !!runnerDeadline;
      profile.company_name = role.company || "";
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
      if (IS_ASHBY) await fillAshbyButtons(profile, log, role); // Ashby Yes/No button-pair questions

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
          let ok = attachResume(b64ToBytes(res.b64), res.name);
          // Greenhouse shows the file's name once it has taken the upload. On a page opened cold the file was once
          // set before the form listened, and nothing went up (Vercel, 2026-11-08), so it is picked once more.
          if (ok && /(^|\.)greenhouse\.io$/.test(location.hostname)) {
            const uploaded = () => !!document.querySelector('[aria-labelledby="upload-label-resume"] .file-upload__filename');
            for (let t = 0; t < 20 && !uploaded(); t++) await sleep(200);
            if (!uploaded() && document.getElementById("resume")) {
              ok = attachResume(b64ToBytes(res.b64), res.name, document.getElementById("resume"));
              for (let t = 0; t < 20 && !uploaded(); t++) await sleep(200);
            }
            if (!uploaded()) { ok = false; log("Greenhouse did not take the résumé upload — attach it yourself", "warn"); }
          }
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
        fillOutcome = { status: m.includes(ABORT) ? "stopped" : "error", error: m };
        if (m.includes(ABORT)) log("⏹ stopped — the rest is yours.", "warn");
        else if (/context invalidated|Extension context/i.test(m)) log("JobScan was reloaded — refresh this page (⌘R), then click Fill again.", "warn");
        else log("fill error: " + m, "warn");
      } finally {
        fillBtn.dataset.filling = "";
        fillBtn.classList.remove("stop");
        fillBtn.textContent = FILL_LABEL;
      }
      return fillOutcome;
    };
    fillBtn.onclick = pressed(runFill);
    // In Chrome this lives in the isolated extension world, never the employer page world.
    // The fixture loads this exact source in its own local page to exercise the real filler.
    globalThis.JobScanRunFill = async () => {
      if (fillBtn.dataset.filling === "1") return {status:"busy"};
      return runFill();
    };
    const reviewBtn = $(".agent-review");
    const reviewOnlyBtn = $(".agent-review-only");
    const autoSubmitBtn = $(".agent-submit");
    let reviewRun = null, approved = null, stopPanel = null;
    let workflowBusy = false;
    // Nothing more to report about a refusal on this page: Filler's own click got that answer and it went out with the
    // result, the words were there before she did anything, or the watch has reported them.
    let refusalSettled = false;
    const closeSoon = () => setTimeout(() => send({ type: "closeTab" }), 1200);   // Done here: the record is saved, so the page can go.
    // The code an employer emails after Submit (Greenhouse) goes from the mailbox straight into its boxes. No model,
    // log or board message ever holds it. Mail can take a minute, so the mailbox is asked again for about 90 seconds.
    const enterEmailedCode = async (stopped) => {
      for (let tries = 0; tries < 9; tries++) {
        checkDeadline();
        const res = await send({ type: "emailCode", company: role.company });
        if (res?.ok && res.code) {
          if (!fillCode(res.code)) return {outcome:'blocked', evidence:'邮箱里的验证码找到了，但页面上没有能填的位置；请你自己填好再点提交。'};
          log("filled the emailed code ✓", "good");
          await sleep(600);
          return JobScanFormAgent.submitAfterCode();
        }
        await sleep(10000);
      }
      return {outcome:'blocked', evidence:`${stopped.evidence} 等了 90 秒，邮箱里没有 ${role.company} 发来的验证码；请你去邮箱看一眼，填好再点提交。`};
    };
    const runReview = async (fillFirst, autoSubmit = false, eventSink = null, runnerLease = null) => {
      if (!globalThis.JobScanFormAgent || !globalThis.JobScanWorkflow) { log("请重新加载扩展和页面。", "warn"); return; }
      if (workflowBusy || fillBtn.dataset.filling === "1") return;
      if (autoSubmit && (role.status !== "new" || role.no_role_match || role.top_role_default)) {
        log("只有明确匹配的待投新岗位可以自动提交。", "warn"); return;
      }
      const standing = role.shared_memory?.submission_policy?.authorization === 'standing';
      if (autoSubmit && !standing && !confirm(`仅授权这一份：${role.company} — ${role.title}\n${role.profile.first_name} ${role.profile.last_name} · ${role.profile.email}\n通过 Review 后自动提交；明确字段错误最多修正后重试一次；结果不明则停止。`)) return;
      workflowBusy = true;
      reviewBtn.disabled = reviewOnlyBtn.disabled = autoSubmitBtn.disabled = true;
      let clickAttempted = false;
      try {
        if (reviewRun) await send({type:"agentInvalidate",runId:reviewRun.id});
        approved = null; reviewRun = null;
        const request = async (type, snapshot, run) => {
          checkDeadline();
          const res = await send({type, body:{job_id:role.job_id, apply_profile:role.apply_profile||"", snapshot,
            run_id:run?.id||null, submit_authorized:autoSubmit,runner_lease:runnerLease}});
          checkDeadline();
          if (!res?.ok) throw new Error(res?.error || "协作服务无响应。");
          reviewRun = res.run; return res.run;
        };
        const result = await JobScanWorkflow.run({
          fill:async()=>{const out=fillFirst ? await runFill() : {status:"completed"};fillBtn.disabled=true;return out;},
          prepare:()=>JobScanFormAgent.prepareAttachments?.(),
          capture:()=>JobScanFormAgent.capture(),
          review:(s,r)=>request('agentReview',s,r), confirm:(s,r)=>request('agentConfirm',s,r),
          planRepair:async(snapshot,run)=>{
            checkDeadline();
            const res=await send({type:'agentRepair',body:{job_id:role.job_id,apply_profile:role.apply_profile||'',snapshot,run_id:run.id,runner_lease:runnerLease}});
            checkDeadline();
            if(!res?.ok)throw new Error(res?.error || 'Filler 修正服务无响应。');return res.run;
          },
          permit:(s,r)=>request('agentPermit',s,r),
          repair:async(s,fixes)=>{checkDeadline();const changes=JobScanFormAgent.patch(s,fixes);await sleep(600);return changes;},
          preflight:s=>{checkDeadline();return JobScanFormAgent.preflight(s);},
          submit:async s=>{
            checkDeadline();
            if(runnerLease){
              const active=await send({type:'runnerCheck',url:location.href,body:runnerLease});
              if(!active?.ok || active.run?.job_id!==role.job_id)throw new Error('执行任务已结束，未点击提交。');
            }
            checkDeadline();clickAttempted=true;
            const outcome=await JobScanFormAgent.submit(s);
            return outcome.outcome==='blocked' && runnerLease && role.mailcode_enabled &&
              globalThis.JobScanPageGate?.inspect()?.kind==='verification' ? enterEmailedCode(outcome) : outcome;
          },
          report:async(outcome,run)=>{
            const res=await send({type:'agentResult',url:location.href,body:{...outcome,run_id:run.id,attempt_id:run.attempt_id}});
            if(!res?.ok)throw new Error('提交回执未保存；不要重复投递。');reviewRun=res.run;return res.run;
          },
          record:async()=>{const res=await send({type:'markApplied',jobId:role.job_id,variant:role.resume_variant});
            if(!res?.ok)throw new Error('记账失败');$('.submitted').disabled=true;role.status='applied';},
          event:e=>{eventSink?.(e);log(`${e.role === 'filler' ? 'Filler' : 'Reviewer'}: ${e.text}`,e.issues?.length?'warn':'good');
            for(const issue of e.issues||[])log((e.snapshot?.fields.find(f=>f.id===issue.field_id)?.label||'页面')+' — '+issue.reason,'warn');}
        }, {autoSubmit});
        approved = result.status === 'review_passed' ? result.snapshot : null;
        const employerSaidNo = clickAttempted && /^雇主拒收/.test(result.reason || '');
        log(result.status === 'submitted' ? '✓ 已读到雇主成功回执。'+result.reason :
          result.status === 'review_passed' ? '✓ Review 通过，尚未提交。' :
          employerSaidNo ? `${EMPLOYER_SAID_NO}（${result.reason.replace(/^雇主拒收：/,'')}）` : '需要处理：'+result.reason,
          ['submitted','review_passed'].includes(result.status)?'good':'warn');
        if (employerSaidNo) refusalSettled = true;
        return {status:result.status,reason:result.reason || '',clicked:clickAttempted};
      } catch (error) {
        if (reviewRun) await send({type:"agentInvalidate",runId:reviewRun.id});
        const reason = String(error.message || error);
        log(reason, "warn");
        // The service refuses a permit when an earlier click is on file: this form may already be with the employer.
        const clicked = clickAttempted || /已提交、结果不明/.test(reason);
        return {status:clicked?'unknown':'blocked',reason,clicked};
      } finally {
        workflowBusy=false;reviewBtn.disabled=reviewOnlyBtn.disabled=autoSubmitBtn.disabled=fillBtn.disabled=false;
      }
    };
    reviewBtn.onclick = pressed(() => runReview(true));
    reviewOnlyBtn.onclick = pressed(() => runReview(false));
    autoSubmitBtn.onclick = pressed(async () => {
      const result = await runReview(true, true);
      // A receipt that did not get recorded keeps the page: the warning is there, and so is I submitted this.
      if (result?.status === 'submitted' && role.status === 'applied') closeSoon();
    });
    globalThis.JobScanRunnerJobId = role.job_id;
    globalThis.JobScanRunReview = async (expected) => {
      if (workflowBusy || fillBtn.dataset.filling === '1') return {status:'blocked',reason:'此页面已有填表任务。'};
      if (expected.apply_profile !== 'casey' || role.job_id !== expected.job_id) return {status:'blocked',reason:'岗位或名片与任务不符。'};
      if(!expected.lease || !Number.isFinite(expected.deadline) || expected.deadline<=Date.now() || expected.deadline>Date.now()+12*60*1000)
        return {status:'blocked',reason:'领取凭据或执行期限无效。'};
      const gate = globalThis.JobScanPageGate?.inspect();
      if (!gate || gate.kind !== 'ready') return {status:'blocked',reason:gate?.evidence || '页面检查组件尚未就绪。'};
      const fresh = await send({type:'runnerLookup',url:location.href});
      const current = fresh?.role;
      if (!fresh?.found || current.job_id !== expected.job_id || current.apply_profile !== 'casey' ||
          current.status !== 'new' || current.tailor_status !== 'ready' || !current.has_tailored_pdf ||
          current.cooldown || current.top_role_default || current.shared_memory?.submission_policy?.authorization !== 'standing')
        return {status:'blocked',reason:'岗位、简历、身份、冷却期或持续授权已改变，停止本份任务。'};
      role = current;
      const leased=await send({type:'runnerCheck',url:location.href,body:expected.lease});
      if(!leased?.ok || leased.run?.job_id!==role.job_id)return {status:'blocked',reason:'领取任务已失效。'};
      runnerDeadline=Math.min(expected.deadline,leased.run.expires_at*1000-30000);
      // The board shows the same findings the panel log does, field by field.
      const boardText=e=>[e.text,...(e.issues||[]).map(issue=>`· ${e.snapshot?.fields.find(f=>f.id===issue.field_id)?.label||'页面'}：${issue.reason}`+
        (issue.replacement==null?'':` → ${issue.replacement}`))].join('\n');
      try {
        checkDeadline();
        const result = await runReview(true,true,e=>send({type:'jobscanRunnerEvent',event:{role:e.role,text:boardText(e),to:e.to}}),expected.lease) ||
          {status:'blocked',reason:'填表审查入口拒绝启动。'};
        if (['needs_user','blocked','unknown'].includes(result.status) && !/^雇主拒收/.test(result.reason || '')) showStop(result);
        return result;
      } finally {runnerDeadline=0;}
    };
    // She may finish a form by hand at any time: after a stop, after a batch's gate held this tab back before any fill,
    // or after reloading a stopped tab. A requeue would then send it again unless the role stops being new, so her own
    // action followed by the employer's own receipt records it. A script on the page can write a receipt, but it cannot
    // fake her action, so a receipt already there at the start or at her first action proves nothing.
    const receiptShown = () => {
      try { return RECEIPT_SEEN.test(JobScanFormAgent.preflight(JobScanFormAgent.capture()).reason || ''); }
      catch { return false; }
    };
    const refusalOnPage = () => {
      try { return JobScanFormAgent.refusalShown() || ''; }
      catch { return ''; }
    };
    const watchHandSubmit = () => {
      endHandWatch?.();
      if (receiptShown()) return;
      // An SPA can move to another posting in this tab, and a receipt there is about that one.
      const posting = location.pathname.replace(/\/application.*$/, '');
      let acted = false, pressedSubmit = false, hinted = false, marking = false, failures = 0, refusalFailures = 0;
      // Refusal words are free text anywhere on a page. A filter, an FAQ or a follow-up question can show the same words after
      // any click, and acting on that closes a role and freezes a company when nothing was sent. So they answer only Submit.
      const pressesSubmit = event => {
        if (event.type === 'keydown') return event.key === 'Enter' && event.target?.tagName === 'INPUT';
        try { return JobScanFormAgent.candidates().includes(event.target?.closest?.('button,input[type=submit]')); }
        catch { return false; }
      };
      const act = event => {
        if (!event.isTrusted || event.target?.closest?.('[id^="jobscan-"]')) return;
        if (!acted && receiptShown()) { end(); return; }
        acted = true;
        if (pressesSubmit(event)) {
          // Refusal words already on the page came before this press, so they are not an answer to it.
          if (!pressedSubmit && refusalOnPage()) refusalSettled = true;
          pressedSubmit = true;
        }
      };
      document.addEventListener('click', act, true);
      document.addEventListener('keydown', act, true);
      const end = () => {
        clearInterval(timer);
        document.removeEventListener('click', act, true);
        document.removeEventListener('keydown', act, true);
        endHandWatch = null;
      };
      // Same trade as the receipt: a script faking these words costs one company a cooldown and sends nothing. The tab stays, so she can read it.
      const reportRefusal = async words => {
        // A pin says only that this page is on the same site, so the words may be about another posting. She can read them; the role stays as it is.
        if (role.from_pin || role.pin_override) {
          refusalSettled = true;
          const text = '这一页写着雇主不收，但这份岗位是靠你点开的链接认出来的，不一定是这个岗位，所以没有记。是的话，请在右上角面板的「不让投了？冷却」那里选天数。';
          log(text,'warn'); stopPanel?.note(text);
          return;
        }
        marking = true;
        const res = await send({type:'handRefusal',jobId:role.job_id,evidence:words,url:location.href});
        marking = false;
        if (res?.ok) {
          refusalSettled = true;
          log(EMPLOYER_SAID_NO,'warn'); stopPanel?.refused(EMPLOYER_SAID_NO);
        } else if (++refusalFailures >= 5) {
          refusalSettled = true;
          const text = '雇主说不收这份，但没记上：请在右上角面板的「不让投了？冷却」那里选天数。';
          log(text,'warn'); stopPanel?.note(text);
        }
      };
      const timer = setInterval(async () => {
        // During a workflow its own record step speaks for the receipt.
        if (marking || workflowBusy) return;
        if (role.status !== 'new' || !location.pathname.startsWith(posting)) return end();
        if (!acted) return;
        if (!receiptShown()) {
          const words = refusalSettled ? '' : refusalOnPage();
          if (words && pressedSubmit) await reportRefusal(words);
          else if (words && !hinted) {
            hinted = true;
            log('这一页写着像雇主不收的话，但你还没点提交，所以什么都没记。真是这样的话，请在右上角面板的「不让投了？冷却」那里选天数。','warn');
          }
          return;
        }
        marking = true;
        const res = await send({type:'markApplied',jobId:role.job_id,variant:role.resume_variant});
        marking = false;
        if (res?.ok) {
          end(); role.status = 'applied'; $('.submitted').disabled = true;
          const text = '读到了雇主的成功回执，已自动记为已投。不要再提交，这一页马上自动关掉。';
          log('✓ '+text,'good'); stopPanel?.applied(text);
          closeSoon();
        } else if (++failures >= 5) {
          end();
          const text = '读到了雇主的成功回执，但没记上：请点「I submitted this」，否则重跑时会再投一次。';
          log(text,'warn'); stopPanel?.note(text);
        }
      },1000);
      endHandWatch = end;
    };
    // The tab stays open after a stop, so the page itself says what is missing and takes the reply.
    const showStop = result => {
      try {
        const fields = JobScanFormAgent.capture().fields;
        const open = (reviewRun?.result?.issues || []).filter(issue => !issue.auto_fix);
        const {items, pageIssues} = globalThis.JobScanStopPanel.rowsFrom({issues: open, fields, jobId: role.job_id});
        // After a click this page asking for a code is the employer's next step, not a possible second send.
        const codeStep = !!result.clicked && globalThis.JobScanPageGate?.inspect()?.kind === 'verification';
        stopPanel = globalThis.JobScanStopPanel.show({company:`${role.company} · ${role.title}`, reason:result.reason, items, pageIssues,
                                                      clicked:!!result.clicked, verification:codeStep, mailcode:!!role.mailcode_enabled,
                                                      elementsFor:id => JobScanFormAgent.elementsFor(id), send});
      } catch (error) { log('停止面板没能打开：'+String(error.message || error),'warn'); }
    };
    const staleTimer = setInterval(() => {
      if (!host.isConnected) { clearInterval(staleTimer); return; }
      if (!approved || !globalThis.JobScanFormAgent) return;
      if (JobScanFormAgent.fingerprint(JobScanFormAgent.capture()) !== JobScanFormAgent.fingerprint(approved)) {
        approved = null; log("表单改变，原 Review 已失效。请重新检查。", "warn");
        send({type:"agentInvalidate",runId:reviewRun.id});
      }
    },1000);

    // Email verification-code button — only when mailcode is configured (app password present).
    if (role.mailcode_enabled) {
      const codeBtn = $(".code");
      codeBtn.hidden = false;
      codeBtn.onclick = pressed(async () => {
        // Only this company's own code. Anyone else's stays in her mailbox: it has no business on an
        // employer's page, and with no company named the service would hand back the newest from anyone.
        const mailbox = () => log("没读到这家公司发来的验证码。可能还没到，也可能发件人对不上：请自己去邮箱看。别的发件人的验证码不会显示在这里。", "warn");
        if (!role.company) { mailbox(); return; }
        codeBtn.disabled = true; const orig = codeBtn.textContent; codeBtn.textContent = "📧 checking…";
        const res = await send({ type: "emailCode", company: role.company });
        codeBtn.disabled = false; codeBtn.textContent = orig;
        if (!res || !res.ok) { log("email-code failed: " + (res && res.error), "warn"); return; }
        if (!res.code) { mailbox(); return; }
        const ok = fillCode(res.code);
        log(ok ? `filled the code from ${role.company} ✓` : `code is ${res.code} — no code field found, paste it`, ok ? "good" : "warn");
      });
    }


    $(".submitted").onclick = pressed(async () => {
      const res = await send({ type: "markApplied", jobId: role.job_id, variant: role.resume_variant });
      if (res && res.ok) {
        log("marked applied in JobScan ✓ (" + role.resume_variant + ")", "good"); $(".submitted").disabled = true;
        role.status = "applied";
        closeSoon();
      }
      else if (isContextDead(res)) log("JobScan was reloaded — refresh this page (⌘R), then click ✓ again.", "warn");
      else log("mark-applied failed: " + (res && res.error), "warn");
    });
    // A role the page does not identify (a guess at the company's top role, or no role at all) is not hers to mark from here.
    if (role.status === "new" && role.job_id && !role.no_role_match && !role.top_role_default) watchHandSubmit();
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
    c.onclick = pressed((ev) => { ev.preventDefault(); ev.stopPropagation(); sparkleDraft(el, c); });
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
      if (!res) { console.warn("[JobScan] no response — is JobScan running? (pick a workspace in the extension popup)"); return; }
      // Owner 2026-09-23: no workspace chosen in this Chrome profile → say so and fill NOTHING.
      if (res.ok === false && res.error === "NO_WORKSPACE") {
        if (!document.getElementById("jobscan-ws-gate")) {
          const g = document.createElement("div");
          g.id = "jobscan-ws-gate";
          g.style.cssText = "position:fixed;top:12px;right:12px;z-index:2147483647;max-width:320px;padding:10px 12px;border-radius:10px;background:#12314f;color:#fff;font:13px/1.45 -apple-system,Segoe UI,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.25)";
          g.innerHTML = "<b>JobScan：还没选工作台</b><div style='margin-top:4px;opacity:.9'>点浏览器右上角的 JobScan 图标 → 选这个 Chrome profile 是谁的资料 → 刷新这页。选之前不会填任何内容。</div>";
          document.documentElement.appendChild(g);
        }
        return;
      }
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
    const shadow = host.attachShadow({ mode: "closed" });
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
    cap.onclick = pressed(async () => {
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
    });
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
