// Shared by the real extension and the local fixture. Reads the DOM, never filler logs.
(() => {
  if (globalThis.JobScanFormAgent) return;
  const documentId = crypto.randomUUID();
  const refs = new Map();
  const ids = new WeakMap();
  const attachmentHashes = new WeakMap();
  let seq = 0, submitClicked = false;
  const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  // Ashby's "autofill from resume" uploader is a parsing helper, not an answer on the form. Greenhouse puts a hidden twin
  // input beside each dropdown only so the browser requires it; the dropdown itself is read below.
  const excluded = el => el.closest('[id^="jobscan-"],.ashby-application-form-autofill-input-root') ||
    /requiredInput/.test(String(el.className || '')) ||
    /password|one.time|verification.code|otp|captcha|token/i.test([el.type, el.name, el.id, el.autocomplete].join(' '));
  // An invisible reCAPTCHA only scores the visit and never asks anything (Ashby, Greenhouse). A challenge loads another frame.
  const quietCaptcha = el => /[?&]size=invisible\b/.test(el.src || '');
  const clean = text => String(text || '').replace(/\s+/g, ' ').trim();
  // By code point: half an emoji is a lone surrogate, which the local service rejects outright.
  const cut = (text, max) => [...String(text || '')].slice(0, max).join('');
  // A box asking for an emailed code, as page_gate.js reads one. A code must never reach the Reviewer model. Once the page says
  // a code was sent, these boxes stay out of the snapshot. Before that a label is all there is, and "Enter your code sample URL"
  // can be a real question, so the box stays where the Reviewer can see it and only what it holds is hidden.
  const CODE_BOX = /\b(?:verification|security|one ?time|authentication|confirmation|email|access)\s*code\b|\bpasscode\b|\bone ?time (?:pass\w*|pin)\b|\botp\b|\bsecurity input\b|\b\d+ ?(?:digit|character|char) code\b|^(?:please )?(?:enter|input|type)\s+(?:(?:in|the|your)\s+)*(?:\d+ (?:digit|character|char)\s+)?code(?:\s+(?:we|you)\b.*)?\W*$|^code\W*$|验证码/i;
  const CODE_SENT = /\b(?:sent|emailed)\b[^.!?]{0,160}\b(?:code|otp)\b|\b(?:code|otp)\b[^.!?]{0,160}\b(?:sent|emailed)\b|验证码[^。！？]{0,80}(?:已发送|已发至)|(?:已发送|已发至)[^。！？]{0,80}验证码/i;
  // codeField also takes these loose words. "Code of conduct" and an export question about "source code" hold them too, and
  // the Reviewer has to read those, so a box like this stays in the snapshot and only a value shaped like a code is hidden.
  const CODE_WORD = /verification|one[\s-]?time|verify|\bcode\b/i;
  const NOT_A_CODE = /\b(?:zip|postal|area|country|dial|employee|promo|referral|discount|coupon)\b/i;
  const CODE_SHAPED = value => /^[A-Za-z0-9-]{4,12}$/.test(value) && (/\d/.test(value) || value === value.toUpperCase());
  const HIDDEN_CODE = '（已隐藏，可能是验证码）';
  // What fillCode wrote. A rebuilt box is a new element, so the code itself is remembered as well.
  const codeFields = new WeakSet(), codesWritten = new Set();
  function codeWritten(code, elements) {
    if (code) codesWritten.add(String(code).trim());
    for (const el of elements || []) codeFields.add(el);
  }
  const isAshby = () => location.hostname === 'jobs.ashbyhq.com';
  const isGreenhouse = () => /(^|\.)greenhouse\.io$/.test(location.hostname);
  // Ashby has no <form>: each question is a "field entry" holding its label and control(s).
  const ashbyEntry = el => isAshby() ? el.closest('[class*="_fieldEntry"],.ashby-application-form-field-entry') : null;
  const entryLabel = entry => entry?.querySelector('label');
  const entryRequired = entry => /(?:^|\s)_required/.test(entryLabel(entry)?.className || '');
  const attachedToForm = el => el.type === 'file' && (el.form || ashbyEntry(el)) && el.files?.length > 0;
  // Greenhouse uploads a file the moment it is picked and then removes its input, leaving only the name on the page.
  // So the bytes Filler picks are hashed then, and a name on the page counts only if it is one Filler picked.
  const picked = new Map(), hashing = [];
  function rememberPicked(file) {
    hashing.push(file.arrayBuffer().then(bytes => crypto.subtle.digest('SHA-256', bytes)).then(hash => {
      picked.set(file.name, {size: file.size, sha256: [...new Uint8Array(hash)].map(x => x.toString(16).padStart(2, '0')).join('')});
    }).catch(() => {}));
  }
  async function prepareAttachments() {
    // Only when Filler picked something: an await here otherwise reads the inputs a tick later than before.
    if (hashing.length) await Promise.all(hashing.splice(0));
    for (const el of document.querySelectorAll('input[type=file]')) {
      if (excluded(el) || (!visible(el) && !attachedToForm(el))) continue;
      for (const file of el.files || []) {
        if (attachmentHashes.has(file)) continue;
        try {
          const hash = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
          attachmentHashes.set(file, [...new Uint8Array(hash)].map(x => x.toString(16).padStart(2, '0')).join(''));
        } catch {
          // Missing byte evidence remains a hard review failure. A replacement File
          // is a different WeakMap key and must be prepared again before approval.
        }
      }
    }
  }
  function label(el) {
    const aria = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map(id => document.getElementById(id)?.textContent || '').join(' ');
    const legend = el.closest('fieldset')?.querySelector('legend')?.textContent;
    if (el.type === 'radio' && legend) return clean(legend);
    // Greenhouse's uploads are labelled only "Attach"; their ids say which is which.
    const upload = el.type === 'file' && isGreenhouse() && {resume: 'Resume/CV', cover_letter: 'Cover Letter'}[el.id];
    if (upload) return upload;
    // A Greenhouse checkbox says only "Accept"; what is being accepted is the group's legend.
    if (el.type === 'checkbox' && legend && isGreenhouse()) return clean(`${legend} — ${[...(el.labels || [])].map(x => x.textContent).join(' ')}`);
    return clean(aria || el.getAttribute('aria-label') || [...(el.labels || [])].map(x => x.textContent).join(' ') || legend ||
      entryLabel(ashbyEntry(el))?.textContent || el.name || el.id);
  }
  // A radio's own label is an answer, not the question (e.g. "Yes, no sponsorship").
  function radioQuestion(el) {
    const legend = el.closest('fieldset')?.querySelector('legend');
    if (legend) return clean(legend.textContent);
    const question = entryLabel(ashbyEntry(el));
    if (question) return clean(question.textContent);
    for (let box = el.parentElement, depth = 0; box && depth < 7; box = box.parentElement, depth++) {
      const radios = [...box.querySelectorAll('input[type=radio]')].filter(r => r.name === el.name);
      if (radios.length < 2) continue;
      const aria = box.getAttribute('aria-labelledby');
      if (aria) return clean(aria.split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' '));
      const prompt = [...box.querySelectorAll('label,legend,[class*=label i]')].find(l =>
        !l.closest('label')?.querySelector('input[type=radio]') &&
        !radios.some(r => [...r.labels].includes(l)) && !l.querySelector('input[type=radio]'));
      if (prompt && clean(prompt.textContent)) return clean(prompt.textContent);
      return ''; // Do not turn an option into a question by climbing into unrelated groups.
    }
    return '';
  }
  // The words a control or its label says for itself, and a typeahead's open list or an error that comes and goes.
  const OWN_WORDS = 'label,button,select,option,[role=listbox],[role=option],[role=alert],[class*=error i]';
  // What an entry says besides its title and controls, e.g. the statement under a bare "Acknowledgement". The Reviewer has
  // to read it, and a confirmed answer has to cover it (app/confirmed_answers.py), whatever kind of control sits below it.
  function entryDescription(entry, options = []) {
    const title = entryLabel(entry);
    const heading = clean(title?.textContent);
    let rest = clean(entry.innerText || entry.textContent);
    if (heading && rest.startsWith(heading)) rest = rest.slice(heading.length);
    const own = [...entry.querySelectorAll(OWN_WORDS)].filter(el => el !== title).map(el => clean(el.innerText || el.textContent));
    // From the end: the controls come after the statement, and the statement can use the same words.
    for (const words of new Set([...own, ...options].filter(Boolean))) {
      const at = rest.lastIndexOf(words);
      if (at >= 0) rest = rest.slice(0, at) + ' ' + rest.slice(at + words.length);
    }
    return cut(clean(rest), 3000);
  }
  // The same text as capture sends, for Filler's confirmed answers. An upload box's text is its own chrome.
  function statementOf(el) {
    const entry = ashbyEntry(el);
    if (!entry || el.type === 'file') return '';
    return entryDescription(entry, el.tagName === 'SELECT' ? [...el.options].map(x => clean(x.textContent)) : []);
  }
  // A required question asked as several checkboxes. No single box is required (see capture), so
  // nothing else notices an empty one until the employer rejects the click.
  function emptyRequiredChoice(entry) {
    const controls = [...entry.querySelectorAll('input,select,textarea')].filter(c => c.type !== 'hidden' && !excluded(c));
    return entryRequired(entry) && controls.length > 1 && controls.every(c => c.type === 'checkbox') && !controls.some(c => c.checked);
  }
  const typedBox = el => el.tagName === 'INPUT' && /^(?:text|number|tel|search|)$/.test(el.type || '');
  const codeBoxWords = el => [label(el), el.getAttribute('aria-label'), ...[...(el.labels || [])].map(x => x.textContent), el.name, el.id,
    el.getAttribute('placeholder')].filter(Boolean).map(text => clean(text).replace(/[_-]+/g, ' '));
  // What fillCode wrote, or that same code pasted anywhere else. This goes for good, whatever the box is called.
  const holdsWrittenCode = el => codeFields.has(el) || (String(el.value ?? '').trim() && codesWritten.has(String(el.value).trim()));
  const codeBox = el => typedBox(el) && (el.getAttribute('autocomplete') === 'one-time-code' || codeBoxWords(el).some(text => CODE_BOX.test(text) && !NOT_A_CODE.test(text)));
  const codeSentShown = () => [...document.querySelectorAll('p,h1,h2,h3,label,[role=alert],[role=status]')].some(el => visible(el) && CODE_SENT.test(clean(el.textContent)));
  // A code typed by hand into a box only the loose words point at. With a code on its way, a word in such a box is one too.
  const looksLikeCode = (el, codeSent) => {
    const value = String(el.value ?? '').trim();
    return typedBox(el) && (CODE_SHAPED(value) || (codeSent && /^[A-Za-z]{4,12}$/.test(value))) && codeBoxWords(el).some(text => CODE_WORD.test(text) && !NOT_A_CODE.test(text));
  };
  // An error like "The code 481516 is incorrect" repeats the code, and errors go to the Reviewer.
  function withoutCodes(text) {
    let out = text;
    for (const code of codesWritten) out = out.split(code).join(HIDDEN_CODE);
    return out.replace(/\b(?=[A-Z0-9-]*\d)[A-Z0-9-]{4,12}\b|\b\d[\d -]{2,}\d\b/g, HIDDEN_CODE);
  }
  function capture() {
    const fields = [], attachments = [], radioGroups = new Set(), yesNoGroups = new Set(), choiceEntries = new Set();
    let partial = false, codeBoxShown = false;
    refs.clear();
    const controls = document.querySelectorAll('input,textarea,select,[role=combobox],[role=checkbox],[role=radio],[role=switch],button[aria-pressed],[contenteditable=true]');
    // One box per digit, however each is labelled. A lone maxlength="1" box is something else.
    const split = [...document.querySelectorAll('input[maxlength="1"],input[autocomplete="one-time-code"]')];
    const digitBoxes = new Set(split.length >= 4 && split.length <= 8 ? split : []);
    const codeSent = codeSentShown();
    for (const el of controls) {
      const asksCode = digitBoxes.has(el) || codeBox(el);
      if (asksCode) codeBoxShown = true;
      if (excluded(el) || holdsWrittenCode(el) || (asksCode && codeSent) || (!visible(el) && !attachedToForm(el)) || (/^(hidden|submit|button|reset|image)$/.test(el.type) && !el.hasAttribute('aria-pressed'))) continue;
      if (!ids.has(el)) ids.set(el, 'f-' + (++seq));
      const id = ids.get(el);
      // Ashby asks Yes/No with two pressed-state buttons: one question, one readable answer.
      const entry = ashbyEntry(el);
      if (entry && el.tagName === 'BUTTON' && entry.querySelector('.ashby-application-form-input-yesno-option')) {
        if (yesNoGroups.has(entry)) continue;
        yesNoGroups.add(entry);
        const buttons = [...entry.querySelectorAll('button[aria-pressed]')].filter(visible);
        const options = buttons.map(b => clean(b.textContent));
        if (options.join() !== 'Yes,No') partial = true;
        const title = clean(entryLabel(entry)?.textContent), description = entryDescription(entry, options);
        fields.push({id, label: cut(title || '未识别题目', 3000), kind: 'radio',
          value: clean(buttons.find(b => b.getAttribute('aria-pressed') === 'true')?.textContent),
          required: entryRequired(entry), readable: options.join() === 'Yes,No', options, error: '',
          ...(description ? {description} : {})});
        refs.set(id, buttons);
        continue;
      }
      // Greenhouse's react-select: its input only takes typing, and the pick shows beside it.
      const select = isGreenhouse() && el.getAttribute('role') === 'combobox' ? el.closest('.select__control') : null;
      if (select) {
        const picks = [...select.querySelectorAll('.select__single-value,.select__multi-value__label')].map(x => clean(x.textContent));
        const question = label(el);
        if (!question) partial = true;
        fields.push({id, label: cut(question || '未识别题目', 3000), kind: 'combobox', value: picks.join(', '),
          required: el.required || el.getAttribute('aria-required') === 'true', readable: true, options: [],
          error: el.getAttribute('aria-invalid') === 'true' ? '网页标记此字段无效。' : ''});
        refs.set(id, [el]);
        continue;
      }
      if (entry && el.type === 'checkbox') choiceEntries.add(entry);
      let kind = (el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : el.type || 'custom');
      let value = String(el.value ?? '');
      let options = [], elements = [el], readable = true;
      if (el.type === 'radio') {
        if (!el.name) { readable = false; }
        else {
          const groupKey = el.name + ':' + (el.form?.id || '');
          if (radioGroups.has(groupKey)) continue;
          radioGroups.add(groupKey);
          elements = [...document.querySelectorAll('input[type=radio]')].filter(x => x.name === el.name && x.form === el.form && visible(x));
          options = elements.map(x => clean([...x.labels].map(l => l.textContent).join(' ')) || x.value);
          value = options[elements.findIndex(x => x.checked)] || '';
        }
      } else if (el.type === 'checkbox') { value = el.checked ? 'Yes' : 'No'; }
      else if (el.tagName === 'SELECT') {
        options = [...el.options].filter(x => x.value !== '').map(x => clean(x.textContent));
        value = el.value === '' ? '' : [...el.selectedOptions].map(x => clean(x.textContent)).join(', ');
      } else if (el.type === 'file') {
        value = [...(el.files || [])].map(x => x.name).join(', ');
        for (const file of el.files || []) attachments.push({field_id:id, name:file.name,
          size:file.size, sha256:attachmentHashes.get(file) || ''});
      }
      // An Ashby typeahead shows its committed choice as the input's own text.
      if (entry && el.tagName === 'INPUT' && el.getAttribute('role') === 'combobox') {
        kind = 'combobox'; value = clean(el.value);
      } else if (el.hasAttribute('role') || el.hasAttribute('contenteditable') || el.hasAttribute('aria-pressed') ||
          el.getAttribute('aria-autocomplete') || el.closest('[role=combobox]')) {
        readable = false; kind = 'custom'; value = clean(el.value || el.textContent);
      }
      const question = el.type === 'radio' ? radioQuestion(el) : label(el);
      if (!question || !readable) partial = true;
      const err = el.getAttribute('aria-invalid') === 'true' ? '网页标记此字段无效。' :
        (el.validity && !el.validity.valid ? '网页校验未通过：' + el.validationMessage : '');
      if (looksLikeCode(el, codeSent) || (asksCode && value.trim())) value = HIDDEN_CODE;
      const description = entry && el.type !== 'file' ? entryDescription(entry, options) : '';
      fields.push({id, label: cut(question || '未识别题目', 3000), kind, value, ...(description ? {description} : {}),
                   // A checkbox inside a multi-part question (Education: "Still student?") is not itself required.
                   required: elements.some(x => x.required || x.getAttribute('aria-required') === 'true') ||
                     !!(entry && entryRequired(entry) && !(el.type === 'checkbox' && entry.querySelectorAll('input,select,textarea').length > 1)),
                   readable, options, error: err});
      refs.set(id, elements);
    }
    // A Greenhouse upload already sent: its input is gone and the group shows the file's name.
    for (const group of isGreenhouse() ? document.querySelectorAll('.file-upload[role=group]') : []) {
      const shown = group.querySelector('.file-upload__filename p');
      if (!shown || group.querySelector('input[type=file]') || !visible(group)) continue;
      if (!ids.has(group)) ids.set(group, 'f-' + (++seq));
      const id = ids.get(group), name = clean(shown.textContent), known = picked.get(name);
      const title = clean(document.getElementById(group.getAttribute('aria-labelledby') || '')?.textContent).replace(/\*$/, '');
      fields.push({id, label: cut(title || '未识别题目', 3000), kind: 'file', value: name,
                   required: group.getAttribute('aria-required') === 'true', readable: true, options: [], error: ''});
      attachments.push({field_id: id, name, size: known?.size || 0, sha256: known?.sha256 || ''});
      refs.set(id, []);
    }
    if ([...document.querySelectorAll('iframe')].some(el => visible(el) && !quietCaptcha(el))) partial = true;
    if ([...document.querySelectorAll('*')].some(el => el.shadowRoot && visible(el) && !excluded(el))) partial = true;
    const empty = [...choiceEntries].filter(emptyRequiredChoice)
      .map(entry => `必填多选题「${cut(clean(entryLabel(entry)?.textContent), 200)}」一项都没选，需要你本人选。`);
    const alerts = [...document.querySelectorAll('[role=alert]')].filter(el => visible(el) && !excluded(el))
      .map(el => clean(el.textContent)).filter(Boolean).map(text => (codeSent || codeBoxShown || codesWritten.size ? withoutCodes(text) : text));
    const errors = [...empty, ...alerts].slice(0, 100);
    return {url: location.href, document_id: documentId, fields, attachments, coverage: partial ? 'partial' : 'complete', errors};
  }
  function fingerprint(snapshot) { return JSON.stringify(snapshot); }
  function patch(snapshot, issues) {
    if (fingerprint(capture()) !== fingerprint(snapshot)) throw new Error('页面已改变，请重新检查后再修正。');
    const changes = [];
    for (const issue of issues) {
      if (!issue.auto_fix || issue.replacement == null) continue;
      const field = snapshot.fields.find(x => x.id === issue.field_id);
      const elements = refs.get(issue.field_id);
      if (!field || !elements?.length || !field.readable) throw new Error('控件已失效。');
      const el = elements[0], value = String(issue.replacement);
      if (el.disabled || el.readOnly) throw new Error('字段不可修改。');
      if (field.kind === 'radio') {
        const index = field.options.indexOf(value);
        if (index < 0) throw new Error('选项已改变。');
        elements[index].click();
      } else if (field.kind === 'select') {
        const option = [...el.options].find(x => clean(x.textContent) === value);
        if (!option) throw new Error('选项已改变。');
        el.value = option.value;
      } else if (['text','email','tel','number','textarea'].includes(field.kind)) {
        const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
      } else throw new Error('此控件需要人工修正。');
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      changes.push({field_id: field.id, label: field.label, before: field.value, after: value});
    }
    const after = capture();
    for (const change of changes) {
      if (after.fields.find(f => f.id === change.field_id)?.value !== change.after) throw new Error('控件没有保存新值。');
    }
    return changes;
  }
  const submitLabel = el => clean(el.innerText || el.value || el.getAttribute('aria-label'));
  function candidates() {
    return [...document.querySelectorAll('button,input[type=submit]')].filter(el => visible(el) && !excluded(el) &&
      /^(submit( application)?|send application|apply now)$/i.test(submitLabel(el)) && (el.form || isAshby()));
  }
  function successEvidence() {
    const selectors = location.pathname === '/agent-lab/fixture' ? ['[data-application-success]'] :
      isAshby() ? ['h2', '.ashby-application-form-success-container'] :
      // job-boards shows its receipt on a /confirmation page, which also opens on its own with no application behind it.
      // So it counts only once our click has moved the form there.
      isGreenhouse() ? ['#application_confirmation', ...(submitClicked && /\/confirmation\/?$/.test(location.pathname) ? ['.confirmation'] : [])] :
      /(^|\.)lever\.co$/.test(location.hostname) ? ['.application-confirmation'] : [];
    // innerText keeps blocks apart: textContent runs <h2>Success</h2><p>Thank you…</p> together as "SuccessThank you".
    const read = el => clean(el.innerText || el.textContent);
    for (const selector of selectors) for (const el of document.querySelectorAll(selector)) {
      if (!visible(el)) continue;
      const ashbySuccess = el.matches('h2') && /^success$/i.test(clean(el.textContent));
      const text = ashbySuccess ? read(el.parentElement) : read(el);
      if (/\bapplication (?:has been |was )?(?:successfully )?(?:submitted|received)\b|\bthank you for applying\b/i.test(text)) return cut(text, 1500);
      // An employer can write its own thanks under Ashby's Success heading: Nestmed's "Thanks for your application to
      // Nestmed. It has been recieved". After our click, that heading with any thanks or receipt under it is the receipt.
      if (ashbySuccess && submitClicked && /\bthanks?\b|\bre(?:ce|ci)eved\b/i.test(text) &&
          !/\bnot\b|n['’]t\b|\bfail|\berror\b|\bunable\b|\bproblem\b/i.test(text)) return cut(text, 1500);
    }
    return '';
  }
  // "We receive too many applications to respond to everyone" is about the employer's inbox, not about her.
  const LIMIT_WORDS = /application limit (?:reached|exceeded)|too many applications(?! to\b)|possible spam/i;
  const PERIOD = String.raw`\b(?:\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|few|several|an?)[\s-]*(?:day|week|month|year)s?\b`;
  const MONTH = String.raw`(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?`;
  const DATE = String.raw`(?:\b${MONTH}\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+20\d\d)?\b|\b${MONTH}\s+20\d\d\b|\b20\d\d-\d\d-\d\d\b|\b\d{1,2}/\d{1,2}/20\d\d\b|\b20\d\d\b)`;
  const COOL = String.raw`(?:cool[- ]?down|cooling[- ]off)`, APPLYING = String.raw`(?:appl(?:y|ying|ied|ications?|icants?)|reappl\w+|candida\w+)`;
  // What she herself is told. A job ad can say "hiring freeze" or "applications that freeze", and "Please wait" is only a spinner.
  const REFUSED_HER = new RegExp([
    // "You've already submitted your application" is a thank-you as often as a no, so it does not count here.
    String.raw`\byou(?:['’]ve| have)?\s+(?:\w+\s+)?(?:recently|already)\s+(?:applied|submitted(?!\s+your\b))\b`,
    String.raw`\byou(?:['’]ve| have)?\s+(?:\w+\s+)?applied\b[^.!?]{0,60}\b(?:recently|already)\b`,
    String.raw`\byou\s+(?:have\s+)?submitted\s+an?\s+application\b[^.!?]{0,40}\brecently\b`,
    String.raw`\bwe\s+already\s+have\s+(?:an?|your)\s+application\b`,
    String.raw`^\W*(?:already|recently)\s+applied\b`,
    String.raw`^\W*(?:application|candidacy)\s+frozen\b`,
    String.raw`\b(?:your|this|job|the)\s+(?:application|candidacy|profile|account)\b[^.!?]{0,40}\b(?:is|are|was|were|been|remains?)\s+(?:\w+\s+)?frozen\b`,
    String.raw`\bapplications?\b[^.!?]{0,30}\b(?:is|are)\s+(?:\w+\s+)?frozen\b`,
    String.raw`\b(?:application|applicant|candidate)s?\s+freeze\b|\bfreeze\s+(?:on|for)\s+(?:\w+\s+)?(?:applications?|candidac\w+)\b`,
    `\\b${APPLYING}\\b[^.!?]{0,60}\\b${COOL}\\b|\\b${COOL}\\b[^.!?]{0,60}(?:\\b${APPLYING}\\b|${PERIOD})`,
    `${PERIOD}[\\s-]+${COOL}\\b|\\byou(?:r|['’]re| are)?\\b[^.!?]{0,40}\\b${COOL}\\b`,
  ].join('|'), 'i');
  // The weaker wording: she may not apply again. "You may reapply in 6 months" and "please wait 5 days" are not here: a
  // confirmation page says those too, and calling a real send a refusal closes the role and freezes the company.
  // A calendar date is about her, where "in 6 months" can be a policy a receipt quotes.
  const STAY_AWAY = new RegExp([
    String.raw`\bwait\b(?![^.!?]{0,20}\b(?:seconds?|minutes?|hours?)\b)[^.!?]{0,40}\b(?:before|to|until)\s+(?:re-?)?appl(?:y|ying)\b`,
    `\\b(?:must|need to|have to|required to)\\s+wait\\s+(?:at least\\s+|another\\s+|for\\s+)?${PERIOD}(?![^.!?]*\\bbefore\\s+(?!(?:re-?)?appl))`,
    String.raw`\b(?:cannot|can['’]?t|may not|must not|unable to|not (?:yet )?(?:able|eligible|allowed) to)\s+(?:re-?apply|apply again)\b`,
    `\\b(?:re-?apply(?:ing)?|appl(?:y|ying) again)\\b[^.!?]{0,40}\\b(?:on|after|until|before)\\s+[^.!?]{0,20}?${DATE}`,
  ].join('|'), 'i');
  // A page that thanks her or says it has her application may well be a receipt, so the weaker wording cannot call it a refusal.
  // "Thanks for your interest" is what a no says, and it is no receipt.
  const THANKED = /\bthank(?:s| you)\b(?! for your interest)|\bwe(?:['’]ve| have)?\s+(?:got|received)\b|\bapplication (?:has been |was |is )?(?:successfully )?(?:submitted|received|complete)\b|\byour application\b|\ball (?:set|done)\b|\bbe in touch\b/i;
  // "If you have already applied" and "Applicants may reapply after 12 months" say nothing about her, wherever in the sentence that word falls.
  const NOT_ABOUT_HER = /\b(?:if|whether|unless|once|when|should|in case|candidates?|applicants?|anyone|those|people|individuals)\b/i;
  // "Have you recently applied", "Jobs you already applied to", "Since you have already applied, tell us which role".
  const ASKING_ABOUT_IT = /\b(?:have|has|did|do|does|jobs?|roles?|positions?|since)\s*$/i;
  // Where the wording that turns her away starts in this text, or -1.
  function turnedAwayAt(text, thanked) {
    for (const found of clean(text).matchAll(/[^.!?]+[.!?]*/g)) {
      const sentence = found[0];
      const said = sentence.match(REFUSED_HER) || (thanked ? null : sentence.match(STAY_AWAY));
      if (said && !sentence.trimEnd().endsWith('?') && !NOT_ABOUT_HER.test(sentence) && !ASKING_ABOUT_IT.test(sentence.slice(0, said.index))) return found.index;
    }
    return -1;
  }
  // A line of text kept as lines: the service reads a period differently on the refusal's own line and on the ones after it.
  const asLines = text => String(text || '').split(/\n+/).map(clean).filter(Boolean).join('\n');
  // The heading and the block it heads. Inside a form's own container that block is the form, and its labels are no refusal.
  function headingBlock(heading) {
    const box = heading.parentElement, formControls = 'input,select,textarea';
    if (box && !box.querySelector?.(formControls)) return asLines(box.innerText || heading.textContent);
    const next = heading.nextElementSibling;
    return asLines(`${heading.textContent}\n${next && !next.querySelector?.(formControls) ? next.innerText || next.textContent : ''}`);
  }
  // An employer saying no in its own words is an answer, not an unclear result.
  function employerRefusal() {
    const thanked = THANKED.test(clean(document.body?.innerText));
    const heading = [...document.querySelectorAll('h1,h2,h3')].find(el => visible(el) &&
      (/\b(?:couldn.?t|could not|can.?t|cannot|unable to) submit your application\b/i.test(clean(el.textContent)) || turnedAwayAt(el.textContent, thanked) >= 0));
    return heading ? cut(headingBlock(heading), 1500) : '';
  }
  // A line after the refusal that says how long to stay away. "Similar role posted 2 days ago" and "3 days a week in office" name a period too.
  const STAY_LINE = new RegExp(`\\b(?:wait|re-?appl\\w*|apply again|try again|come back)\\b[^.!?]{0,40}(?:${PERIOD}|${DATE})|\\b(?:before|after|until)\\s+(?:at least\\s+)?(?:${PERIOD}|${DATE})`, 'i');
  // Those words in the page's text, with the rest of their line. If they name no period, a short line just after them
  // saying how long to stay away comes too: the cooldown lasts as long as the employer says, and a question further down the form is not that.
  function refusalInText() {
    const body = String(document.body?.innerText || ''), thanked = THANKED.test(clean(body));
    const lines = body.split(/\n+/).map(clean).filter(Boolean);
    for (const [at, line] of lines.entries()) {
      const limit = line.search(LIMIT_WORDS), turned = turnedAwayAt(line, thanked);
      if (limit < 0 && turned < 0) continue;
      const before = line.slice(0, limit < 0 ? turned : turned < 0 ? limit : Math.min(limit, turned));
      const block = line.slice(Math.max(before.lastIndexOf('.'), before.lastIndexOf('!'), before.lastIndexOf('?')) + 1).trim();
      const period = new RegExp(`${PERIOD}|${DATE}`, 'i');
      const next = period.test(block) ? '' : lines.slice(at + 1, at + 3).find(text => STAY_LINE.test(text) && !text.includes('?') && text.length <= 200);
      return cut(next ? `${block}\n${next}` : block, 800);
    }
    return '';
  }
  // The employer's answer to our click. The same words on a page we never clicked answer nothing (a form can
  // say "we receive too many applications"), so they stop for the owner and close nothing.
  const REFUSED = '雇主拒收：';
  const refusal = words => (submitClicked ? REFUSED : '页面上写着这句话，但还没有点提交，不算雇主拒收，请你看一眼：') + words;
  // For the watch on her own hand submit: Filler clicked nothing, so whether these words answer anything is the watch's call.
  function refusalShown() {
    if (successEvidence()) return '';
    const words = employerRefusal() || refusalInText();
    return words ? REFUSED + words : '';
  }
  function blocker() {
    const refused = employerRefusal(); if (refused) return refusal(refused);
    if ([...document.querySelectorAll('iframe')].some(el => visible(el) && !quietCaptcha(el) && /captcha|recaptcha|hcaptcha/i.test(el.src || el.title))) return '页面有验证码，需要用户处理。';
    if ([...document.querySelectorAll('input[type=password]')].some(visible)) return '页面需要登录。';
    const limited = refusalInText();
    if (limited) return refusal(limited);
    // A code step can appear only after Submit; name it instead of timing out as "unknown".
    const gate = globalThis.JobScanPageGate?.inspect();
    if (gate && ['captcha', 'login', 'verification'].includes(gate.kind)) return gate.evidence;
    return '';
  }
  function preflight(snapshot) {
    if (fingerprint(capture()) !== fingerprint(snapshot)) return {ok:false,reason:'表单已改变。'};
    if (successEvidence()) return {ok:false,reason:'页面已经有投递成功回执，不再提交。'};
    const blocked = blocker(); if (blocked) return {ok:false,reason:blocked};
    // Ashby's banner answering our own last click is what the next click is for (see ashbyCorrections).
    const errors = snapshot.errors.filter(text => !(submitClicked && isAshby() && /^your form needs corrections\b/i.test(text)));
    if (snapshot.coverage !== 'complete' || errors.length) return {ok:false,reason:'仍有未读取控件或页面错误。'};
    const buttons = candidates();
    if (buttons.length !== 1) return {ok:false,reason:'没有唯一可确认的 Submit 按钮。'};
    const button = buttons[0];
    if (button.disabled || button.getAttribute('aria-disabled') === 'true') return {ok:false,reason:'Submit 按钮不可用，需要检查页面提示。'};
    const formText = clean((button.form || document.body).innerText);
    // Block's Greenhouse form asks for an "accept" to "binding arbitration with ... JAMS", worded as no agreement at all.
    if (/terms of (service|use)|binding agreement|arbitration agreement|binding arbitration|sign this agreement/i.test(formText)) return {ok:false,reason:'表单包含协议，需要用户确认后处理。'};
    const valid = button.form ? button.form.checkValidity() :
      [...document.querySelectorAll('input,textarea,select')].filter(el => visible(el) && !excluded(el)).every(el => el.checkValidity());
    if (!valid) return {ok:false,reason:'浏览器字段校验未通过。'};
    return {ok:true};
  }
  // Ashby lists what it refused in one banner above the button ("Your form needs corrections" / "Missing entry for
  // required field: Email"), not on the fields. Nothing went out, so this is a field error to fix, not an unclear result.
  function ashbyCorrections() {
    if (!isAshby()) return '';
    const lines = String(document.body?.innerText || '').split(/\n+/).map(clean).filter(Boolean);
    const at = lines.findIndex(line => /^your form needs corrections\b/i.test(line));
    if (at < 0) return '';
    const missing = lines.slice(at + 1, at + 30).filter(line => /^(?:missing entry for required field|invalid)\b/i.test(line));
    return cut([lines[at], ...missing].join('\n'), 3000);
  }
  // A value can show on the page while the form's own state never took it: on 2026-11-07 Numeral's Ashby form answered
  // "Missing entry for required field: Email" with the address on screen. The owner's fix by hand is to click elsewhere
  // or type it again, so right before the click every typed answer is written once more the way typing does: cleared,
  // set, then left. Setting the same value alone does nothing, as React skips a value it already holds. A typeahead is
  // left alone, as retyping it drops the option picked. Returns why it stopped if a value did not come back the same.
  async function recommitTyped() {
    if (typeof HTMLInputElement === 'undefined') return '';
    for (const el of document.querySelectorAll('input,textarea')) {
      if (excluded(el) || !visible(el) || el.disabled || el.readOnly || codeBox(el) || holdsWrittenCode(el)) continue;
      if (el.tagName === 'INPUT' && !/^(?:text|email|tel|url|number|)$/.test(el.type || '')) continue;
      if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') || el.closest('[role=combobox]')) continue;
      const value = String(el.value ?? '');
      if (!value.trim()) continue;
      const setter = Object.getOwnPropertyDescriptor(el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, 'value').set;
      el.focus();
      setter.call(el, '');
      el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'deleteContentBackward'}));
      setter.call(el, value);
      el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:value}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      el.dispatchEvent(new FocusEvent('focusout', {bubbles:true}));
      el.blur();
      if (el.value !== value) return `重新确认「${cut(label(el), 60)}」时页面把值改了，没有点提交。`;
    }
    const neutral = document.querySelector('h1,h2') || document.body;
    for (const type of ['mousedown', 'mouseup', 'click']) neutral.dispatchEvent(new MouseEvent(type, {bubbles:true}));
    await new Promise(resolve => setTimeout(resolve, 400));
    return '';
  }
  async function submit(snapshot) {
    let gate = preflight(snapshot);
    if (!gate.ok) return {outcome:'blocked',evidence:gate.reason};
    const changed = await recommitTyped();
    if (changed) return {outcome:'blocked',evidence:changed};
    gate = preflight(snapshot);
    if (!gate.ok) return {outcome:'blocked',evidence:gate.reason};
    // A banner left from the last click answers nothing new: it counts once it has gone and come back.
    const bannerCleared = !ashbyCorrections();
    submitClicked = true;
    candidates()[0].click();
    return answer(bannerCleared);
  }
  // Greenhouse emails a code after Submit, and the same button then finishes the same application. It was reviewed
  // and permitted already, so only a page still asking for that code, with one Submit to press, gets this click.
  async function submitAfterCode() {
    const buttons = candidates();
    if (!submitClicked || globalThis.JobScanPageGate?.inspect()?.kind !== 'verification')
      return {outcome:'blocked',evidence:'页面不在验证码这一步，没有再点提交。'};
    if (buttons.length !== 1 || buttons[0].disabled || buttons[0].getAttribute('aria-disabled') === 'true')
      return {outcome:'blocked',evidence:'验证码填好了，但找不到唯一可点的提交按钮；请你看一眼再点。'};
    buttons[0].click();
    return answer(!ashbyCorrections());
  }
  async function answer(bannerCleared) {
    const end = Date.now() + 45000;
    while (Date.now() < end) {
      await new Promise(resolve => setTimeout(resolve,350));
      const success = successEvidence();
      if (success) return {outcome:'submitted',evidence:success};
      const blocked = blocker(); if (blocked) return {outcome:'blocked',evidence:blocked};
      const corrections = ashbyCorrections();
      if (!corrections) bannerCleared = true;
      else if (bannerCleared && candidates().some(b => !b.disabled)) return {outcome:'validation_error',evidence:corrections};
      const after = capture(), errors = [...after.errors, ...after.fields.filter(f => f.error).map(f => `${f.label}: ${f.error}`)];
      // Retry only a specific invalid field; generic server/network failure is ambiguous.
      if (errors.length && after.fields.some(f => f.error) && candidates().some(b => !b.disabled))
        return {outcome:'validation_error',evidence:cut(errors.join('\n'), 3000)};
    }
    // What the page says is the only way to tell later what really happened.
    const shown = [...clean(document.body.innerText)].slice(-600).join('');
    return {outcome:'unknown',evidence:`没有读到明确成功回执或可修正的字段错误；可能已经提交，停止自动重试。页面现在显示：${shown}`};
  }
  const elementsFor = id => refs.get(id) || [];
  globalThis.JobScanFormAgent = {capture, prepareAttachments, rememberPicked, patch, fingerprint, radioQuestion, preflight, submit, submitAfterCode, elementsFor, refusalShown, codeWritten, statementOf, candidates};
})();
