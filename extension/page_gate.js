// Read-only eligibility gate before filling. No credentials, mailbox reads, or clicks.
(() => {
  if (globalThis.JobScanPageGate) return;
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const text = el => clean(el.innerText || el.textContent || el.value);
  const visible = el => {
    if (!el || !el.getClientRects().length || el.closest('[hidden],[aria-hidden="true"],[inert],[id^="jobscan-"]')) return false;
    for (let current = el; current; current = current.parentElement) {
      const style = getComputedStyle(current);
      if (style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility) || style.opacity === '0') return false;
    }
    return true;
  };
  const all = (selector, root = document) => [...root.querySelectorAll(selector)].filter(visible);
  const headings = () => all('h1,h2,h3,[role="heading"],[role="alert"]');
  function fieldLabel(el) {
    // el.labels is a live NodeList in real browsers (no .map) and null on some input types.
    return clean([el.getAttribute('aria-label'), ...[...(el.labels || [])].map(label => text(label)),
      el.name, el.id, el.getAttribute('placeholder'), el.getAttribute('autocomplete')].filter(Boolean).join(' '));
  }
  // The same three as in form_agent.js, which keeps a code out of what the Reviewer reads. A test holds them together.
  const CODE_BOX = /\b(?:verification|security|one ?time|authentication|confirmation|email|access)\s*code\b|\bpasscode\b|\bone ?time (?:pass\w*|pin)\b|\botp\b|\bsecurity input\b|\b\d+ ?(?:digit|character|char) code\b|^(?:please )?(?:enter|input|type)\s+(?:(?:in|the|your)\s+)*(?:\d+ (?:digit|character|char)\s+)?code(?:\s+(?:we|you)\b.*)?\W*$|^code\W*$|验证码/i;
  const NOT_A_CODE = /\b(?:zip|postal|area|country|dial|employee|promo|referral|discount|coupon)\b/i;
  const CODE_SENT = /\b(?:sent|emailed)\b[^.!?]{0,160}\b(?:code|otp)\b|\b(?:code|otp)\b[^.!?]{0,160}\b(?:sent|emailed)\b|验证码[^。！？]{0,80}(?:已发送|已发至)|(?:已发送|已发至)[^。！？]{0,80}验证码/i;
  const codeBox = el => /^(?:text|number|tel|search|)$/.test(el.type || '') && (el.getAttribute('autocomplete') === 'one-time-code' ||
    [el.getAttribute('aria-label'), ...[...(el.labels || [])].map(label => text(label)), el.name, el.id, el.getAttribute('placeholder')].filter(Boolean)
      .map(value => clean(value).replace(/[_-]+/g, ' ')).some(value => CODE_BOX.test(value) && !NOT_A_CODE.test(value)));
  // One box per digit, however each is labelled.
  const digitBoxes = () => { const boxes = all('input[maxlength="1"]'); return boxes.length >= 4 && boxes.length <= 8; };
  const fillable = root => all('input,textarea,select,[role="combobox"]', root)
    .filter(el => !el.disabled && !['hidden', 'submit', 'button', 'password', 'reset'].includes(el.type));
  const applicant = el => /\b(?:e-?mail|phone|first.?name|last.?name|full.?name|name|resume|cv)\b/i.test(fieldLabel(el));
  // Ashby renders the whole application with no <form>: Submit and the applicant fields
  // only share a plain container, so the form walk below can never find them.
  function formlessApplication() {
    const submit = all('button,input[type="submit"],[role="button"]')
      .find(el => /^(?:submit(?: your)? application|send application)\b/i.test(text(el)));
    for (let box = submit?.parentElement; box; box = box.parentElement) {
      if (fillable(box).some(applicant)) return box;
    }
    return null;
  }
  function applicationForm() {
    for (const form of all('form,[role="form"]')) {
      const fields = fillable(form);
      if (!fields.length) continue;
      if (!fields.some(applicant)) continue;
      const submit = all('button,input[type="submit"],[role="button"]', form)
        .some(el => /^(?:submit(?: your)? application|send application|apply(?: now| for (?:this )?(?:job|position|role))?)\b/i.test(text(el)));
      const identity = clean([form.id, form.name, form.getAttribute('aria-label')].filter(Boolean).join(' '));
      const namedForm = /(?:^|[\s_-])(?:job[\s_-]*)?application(?:[\s_-]|$)/i.test(identity);
      const formHeading = all('h1,h2,h3,[role="heading"]', form)
        .some(el => /^(?:job application|application form|apply for (?:this )?(?:job|position|role))\b/i.test(text(el)));
      // A disabled Submit often becomes enabled after required fields are filled.
      // It identifies the form but never counts as evidence of submission.
      if (submit || namedForm || formHeading) return form;
    }
    return formlessApplication();
  }
  function expiredMessage(value) {
    const message = clean(value).replace(/^(?:sorry|unfortunately|error)[,:!.\s]+/i, '');
    return /^(?:(?:this|the)\s+)?(?:job(?: posting)?|position|role|vacancy)(?:\s+(?:you (?:are looking for|requested)))?\s+(?:(?:is|has been|has)\s+)?(?:no longer (?:available|active|accepting applications)|not found|filled|closed|expired)\s*(?:[.!,:;]|$)/i.test(message) ||
      /^(?:(?:we are|we're)\s+)?no longer accepting applications(?:\s+for (?:this|the) (?:job|position|role))?[.!]?$/i.test(message) ||
      /^applications (?:for this (?:job|position|role) )?(?:are|have been) closed[.!]?$/i.test(message);
  }
  function inspect() {
    const notices = headings();
    // An invisible reCAPTCHA (Greenhouse, Ashby) only scores the visit. A challenge it raises loads another frame.
    const captcha = all('iframe').find(el => !/[?&]size=invisible\b/.test(el.getAttribute('src') || '') &&
        /captcha|recaptcha|hcaptcha/i.test(`${el.getAttribute('src')} ${el.getAttribute('title')}`)) ||
      notices.find(el => /^(?:please )?(?:verify (?:that )?you are (?:a )?human|complete (?:the )?captcha|security check)\b/i.test(text(el)));
    if (captcha) return {kind:'captcha', evidence:'页面显示验证码或人机验证，需要用户处理。'};

    const password = all('input[type="password"]')[0];
    if (password) return {kind:'login', evidence:'页面有可见密码输入框，需要登录或创建账户。'};

    const otp = all('input').some(codeBox) || digitBoxes();
    const sent = all('main,[role="main"],form,[role="alert"],p,h1,h2,h3,label').map(text).find(value => CODE_SENT.test(value));
    if (otp && sent) return {kind:'verification', evidence:'页面显示验证码已发送，并有验证码输入框；需要处理验证步骤。'};

    const form = applicationForm();
    if (!form) {
      const login = notices.find(el => /^(?:(?:please|you must)\s+)?(?:sign[ -]?in|log[ -]?in)(?:\s+to (?:apply|continue|your account))?[.!]?$/i.test(text(el)) ||
        /^(?:create|sign up for) an? account to (?:apply|continue)[.!]?$/i.test(text(el))) ||
        all('button,a,[role="button"]').find(el => /^(?:sign[ -]?in|log[ -]?in) to (?:apply|continue)[.!]?$/i.test(text(el)));
      if (login) return {kind:'login', evidence:text(login).slice(0,300)};
      const expired = notices.find(el => expiredMessage(text(el)));
      if (expired) return {kind:'expired', evidence:text(expired).slice(0,300)};
    }
    if (form) return {kind:'ready', evidence:'页面有可见的申请表和可填写的申请人字段。'};
    return {kind:'unknown', evidence:'未找到可确认的申请表、下架通知或验证入口；暂不开始填写。'};
  }
  globalThis.JobScanPageGate = {inspect};
})();
