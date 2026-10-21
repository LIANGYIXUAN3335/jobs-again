// The saved-login button: what it may press on the page, and when the worker hands a login over.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const fillSource = fs.readFileSync(path.join(__dirname, '../extension/login_fill.js'), 'utf8');
const workerSource = fs.readFileSync(path.join(__dirname, '../extension/background.js'), 'utf8');

class FakeInput {
  set value(next) { this.typed = next; }
  get value() { return this.typed || ''; }
}
function matches(el, selectors) {
  return selectors.split(',').some(selector => {
    if (selector === '[role=button]') return el.role === 'button';
    const [tag, type] = selector.match(/^(\w+)(?:\[type=(\w+)\])?$/).slice(1);
    return el.tag === tag && (!type || el.type === type);
  });
}
const common = (tag, options, clicks) => ({
  tag, innerText: '', getAttribute: () => null, getClientRects: () => options.hidden ? [] : [{}],
  dispatchEvent() {}, click() { clicks.push(this.innerText || this.value); }, compareDocumentPosition: () => 4, ...options,
});
// A <button> with no type attribute really does report type "submit": that is the whole defect.
function button(text, clicks, options = {}) { return common('button', {type: 'submit', innerText: text, ...options}, clicks); }
function submitInput(value, clicks) { return Object.assign(new FakeInput(), common('input', {type: 'submit'}, clicks), {typed: value}); }

// Runs the script over a page and presses the button it offers. `inForm` controls belong to the
// password box's form; with none, the box has no form and the search covers `controls` on the page.
async function press({controls = () => [], inForm = () => [], trusted = true, after = 2000} = {}) {
  const clock = {now: 1000};
  const clicks = [];
  const form = {querySelectorAll: selector => formControls.filter(el => matches(el, selector))};
  const formControls = inForm(clicks);
  const password = Object.assign(new FakeInput(), common('input', {type: 'password', form: formControls.length ? form : null}, clicks));
  const username = Object.assign(new FakeInput(), common('input', {type: 'email', form: null, name: 'email'}, clicks));
  const all = [username, password, ...formControls, ...controls(clicks)];
  let offered = null;
  const asked = [];
  const window = {};
  window.top = window;
  const ctx = vm.createContext({
    window, HTMLInputElement: FakeInput, Event: class {}, Node: {DOCUMENT_POSITION_FOLLOWING: 4}, Date: class extends Date { static now() { return clock.now; } },
    getComputedStyle: () => ({visibility: 'visible'}), setTimeout: fn => fn(), setInterval: () => 0, clearInterval() {},
    document: {
      querySelectorAll: selector => all.filter(el => matches(el, selector)),
      createElement(tag) {
        const el = {tag, style: {}, attachShadow: () => ({append(child) { offered = child; }})};
        return el;
      },
      body: {append() {}},
    },
    chrome: {runtime: {sendMessage(message, reply) {
      asked.push(message.type);
      reply(message.type === 'loginStatus' ? {ok: true, username: 'me@example.test'} : {ok: true, username: 'me@example.test', password: 'fake-pw'});
    }}},
  });
  vm.runInContext(fillSource, ctx);
  await new Promise(setImmediate);
  clock.now += after;
  await offered.onclick({isTrusted: trusted});
  return {clicks, text: offered.textContent, password: password.value, username: username.value, asked};
}

test('no form and no sign-in wording: no button is pressed, and it says so', async () => {
  const seen = await press({controls: clicks => [button('Accept cookies', clicks), button('Create account', clicks)]});
  assert.deepEqual(seen.clicks, []);
  assert.equal(seen.text, '已填好，请自己点登录');
  assert.equal(seen.password, 'fake-pw');
  assert.equal(seen.username, 'me@example.test');
});

test('a submit button in the form that says something else is not pressed', async () => {
  const seen = await press({inForm: clicks => [button('Register', clicks)]});
  assert.deepEqual(seen.clicks, []);
  assert.equal(seen.text, '已填好，请自己点登录');
});

test('the sign-in button is pressed even when another submit button comes first', async () => {
  const seen = await press({controls: clicks => [button('Accept cookies', clicks), button('Sign in', clicks)]});
  assert.deepEqual(seen.clicks, ['Sign in']);
  assert.equal(seen.text, '已填好并点了登录');
});

test('the wording is matched on an input submit and on a role=button too', async () => {
  assert.deepEqual((await press({inForm: clicks => [submitInput('Log in', clicks)]})).clicks, ['Log in']);
  const div = clicks => [common('div', {role: 'button', innerText: 'Login'}, clicks)];
  assert.deepEqual((await press({controls: div})).clicks, ['Login']);
});

test('Continue counts inside the password box\'s form, but outside one it says nothing about signing in', async () => {
  assert.deepEqual((await press({inForm: clicks => [button('Continue', clicks)]})).clicks, ['Continue']);
  assert.deepEqual((await press({controls: clicks => [button('Continue', clicks)]})).clicks, []);
});

test('a longer sentence that merely starts with the words does not count', async () => {
  const seen = await press({controls: clicks => [button('Sign in with Google', clicks), button('Continue with SSO', clicks)]});
  assert.deepEqual(seen.clicks, []);
});

test('a hidden sign-in button is left alone', async () => {
  const seen = await press({controls: clicks => [button('Sign in', clicks, {hidden: true})]});
  assert.deepEqual(seen.clicks, []);
});

test('a press in the moment the button appears fetches nothing: a page can time the box to her click in that corner', async () => {
  const seen = await press({controls: clicks => [button('Sign in', clicks)], after: 300});
  assert.deepEqual(seen.asked, ['loginStatus']);
  assert.equal(seen.password, '');
  assert.deepEqual(seen.clicks, []);
});

test('a click from a page script fetches nothing', async () => {
  const seen = await press({controls: clicks => [button('Sign in', clicks)], trusted: false});
  assert.deepEqual(seen.asked, ['loginStatus']);
  assert.deepEqual(seen.clicks, []);
  assert.equal(seen.password, '');
});

// ---- how long it keeps looking ----

// The script on a page with the interval in the test's hands. `status` answers each status request in turn.
function watching({status = [{ok: true, username: 'me@example.test'}], box = true} = {}) {
  const asked = [], offered = [];
  let tick = null, stopped = false, hold = null;
  const password = Object.assign(new FakeInput(), common('input', {type: 'password', form: null}, []));
  const window = {};
  window.top = window;
  const page = {box};
  vm.runInContext(fillSource, vm.createContext({
    window, HTMLInputElement: FakeInput, Event: class {}, Node: {DOCUMENT_POSITION_FOLLOWING: 4},
    getComputedStyle: () => ({visibility: 'visible'}), setTimeout: fn => fn(),
    setInterval: fn => { tick = fn; return 1; }, clearInterval: () => { stopped = true; },
    document: {
      querySelectorAll: selector => (page.box && matches(password, selector) ? [password] : []),
      createElement: tag => ({tag, style: {}, attachShadow: () => ({append(child) { offered.push(child); }})}),
      body: {append() {}},
    },
    chrome: {runtime: {sendMessage(message, reply) {
      asked.push(message.type);
      if (status[0] === 'hold') { hold = reply; return; }
      reply(status.length > 1 ? status.shift() : status[0]);
    }}},
  }));
  return {asked, offered, page, stopped: () => stopped, release(answer) { status.shift(); hold(answer); },
    every: async times => { for (let i = 0; i < times; i++) { tick(); await new Promise(setImmediate); } }};
}

test('a password box first shown long after the page loaded still gets the button', async () => {
  const view = watching({box: false});
  await view.every(200);   // 400 seconds, with no new page load
  assert.equal(view.asked.length, 0, 'no box, no request');
  assert.equal(view.stopped(), false);
  view.page.box = true;
  await view.every(1);
  assert.deepEqual(view.asked, ['loginStatus']);
  assert.equal(view.offered.length, 1);
});

test('a failed status request tries again, however it failed', async () => {
  for (const failed of [null, {ok: false, error: 'down'}]) {
    const view = watching({status: [failed, failed, {ok: true, username: 'me@example.test'}]});
    await new Promise(setImmediate);
    assert.equal(view.offered.length, 0);
    assert.equal(view.stopped(), false);
    await view.every(1);
    assert.equal(view.offered.length, 0);
    await view.every(1);
    assert.equal(view.offered.length, 1);
    assert.equal(view.asked.length, 3);
  }
});

test('it stops once the service says there is no saved login, and asks no more', async () => {
  const view = watching({status: [{ok: true, username: ''}]});
  await new Promise(setImmediate);
  await view.every(5);
  assert.equal(view.asked.length, 1);
  assert.equal(view.offered.length, 0);
  assert.equal(view.stopped(), true);
});

test('it stops once the button is shown, and never shows a second', async () => {
  const view = watching();
  await new Promise(setImmediate);
  await view.every(5);
  assert.equal(view.offered.length, 1);
  assert.equal(view.asked.length, 1);
  assert.equal(view.stopped(), true);
});

test('a request still on its way is not asked a second time', async () => {
  const view = watching({status: ['hold', {ok: true, username: 'me@example.test'}]});
  await view.every(3);
  assert.equal(view.asked.length, 1);
  view.release({ok: true, username: 'me@example.test'});
  await new Promise(setImmediate);
  assert.equal(view.offered.length, 1);
});

// ---- the service worker: it decides whether a saved login leaves at all ----

function worker() {
  const requests = [];
  const listeners = [];
  const storage = {jobscanPort: 8787, jobscanReviewTokens: {8787: 'fake-token'}};
  const ctx = vm.createContext({
    URL, console,
    fetch: async (url, init) => {
      requests.push({url, body: JSON.parse(init.body)});
      return {ok: true, json: async () => ({username: 'me@example.test', password: 'fake-pw'})};
    },
    chrome: {
      runtime: {onInstalled: {addListener() {}}, onMessage: {addListener(fn) { listeners.push(fn); }}},
      storage: {local: {get: async defaults => ({...defaults, ...storage})}},
    },
  });
  vm.runInContext(workerSource, ctx);
  return {requests, send: (message, sender) => new Promise(resolve => listeners[0](message, sender, resolve))};
}
const HOST = 'nvidia.wd5.myworkdayjobs.com';
const top = (url, extra = {}) => ({frameId: 0, url, tab: {url}, ...extra});

for (const type of ['loginStatus', 'loginFill']) {
  test(`${type}: the top frame of an https tab gets the login saved for that exact host`, async () => {
    const {requests, send} = worker();
    const reply = await send({type, host: 'other.myworkdayjobs.com'}, top(`https://${HOST}/login`));
    assert.equal(reply.ok, true);
    assert.equal(reply.username, 'me@example.test');
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, `http://127.0.0.1:8787/api/agent-lab/logins/${type === 'loginFill' ? 'fill' : 'status'}`);
    assert.equal(requests[0].body.host, HOST);   // From the tab, not from the message.
  });

  test(`${type}: refused on an http page of the saved host, which anyone on the network can serve`, async () => {
    const {requests, send} = worker();
    const reply = await send({type}, top(`http://${HOST}/login`));
    assert.equal(reply.ok, false);
    assert.equal(reply.password, undefined);
    assert.equal(requests.length, 0);
  });

  test(`${type}: refused to any frame but the top one`, async () => {
    const {requests, send} = worker();
    const reply = await send({type}, {frameId: 3, url: `https://${HOST}/embed`, tab: {url: `https://${HOST}/login`}});
    assert.equal(reply.ok, false);
    assert.equal(requests.length, 0);
  });

  test(`${type}: refused when the page that asked is not the one the tab now shows`, async () => {
    const {requests, send} = worker();
    const reply = await send({type}, {frameId: 0, url: `http://${HOST}/login`, tab: {url: `https://${HOST}/login`}});
    assert.equal(reply.ok, false);
    assert.equal(requests.length, 0);
  });

  test(`${type}: refused with no tab, or no frame or page to judge`, async () => {
    const {requests, send} = worker();
    assert.equal((await send({type}, {id: 'popup'})).ok, false);
    assert.equal((await send({type}, {frameId: 0, url: `https://${HOST}/login`})).ok, false);
    assert.equal((await send({type}, {url: `https://${HOST}/login`, tab: {url: `https://${HOST}/login`}})).ok, false);
    assert.equal((await send({type}, {frameId: 0, tab: {url: `https://${HOST}/login`}})).ok, false);
    assert.equal(requests.length, 0);
  });
}
