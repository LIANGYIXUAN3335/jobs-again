// A sign-in page on a job site with a saved login gets one button. The button runs plain code:
// it asks for this exact site's login, types it and presses sign in. No model is involved.
(() => {
  if (globalThis.JobScanLoginFill || window !== window.top) return;
  globalThis.JobScanLoginFill = true;
  const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  const passwordField = () => [...document.querySelectorAll('input[type=password]')].find(visible);
  const ask = message => new Promise(resolve => {
    try { chrome.runtime.sendMessage(message, reply => resolve(chrome.runtime.lastError ? null : reply)); }
    catch { resolve(null); }
  });
  function setValue(el, value) {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, value);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }
  function usernameField(password) {
    const fields = [...document.querySelectorAll('input')].filter(el => visible(el) && !['password', 'hidden', 'checkbox', 'radio', 'submit', 'button'].includes(el.type));
    const named = fields.find(el => el.type === 'email' || /email|user|login/i.test([el.name, el.id, el.autocomplete, el.getAttribute('data-automation-id')].join(' ')));
    if (named) return named;
    // Otherwise the box just before the password box.
    return fields.filter(el => el.compareDocumentPosition(password) & Node.DOCUMENT_POSITION_FOLLOWING).pop();
  }
  // Only a button that says it signs in. "Any submit button" is not enough: an untyped <button>
  // counts as submit, so that could be "Accept cookies" or "Create account". Off the password
  // box's form, a bare "Continue" could be either of those too. No match: she presses it herself.
  function submitButton(password) {
    const scope = password.form || document;
    const says = password.form ? /^(sign ?in|log ?in|continue)$/i : /^(sign ?in|log ?in)$/i;
    return [...scope.querySelectorAll('button,input[type=submit],[role=button]')].filter(visible)
      .find(el => says.test((el.innerText || el.value || '').trim()));
  }
  // Answered: the service said whether this site has a saved login. A failed request says nothing, so it asks again.
  let answered = false, asking = false;
  async function offer() {
    if (answered || asking || !passwordField()) return;
    asking = true;
    const saved = await ask({type: 'loginStatus'});
    asking = false;
    if (!saved?.ok) return;
    answered = true;
    if (!saved.username) return;
    const host = document.createElement('div');
    host.id = 'jobscan-login-fill';
    const root = host.attachShadow({mode: 'closed'});
    const button = document.createElement('button');
    button.textContent = `用保存的账号登录（${saved.username}）`;
    button.style.cssText = 'position:fixed;top:16px;right:16px;z-index:2147483647;padding:8px 14px;border:0;border-radius:6px;background:#216b51;color:#fff;font:14px -apple-system,sans-serif;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25)';
    const shownAt = Date.now();
    button.onclick = async event => {
      if (!event.isTrusted) return;   // A script on the page cannot press this.
      // It can time the box to appear just as she goes to click in this corner, where the main panel sits. A press
      // this soon was meant for something else, and it would type her password into a box the page can read.
      if (Date.now() - shownAt < 1000) return;
      button.disabled = true;
      const login = await ask({type: 'loginFill'});
      const current = passwordField();
      if (!login?.ok || !login.password || !current) { button.textContent = '没能取到账号，请自己登录'; return; }
      const name = usernameField(current);
      if (name) setValue(name, login.username);
      setValue(current, login.password);
      await new Promise(resolve => setTimeout(resolve, 300));
      const go = submitButton(current);
      if (go) go.click();
      button.textContent = go ? '已填好并点了登录' : '已填好，请自己点登录';
    };
    root.append(button);
    (document.body || document.documentElement).append(host);
  }
  offer();
  // Sign-in forms can appear at any time after the page has loaded, with no new page load to run this again.
  const timer = setInterval(() => { if (answered) clearInterval(timer); else offer(); }, 2000);
})();
