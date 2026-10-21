// The main panel on a real page: who may press it, what a batch fill is allowed to do, and what happens after a stop.
// The page is faked. The production functions are cut out of content.js by name, as the other .cjs tests do.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), test = require('node:test'), vm = require('node:vm');

const read = name => fs.readFileSync(path.join(__dirname, '../extension', name), 'utf8');
const source = read('content.js');
function section(start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `Missing production section: ${start}`);
  return source.slice(a, b);
}
const flush = async () => { for (let i = 0; i < 40; i++) await new Promise(setImmediate); };
const {rowsFrom} = vm.runInNewContext(`${read('stop_panel.js')}; globalThis.JobScanStopPanel`, {document: {}});
// The page's Submit button, and what a click on it looks like to the watch.
const SUBMIT = {tagName: 'BUTTON'};
const onSubmit = {closest: selector => (selector.includes('button') ? SUBMIT : null)};
const trusted = {isTrusted: true, stopPropagation() {}, preventDefault() {}};
const untrusted = {...trusted, isTrusted: false};

class Fake {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.dataset = {}; this.textContent = ''; this.className = ''; this.disabled = false; this.hidden = false;
    this.style = {setProperty() {}}; this.classList = {add() {}, remove() {}, contains: () => false, toggle: () => false}; this.listeners = {};
  }
  appendChild(node) { this.children.push(node); node.parent = this; return node; }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener() {}
  remove() { this.removed = true; }
  querySelectorAll() { return []; }
  closest() { return null; }
  getAttribute() { return null; }
  // The test presses the real handlers through `nodes`; a page script has no such way in.
  attachShadow(init) { this.init = init; this.nodes = {}; return {set innerHTML(_) {}, querySelector: selector => this.nodes[selector] ||= new Fake()}; }
  walk() { return [this, ...this.children.flatMap(node => node.walk())]; }
}

// One panel on one form, with every collaborator recorded.
function page({role = {}, replies = {}, submit = {outcome: 'unknown', evidence: '没有读到回执。'}, preflight = {ok: true}, applied = false, mail = true} = {}) {
  const sent = [], filled = [], timers = new Map(), listeners = {click: [], keydown: []}, shown = [], delays = [];
  let refusal = '';
  const context = {
    console, URL, Number, setTimeout: (fn, ms) => { delays.push(ms); fn(); return 0; }, clearTimeout() {},
    setInterval: fn => { const id = timers.size + 1; timers.set(id, fn); return id; }, clearInterval: id => timers.delete(id),
    location: {href: 'https://jobs.ashbyhq.com/acme/1/application', pathname: '/acme/1/application', hostname: 'jobs.ashbyhq.com'},
    confirm: () => true, prompt: () => '30',
    document: {
      documentElement: new Fake('html'), getElementById: () => null, createElement: tag => new Fake(tag),
      querySelectorAll: () => [], querySelector: () => null,
      addEventListener: (type, fn) => listeners[type].push(fn),
      removeEventListener: (type, fn) => { listeners[type] = listeners[type].filter(item => item !== fn); },
    },
    send: async message => {
      sent.push(message);
      const reply = replies[message.type];
      if (reply) return typeof reply === 'function' ? reply(message) : reply;
      const run = {id: 'run', attempt_id: 'try', job_id: 'job-1', expires_at: Date.now() / 1000 + 600,
        result: {summary: '通过', model_status: 'completed', decision: 'pass', issues: []}};
      if (message.type === 'runnerLookup') return {found: true, role: runnerRole(context.liveRole)};
      return {ok: true, run};
    },
    fillFields: async profile => { filled.push({...profile}); return {filled: ['name'], skipped: []}; },
    fillCommon: async () => [], fillAshbyButtons: async () => {}, fillAshbyHistory: async () => {}, fillGreenhouseHistory: async () => {},
    fillDemographics: async () => 0, untilSettled: async () => 0, fillCoverLetterBox: async () => {}, fillCode: () => true,
    claimPin() {}, sparkleStart() {}, attachResume: () => true, b64ToBytes: () => [], fillGoogle: async () => {}, fillSmartRecruiters: async () => {}, fillWorkday: async () => {},
    JobScanFormAgent: {
      capture: () => ({url: 'u', document_id: 'd', fields: [], attachments: [], coverage: 'complete', errors: []}),
      fingerprint: JSON.stringify, prepareAttachments: async () => {}, elementsFor: () => [], patch: () => [],
      preflight: () => (applied ? {ok: false, reason: '页面已经有投递成功回执，不再提交。'} : preflight),
      submit: async () => (typeof submit === 'function' ? submit() : submit),
      refusalShown: () => refusal, candidates: () => [SUBMIT],
    },
    JobScanPageGate: {inspect: () => ({kind: 'ready'})},
    JobScanStopPanel: {rowsFrom, show: args => { const panel = {args, applied: [], refused: [], notes: []}; shown.push(panel);
      return {applied(text) { panel.applied.push(text); }, refused(text) { panel.refused.push(text); }, note(text) { panel.notes.push(text); }}; }},
    setApplied(value) { applied = value; },
    setRefusal(value) { refusal = value; },
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(read('agent_workflow.js'), context);
  vm.runInContext(`var abortFill = false, panelLog = null, runnerDeadline = 0, IS_GOOGLE = false, IS_SR = false, IS_WORKDAY = false, IS_ASHBY = true;
    const ABORT = "__jobscan_abort__"; const sleep = async () => {}; const humanPause = async () => {};
    const checkDeadline = () => {}; const isContextDead = () => false;
    ${section('  // Only a person\'s click counts.', '  // Human-like pacing')}
    ${section('  // ---- floating panel ----', '  // ---- boot:')}
    ${section('  function capturePill() {', '  // The pill belongs on every careers page')}
    globalThis.panel = panel; globalThis.capturePill = capturePill;`, context);
  const full = {job_id: 'job-1', company: 'Acme', title: 'Engineer', status: 'new', this_status: 'new', apply_profile: 'casey', tailor_status: 'ready',
    has_tailored_pdf: false, resume_variant: 'tailored', ai_enabled: true, mailcode_enabled: mail, company_applied: [], eeo_decline: false,
    shared_memory: {submission_policy: {authorization: 'standing'}}, profile: {first_name: 'A', last_name: 'L', email: 'a@example.test'}, ...role};
  context.liveRole = full;
  const host = context.panel(full);
  const tick = async () => { for (const fn of [...timers.values()]) await fn(); await flush(); };
  return {context, host, sent, filled, timers, listeners, shown, delays, tick, types: () => sent.map(message => message.type),
    button: selector => host.nodes[selector],
    logged: () => (host.nodes['.log'] ? host.nodes['.log'].walk().map(node => node.textContent).join('\n') : ''),
    dynamic: () => host.nodes['.status'].walk().filter(node => node.onclick)};
}
function runnerRole(role) { return {...role, profile: {...role.profile}, has_tailored_pdf: true}; }
const runnerRun = view => view.context.JobScanRunReview({job_id: 'job-1', apply_profile: 'casey', lease: {worker_id: 'w'}, deadline: Date.now() + 60000});

test('the main panel and the capture pill keep their roots closed', () => {
  const view = page();
  assert.equal(view.host.init.mode, 'closed');
  view.context.capturePill();
  const pill = view.context.document.documentElement.children.at(-1);
  assert.equal(pill.id, 'jobscan-capture');
  assert.equal(pill.init.mode, 'closed');
});

test('a script pressing any acting button gets nothing', async () => {
  for (const selector of ['.fill', '.agent-review', '.agent-review-only', '.agent-submit', '.code', '.submitted', '.x', '.min']) {
    const view = page();
    view.button(selector).onclick(untrusted);
    await flush();
    assert.deepEqual(view.types(), [], selector);
    assert.equal(view.filled.length, 0, selector);
    assert.ok(!view.host.removed, selector);
  }
});

test('the panel\'s other buttons ignore a script too', async () => {
  const card = name => ({label: name, display_name: name, email: `${name}@example.test`});
  const view = page({role: {pins_rejected: [{company: 'Other', title: 'Role', job_id: 'j2'}],
    apply_profile: 'casey', apply_profiles: {default: 'casey', profiles: {casey: card('A'), alex: card('Y')}}}});
  const buttons = view.dynamic();
  assert.ok(buttons.length >= 7, 'pin, identity switch, four cooldowns, custom');
  for (const button of buttons) button.onclick(untrusted);
  await flush();
  assert.deepEqual(view.types(), []);
  assert.equal(view.context.document.documentElement.children.length, 1, 'no second panel built');
});

test('a real click still works', async () => {
  let view = page();
  view.button('.fill').onclick(trusted);
  await flush();
  assert.equal(view.filled.length, 1);
  view = page();
  view.button('.submitted').onclick(trusted);
  await flush();
  assert.deepEqual(view.types().slice(0, 1), ['markApplied']);
  view = page();
  view.button('.agent-review').onclick(trusted);
  await flush();
  assert.ok(view.types().includes('agentReview'));
});

test('the undo button on a cooldown ignores a script', async () => {
  const view = page({role: {cooldown: {until: '2027-01-01', days_left: 30, reason: '', source: 'manual'}}});
  const [undo] = view.dynamic();
  undo.onclick(untrusted);
  await flush();
  assert.deepEqual(view.types(), []);
  undo.onclick(trusted);
  await flush();
  assert.deepEqual(view.types(), ['cooldownClear']);
});

test('a chip and the capture pill act only on a real click', async () => {
  const chip = new Fake('button');
  const context = {pressed: null, sparkleDraft() { context.drafted = true; }, sparkleHost: new Fake(), document: {documentElement: {contains: () => true}, createElement: () => chip}};
  vm.createContext(context);
  vm.runInContext(`${section('  // Only a person\'s click counts.', '  // Human-like pacing')}
    var sparkleHost = globalThis.sparkleHost; ${section('  function sparkleChipFor(el) {', '  async function sparkleDraft(')}
    globalThis.chipFor = sparkleChipFor;`, context);
  context.chipFor({dataset: {}});
  chip.onclick(untrusted);
  assert.equal(context.drafted, undefined);
  chip.onclick(trusted);
  assert.equal(context.drafted, true);

  const view = page();
  view.context.capturePill();
  const nodes = view.context.document.documentElement.children.at(-1).nodes;
  let read = 0;
  view.context.extractJob = async () => { read++; return {title: 't', jd: 'x'.repeat(100)}; };
  nodes['.cap'].onclick(untrusted);
  await flush();
  assert.equal(read, 0);
  nodes['.cap'].onclick(trusted);
  await flush();
  assert.equal(read, 1);
});

test('a code from an unknown sender never reaches the log, and is not even fetched', async () => {
  const view = page({replies: {emailCode: message => (message.company ? {ok: true, code: null} : {ok: true, code: '482913', subject: 'Your code'})}});
  view.button('.code').onclick(trusted);
  await flush();
  assert.deepEqual(view.sent.map(message => message.company), ['Acme'], 'only the company-matched lookup runs');
  assert.doesNotMatch(view.logged(), /482913/);
  assert.match(view.logged(), /邮箱/);
});

test('this company\'s own code goes straight into the field', async () => {
  const view = page({replies: {emailCode: {ok: true, code: '111222'}}});
  let code = '';
  view.context.fillCode = value => { code = value; return true; };
  view.button('.code').onclick(trusted);
  await flush();
  assert.equal(code, '111222');
  assert.doesNotMatch(view.logged(), /111222/);
});

test('no company name means no lookup at all: without one the service returns the newest code from anyone', async () => {
  const view = page({role: {company: ''}, replies: {emailCode: {ok: true, code: '482913'}}});
  view.button('.code').onclick(trusted);
  await flush();
  assert.deepEqual(view.types(), []);
  assert.match(view.logged(), /邮箱/);
});

test('a batch fill is marked unattended and names the company; the panel\'s own Fill is not', async () => {
  let view = page();
  await runnerRun(view);
  assert.equal(view.filled[0].unattended, true);
  assert.equal(view.filled[0].company_name, 'Acme');
  view = page();
  view.button('.fill').onclick(trusted);
  await flush();
  assert.equal(view.filled[0].unattended, false);
  assert.equal(view.filled[0].company_name, 'Acme');
});

test('the mark does not stick: a Fill after a batch on the same tab is attended again', async () => {
  const view = page();
  await runnerRun(view);
  view.button('.fill').onclick(trusted);
  await flush();
  assert.equal(view.filled.at(-1).unattended, false);
});

test('a profile refreshed from the server still gets both marks', async () => {
  const view = page({role: {profile: {}}, replies: {profile: {ok: true, profile: {first_name: 'A', unattended: true, company_name: 'Wrong'}}}});
  view.button('.fill').onclick(trusted);
  await flush();
  assert.equal(view.filled[0].unattended, false);
  assert.equal(view.filled[0].company_name, 'Acme');
});

test('a stop after Submit was clicked tells the panel, on an unknown result and on a captcha after the click', async () => {
  for (const submit of [{outcome: 'unknown', evidence: '没有读到回执。'}, {outcome: 'blocked', evidence: '页面有验证码，需要用户处理。'}]) {
    const view = page({submit});
    const result = await runnerRun(view);
    assert.equal(result.status, submit.outcome);
    assert.equal(view.shown[0].args.clicked, true);
  }
});

test('a receipt that shows up after an unclear click still gets recorded once she has acted', async () => {
  const view = page();
  await runnerRun(view);
  const fn = view.listeners.click[0];
  assert.ok(fn, 'the watch outlives the click');
  fn({isTrusted: true, target: {closest: () => null}});
  view.context.setApplied(true);
  await view.tick();
  assert.equal(view.sent.filter(message => message.type === 'markApplied').length, 1);
  assert.match(view.shown[0].applied[0], /记为已投/);
});

test('a permit refused because an earlier click is on file counts as clicked, so the panel does not say "not submitted"', async () => {
  const view = page({replies: {agentPermit: {ok: false, error: '已提交、结果不明或已达两次尝试上限；请核对原申请。'}}});
  const result = await runnerRun(view);
  assert.equal(result.status, 'unknown');
  assert.equal(result.clicked, true);
  assert.equal(view.shown[0].args.clicked, true);
});

test('any other permit refusal is still a stop before the click', async () => {
  const view = page({replies: {agentPermit: {ok: false, error: '公司仍在投递冷却期内。'}}});
  const result = await runnerRun(view);
  assert.equal(result.status, 'blocked');
  assert.equal(view.shown[0].args.clicked, false);
});

test('a stop before any click says so', async () => {
  const view = page({preflight: {ok: false, reason: '仍有未读取控件或页面错误。'}});
  const result = await runnerRun(view);
  assert.equal(result.status, 'needs_user');
  assert.equal(view.shown[0].args.clicked, false);
  assert.ok(!view.types().includes('agentResult'));
});

test('an exception after the click still counts as clicked', async () => {
  const view = page({replies: {agentResult: {ok: false}}});
  const result = await runnerRun(view);
  assert.equal(result.status, 'unknown');
  assert.equal(view.shown[0].args.clicked, true);
});

// ---- her hand submit, with or without a stop ----
const markCount = view => view.sent.filter(message => message.type === 'markApplied').length;

test('the watch starts with the panel: a tab a gate held back, or one she reloaded, never reached a stop', async () => {
  const view = page();
  assert.equal(view.listeners.click.length, 1);
  view.listeners.click[0]({isTrusted: true, target: {closest: () => null}});
  view.context.setApplied(true);
  await view.tick();
  assert.deepEqual(view.sent.filter(message => message.type === 'markApplied').map(message => message.jobId), ['job-1']);
  assert.equal(view.button('.submitted').disabled, true);
  assert.equal(view.listeners.click.length, 0, 'the watch has ended');
  assert.match(view.logged(), /记为已投/);
});

test('no receipt, or no action of hers, marks nothing on a tab with no stop', async () => {
  let view = page();
  view.listeners.click[0]({isTrusted: true, target: {closest: () => null}});
  await view.tick();
  assert.equal(markCount(view), 0);
  view = page();
  view.context.setApplied(true);
  await view.tick();
  view.listeners.click[0]({isTrusted: false, target: {closest: () => null}});
  await view.tick();
  assert.equal(markCount(view), 0);
});

test('a receipt already there at her first action came before it, so it proves nothing', async () => {
  const view = page();
  view.context.setApplied(true);
  view.listeners.click[0]({isTrusted: true, target: {closest: () => null}});
  await view.tick();
  assert.equal(markCount(view), 0);
  assert.equal(view.listeners.click.length, 0);
});

test('a role the page does not identify is not marked from here', async () => {
  for (const role of [{top_role_default: true}, {no_role_match: true}, {status: 'applied'}, {job_id: ''}]) {
    const view = page({role});
    assert.equal(view.listeners.click.length, 0, JSON.stringify(role));
  }
});

test('a panel rebuilt on the same page takes over the watch instead of adding one', () => {
  const view = page();
  view.context.panel(view.context.liveRole);
  assert.equal(view.listeners.click.length, 1);
  assert.equal(view.listeners.keydown.length, 1);
});

test('closing the panel does not end the watch: she can still submit by hand', async () => {
  const view = page();
  view.button('.x').onclick(trusted);
  view.listeners.click[0]({isTrusted: true, target: {closest: () => null}});
  view.context.setApplied(true);
  await view.tick();
  assert.equal(markCount(view), 1);
});

test('her pressing I submitted this ends the watch', async () => {
  const view = page();
  view.button('.submitted').onclick(trusted);
  await flush();
  view.listeners.click[0]({isTrusted: true, target: {closest: () => null}});
  view.context.setApplied(true);
  await view.tick();
  assert.equal(markCount(view), 1, 'only her own press');
  assert.equal(view.listeners.click.length, 0);
});

test('a workflow in flight records its own receipt, and the watch stays out of it', async () => {
  let release;
  const view = page({submit: () => new Promise(resolve => { release = resolve; })});
  view.listeners.click[0]({isTrusted: true, target: {closest: () => null}});
  const run = runnerRun(view);
  await flush();
  view.context.setApplied(true);
  await view.tick();
  assert.equal(markCount(view), 0, 'still clicking');
  release({outcome: 'submitted', evidence: 'Your application was submitted.'});
  await run;
  await view.tick();
  assert.equal(markCount(view), 1, 'the workflow, once');
});

// Stop before any click, ready to watch.
async function stopped(options = {}) {
  const view = page({preflight: {ok: false, reason: '仍有未读取控件或页面错误。'}, ...options});
  await runnerRun(view);
  view.press = (target = {closest: () => null}, isTrusted = true) => { for (const fn of view.listeners.click) fn({isTrusted, target}); };
  view.marks = () => view.sent.filter(message => message.type === 'markApplied');
  return view;
}

test('her hand submit, then the employer\'s receipt, marks the role applied and says so', async () => {
  const view = await stopped();
  view.press();
  await view.tick();
  assert.equal(view.marks().length, 0, 'no receipt yet');
  view.context.setApplied(true);
  await view.tick();
  assert.deepEqual(view.marks().map(message => [message.jobId, message.variant]), [['job-1', 'tailored']]);
  assert.match(view.shown[0].applied[0], /记为已投/);
  assert.match(view.logged(), /记为已投/);
  assert.equal(view.button('.submitted').disabled, true);
  await view.tick();
  assert.equal(view.marks().length, 1, 'once');
  assert.equal(view.listeners.click.length, 0, 'the watch has ended');
});

test('a receipt with no action of hers marks nothing: a page script can write one but cannot fake her click', async () => {
  const view = await stopped();
  view.context.setApplied(true);
  await view.tick();
  view.press({closest: () => null}, false);
  await view.tick();
  assert.equal(view.marks().length, 0);
});

test('pressing JobScan\'s own panels is not submitting the form', async () => {
  const view = await stopped();
  view.press({closest: selector => (selector === '[id^="jobscan-"]' ? {} : null)});
  view.context.setApplied(true);
  await view.tick();
  assert.equal(view.marks().length, 0);
});

test('a keystroke counts: Enter can submit a form', async () => {
  const view = await stopped();
  for (const fn of view.listeners.keydown) fn({isTrusted: true, target: {}});
  view.context.setApplied(true);
  await view.tick();
  assert.equal(view.marks().length, 1);
});

test('a receipt already on the page at the stop proves nothing about what she does next', async () => {
  const view = page({applied: true});
  await runnerRun(view);
  assert.equal(view.listeners.click.length, 0);
  assert.equal(view.timers.size, 1, 'only the staleness timer');
  assert.equal(view.sent.filter(message => message.type === 'markApplied').length, 0);
});

test('moving to another posting in the same tab ends the watch', async () => {
  const view = await stopped();
  view.press();
  view.context.location.pathname = '/acme/2/application';
  view.context.setApplied(true);
  await view.tick();
  assert.equal(view.marks().length, 0);
  assert.equal(view.listeners.click.length, 0);
});

test('a later path under the same posting keeps the watch', async () => {
  const view = await stopped();
  view.press();
  view.context.location.pathname = '/acme/1/application/thanks';
  view.context.setApplied(true);
  await view.tick();
  assert.equal(view.marks().length, 1);
});

test('when the record will not save she is told to press the button herself', async () => {
  const view = await stopped({replies: {markApplied: {ok: false, error: 'down'}}});
  view.press();
  view.context.setApplied(true);
  for (let i = 0; i < 6; i++) await view.tick();
  assert.equal(view.marks().length, 5);
  assert.match(view.shown[0].notes[0], /I submitted this/);
  assert.equal(view.shown[0].applied.length, 0);
});

// ---- once the employer's receipt is on record the page closes itself ----
const closes = view => view.types().filter(type => type === 'closeTab').length;
const SENT = {outcome: 'submitted', evidence: 'Your application was submitted.'};

test('I submitted this closes the tab after the delay, and only once the record is saved', async () => {
  let view = page();
  view.button('.submitted').onclick(trusted);
  await flush();
  assert.equal(closes(view), 1);
  assert.ok(view.delays.includes(1200));
  assert.ok(view.types().indexOf('markApplied') < view.types().indexOf('closeTab'));
  view = page({replies: {markApplied: {ok: false, error: 'down'}}});
  view.button('.submitted').onclick(trusted);
  await flush();
  assert.equal(closes(view), 0);
});

test('a hand submit recorded automatically closes the tab the same way', async () => {
  const view = page();
  view.listeners.click[0]({isTrusted: true, target: {closest: () => null}});
  view.context.setApplied(true);
  await view.tick();
  assert.equal(closes(view), 1);
  assert.ok(view.delays.includes(1200));
  assert.ok(view.types().indexOf('markApplied') < view.types().indexOf('closeTab'));
  await view.tick();
  assert.equal(closes(view), 1, 'once');
  assert.doesNotMatch(view.logged(), /可以关了/, 'she has nothing left to do');
});

test('a hand submit that will not save keeps the tab, with the button still there to press', async () => {
  const view = page({replies: {markApplied: {ok: false, error: 'down'}}});
  view.listeners.click[0]({isTrusted: true, target: {closest: () => null}});
  view.context.setApplied(true);
  for (let i = 0; i < 6; i++) await view.tick();
  assert.equal(closes(view), 0);
});

test('Fill, Review, Submit on the panel closes the tab once the employer confirmed', async () => {
  const view = page({submit: SENT});
  view.button('.agent-submit').onclick(trusted);
  await flush();
  assert.equal(closes(view), 1);
  assert.ok(view.delays.includes(1200));
  assert.ok(view.types().indexOf('markApplied') < view.types().indexOf('closeTab'));
});

test('a stop or an unclear result keeps the tab, on the panel and in a batch', async () => {
  for (const options of [{submit: {outcome: 'unknown', evidence: '没有读到回执。'}}, {submit: {outcome: 'blocked', evidence: '页面有验证码，需要用户处理。'}},
    {submit: {outcome: 'blocked', evidence: '雇主拒收：You have already applied to this job.'}}, {preflight: {ok: false, reason: '仍有未读取控件或页面错误。'}},
    {replies: {agentPermit: {ok: false, error: '公司仍在投递冷却期内。'}}}]) {
    const view = page(options);
    view.button('.agent-submit').onclick(trusted);
    await flush();
    assert.equal(closes(view), 0, JSON.stringify(options));
    const batch = page(options);
    await runnerRun(batch);
    await batch.tick();
    assert.equal(closes(batch), 0, JSON.stringify(options));
  }
});

test('a receipt that did not get recorded keeps the tab: its warning and the button are all she has', async () => {
  const view = page({submit: SENT, replies: {markApplied: {ok: false, error: 'down'}}});
  view.button('.agent-submit').onclick(trusted);
  await flush();
  assert.ok(view.types().includes('markApplied'));
  assert.equal(closes(view), 0);
  assert.match(view.logged(), /记账失败/);
});

test('a batch leaves closing to the runner page, which closes after /finish', async () => {
  const view = page({submit: SENT});
  await runnerRun(view);
  assert.equal(closes(view), 0);
});

test('a script pressing Fill, Review, Submit closes nothing', async () => {
  const view = page({submit: SENT});
  view.button('.agent-submit').onclick(untrusted);
  await flush();
  assert.deepEqual(view.types(), []);
});

// ---- the employer says no to her hand submit ----
const WORDS = '雇主拒收：You recently applied to this position. Please wait 6 months before applying again.';
const reports = view => view.sent.filter(message => message.type === 'handRefusal');
const herAction = view => view.listeners.click[0]({isTrusted: true, target: {closest: () => null}});
const herSubmit = view => view.listeners.click[0]({isTrusted: true, target: onSubmit});

test('refusal words that appear after she presses Submit are reported once, with the job, the words and the page', async () => {
  const view = page();
  herSubmit(view);
  await view.tick();
  assert.equal(reports(view).length, 0);
  view.context.setRefusal(WORDS);
  await view.tick();
  assert.deepEqual(reports(view).map(message => [message.jobId, message.evidence, message.url]), [['job-1', WORDS, view.context.location.href]]);
  await view.tick();
  assert.equal(reports(view).length, 1, 'once');
  assert.match(view.logged(), /不收这份/);
  assert.equal(markCount(view), 0, 'a refusal is not an application');
  assert.equal(closes(view), 0, 'the tab stays so she can read it');
});

test('she is told on the stop panel too, and it stops inviting a re-run', async () => {
  const view = await stopped();
  view.press(onSubmit);
  view.context.setRefusal(WORDS);
  await view.tick();
  assert.equal(reports(view).length, 1);
  assert.match(view.shown[0].refused[0], /不收这份/);
});

test('words already on the page when she presses Submit answer nothing she did', async () => {
  const view = page();
  view.context.setRefusal(WORDS);
  herSubmit(view);
  await view.tick();
  assert.equal(reports(view).length, 0);
  herSubmit(view);
  await view.tick();
  assert.equal(reports(view).length, 0, 'not by a later press either');
});

test('those words after any other click close nothing, and she is told once how to do it herself', async () => {
  const view = page();
  herAction(view);
  view.context.setRefusal(WORDS);
  await view.tick();
  await view.tick();
  assert.equal(reports(view).length, 0, 'a filter, an FAQ or a follow-up question can say the same');
  assert.equal(view.logged().match(/还没点提交/g).length, 1);
  assert.match(view.logged(), /不让投了/);
  assert.equal(closes(view), 0);
});

test('words she brought up with another click stay unanswered when she then presses Submit', async () => {
  const view = page();
  herAction(view);
  view.context.setRefusal(WORDS);
  await view.tick();
  herSubmit(view);
  await view.tick();
  assert.equal(reports(view).length, 0);
});

test('Enter inside a box counts as Submit, and no other key or box does', async () => {
  const typed = key => ({isTrusted: true, type: 'keydown', key, target: {tagName: 'INPUT', closest: () => null}});
  let view = page();
  view.listeners.keydown[0](typed('Enter'));
  view.context.setRefusal(WORDS);
  await view.tick();
  assert.equal(reports(view).length, 1);
  for (const event of [typed('a'), {...typed('Enter'), target: {tagName: 'TEXTAREA', closest: () => null}}]) {
    view = page();
    view.listeners.keydown[0](event);
    view.context.setRefusal(WORDS);
    await view.tick();
    assert.equal(reports(view).length, 0);
  }
});

test('a role the page only knows from a pin may be another posting, so the words freeze nothing and she is told', async () => {
  for (const role of [{from_pin: true}, {pin_override: true}]) {
    const view = page({role});
    herSubmit(view);
    view.context.setRefusal(WORDS);
    await view.tick();
    await view.tick();
    assert.equal(reports(view).length, 0);
    assert.equal(view.logged().match(/不一定是这个岗位/g).length, 1);
    assert.match(view.logged(), /不让投了/);
  }
});

test('on the panel\'s own Submit a refusal says what happened, not that something waits for her', async () => {
  const view = page({submit: {outcome: 'blocked', evidence: WORDS}});
  view.button('.agent-submit').onclick(trusted);
  await flush();
  assert.match(view.logged(), /雇主说不收这份/);
  assert.match(view.logged(), /不用再提交/);
  assert.doesNotMatch(view.logged(), /需要处理/);
});

test('a stop before any click still says what it needs', async () => {
  const view = page({preflight: {ok: false, reason: '仍有未读取控件或页面错误。'}});
  view.button('.agent-submit').onclick(trusted);
  await flush();
  assert.match(view.logged(), /需要处理：/);
});

test('the words behind a cooldown stay text on the panel: a script from that page would see this closed root', () => {
  const reason = '雇主拒收：<img src=x onerror=alert(1)> You have already applied.';
  const view = page({role: {company: '<b>Acme</b>', cooldown: {until: '2027-01-01', days_left: 3, reason, source: 'manual'}}});
  const nodes = view.host.nodes['.status'].walk();
  assert.ok(nodes.every(node => !String(node.innerHTML || '').includes('onerror')), 'no markup');
  assert.ok(nodes.some(node => node.textContent.includes(reason) && node.textContent.includes('<b>Acme</b>')), 'she still reads them');
});

test('the status line keeps titles and company names as text: they come from the posting', () => {
  const evil = '<img src=x onerror=alert(1)>';
  const guessed = page({role: {company: evil, title: evil, this_status: 'applied', top_role_default: true}});
  const before = page({role: {company: evil, title: 'Engineer',
    company_applied: [{title: evil, status: 'applied'}, {title: '" onmouseover="x', status: 'applied'}], pins_rejected: [{company: evil, title: evil}]}});
  for (const view of [guessed, before]) assert.doesNotMatch(view.host.nodes['.status'].innerHTML, /<img|" onmouseover/);
  assert.match(guessed.host.nodes['.status'].innerHTML, /&#60;img/);
  assert.match(before.host.nodes['.status'].innerHTML, /applied to &#60;img/);
});

test('the stop panel gets one row per question: two complaints on a field, and a title the form repeats', async () => {
  const fields = [{id: 'a', label: 'Why us?', kind: 'textarea', value: '', options: []},
    {id: 'b', label: 'Education History', kind: 'select', value: '', options: ['January']}, {id: 'c', label: 'Education History', kind: 'select', value: '', options: ['January']}];
  const issues = [{field_id: 'a', reason: '必填'}, {field_id: 'a', reason: '网页校验未通过'}, {field_id: 'b', reason: '必填'}, {field_id: 'c', reason: '必填'}];
  const run = {id: 'run', attempt_id: 'try', job_id: 'job-1', expires_at: Date.now() / 1000 + 600, status: 'needs_user',
    result: {summary: '停下', model_status: 'skipped', decision: 'needs_user', issues}};
  const view = page({replies: {agentReview: {ok: true, run}}});
  view.context.JobScanFormAgent.capture = () => ({url: 'u', document_id: 'd', fields, attachments: [], coverage: 'complete', errors: []});
  await runnerRun(view);
  const {items} = view.shown[0].args;
  assert.deepEqual(JSON.parse(JSON.stringify(items.map(item => [item.label, item.reason, item.answerable]))),
    [['Why us?', '必填；网页校验未通过', true], ['Education History', '必填', false]]);
  assert.deepEqual([...items[1].also], ['c']);
});

test('no action of hers reports nothing, and a script or JobScan\'s own panel is no action', async () => {
  let view = page();
  view.context.setRefusal(WORDS);
  await view.tick();
  assert.equal(reports(view).length, 0);
  view = page();
  view.listeners.click[0]({isTrusted: false, target: onSubmit});
  view.context.setRefusal(WORDS);
  await view.tick();
  assert.equal(reports(view).length, 0);
  view = page();
  view.listeners.click[0]({isTrusted: true, target: {closest: selector => (selector === '[id^="jobscan-"]' ? {} : selector.includes('button') ? SUBMIT : null)}});
  view.context.setRefusal(WORDS);
  await view.tick();
  assert.equal(reports(view).length, 0);
});

test('a receipt beats the words: it is recorded as applied, and no refusal goes out', async () => {
  const view = page();
  herSubmit(view);
  view.context.setRefusal(WORDS);
  view.context.setApplied(true);
  await view.tick();
  assert.equal(markCount(view), 1);
  assert.equal(reports(view).length, 0);
});

test('a receipt after a refusal still counts: a faked refusal must not hide a real send', async () => {
  const view = page();
  herSubmit(view);
  view.context.setRefusal(WORDS);
  await view.tick();
  view.context.setApplied(true);
  await view.tick();
  assert.equal(reports(view).length, 1);
  assert.equal(markCount(view), 1);
});

test('a report that will not save tries a few times, then tells her how to do it herself', async () => {
  const view = page({replies: {handRefusal: {ok: false, error: 'down'}}});
  herSubmit(view);
  view.context.setRefusal(WORDS);
  for (let i = 0; i < 8; i++) await view.tick();
  assert.equal(reports(view).length, 5);
  assert.match(view.logged(), /没记上/);
});

test('Filler\'s own click was already answered, so the watch does not report it again', async () => {
  for (const run of [view => runnerRun(view), async view => { view.button('.agent-submit').onclick(trusted); await flush(); }]) {
    const view = page({submit: {outcome: 'blocked', evidence: WORDS}});
    herSubmit(view);
    view.context.setRefusal(WORDS);
    await run(view);
    await view.tick();
    assert.equal(reports(view).length, 0);
  }
});

test('moving to another posting ends the watch: the words are about that one', async () => {
  const view = page();
  herSubmit(view);
  view.context.location.pathname = '/acme/2/application';
  view.context.setRefusal(WORDS);
  await view.tick();
  assert.equal(reports(view).length, 0);
});

test('the service worker sends the report to the hand-refusal route, signed, and only for a real tab', async () => {
  const requests = [], listeners = [];
  const context = vm.createContext({URL, console,
    fetch: async (url, init) => { requests.push({url, init, body: JSON.parse(init.body)}); return {ok: true, json: async () => ({ok: true})}; },
    chrome: {runtime: {onInstalled: {addListener() {}}, onMessage: {addListener(fn) { listeners.push(fn); }}},
      storage: {local: {get: async defaults => ({...defaults, jobscanPort: 8787, jobscanReviewTokens: {8787: 'fake-token'}})}}}});
  vm.runInContext(read('background.js'), context);
  const send = (message, sender) => new Promise(resolve => listeners[0](message, sender, resolve));
  const url = 'https://jobs.ashbyhq.com/acme/1/application';
  const message = {type: 'handRefusal', jobId: 'job-1', evidence: WORDS, url};
  assert.equal((await send(message, {tab: {id: 1}, frameId: 0, url})).ok, true);
  assert.equal(requests[0].url, 'http://127.0.0.1:8787/api/agent-lab/hand-refusal');
  assert.equal(requests[0].init.headers['X-JobScan-Review'], 'fake-token');
  assert.deepEqual({...requests[0].body}, {job_id: 'job-1', evidence: WORDS, url});
  assert.equal((await send(message, {id: 'popup'})).ok, false);
  assert.equal((await send(message, {tab: {id: 1}, frameId: 0, url: 'chrome-extension://x/runner.html'})).ok, false);
  assert.equal((await send(message, {tab: {id: 1}, frameId: 3, url})).ok, false, 'a frame inside the page has no say');
  assert.equal((await send(message, {tab: {id: 1}, frameId: 0, url: url + '?moved=1'})).ok, false, 'the tab moved on');
  assert.equal((await send({...message, url: undefined}, {tab: {id: 1}, frameId: 0, url})).ok, false);
  assert.equal(requests.length, 1);
});

// ---- a code step after the click ----
test('a code step after the click tells the stop panel, with whether the mail button is there', async () => {
  for (const mail of [true, false]) {
    let view;
    view = page({mail, submit: () => { view.context.JobScanPageGate.inspect = () => ({kind: 'verification'});
      return {outcome: 'blocked', evidence: '页面显示验证码已发送，并有验证码输入框；需要处理验证步骤。'}; }});
    await runnerRun(view);
    assert.equal(view.shown[0].args.clicked, true);
    assert.equal(view.shown[0].args.verification, true);
    assert.equal(view.shown[0].args.mailcode, mail);
  }
});

test('any other stop after a click is no code step, and neither is a code box with no click', async () => {
  let view = page({submit: {outcome: 'unknown', evidence: '没有读到回执。'}});
  await runnerRun(view);
  assert.equal(view.shown[0].args.verification, false);
  view = page();
  view.context.JobScanFormAgent.preflight = () => { view.context.JobScanPageGate.inspect = () => ({kind: 'verification'}); return {ok: false, reason: '仍有未读取控件或页面错误。'}; };
  await runnerRun(view);
  assert.equal(view.shown[0].args.clicked, false);
  assert.equal(view.shown[0].args.verification, false);
});

test('form_agent\'s preflight really answers in the words the watch looks for', () => {
  const agentSource = read('form_agent.js');
  const seen = vm.runInNewContext(`${section('  const RECEIPT_SEEN', '  function panel(role)')}; RECEIPT_SEEN`);
  const load = headings => {
    const element = {getClientRects: () => [{}], getAttribute: () => null, hasAttribute: () => false, closest: () => null};
    const heading = {...element, textContent: 'Your application was submitted.', matches: () => false};
    const context = {crypto: {randomUUID: () => 'doc'}, getComputedStyle: () => ({visibility: 'visible'}),
      location: {hostname: 'jobs.ashbyhq.com', pathname: '/acme/1/application', href: 'https://jobs.ashbyhq.com/acme/1/application'},
      document: {body: {innerText: 'Application'}, querySelectorAll: selector => (selector === 'h2' ? headings(heading) : [])}};
    vm.runInNewContext(agentSource, context);
    return context.JobScanFormAgent;
  };
  const shown = load(heading => [heading]), plain = load(() => []);
  assert.match(shown.preflight(shown.capture()).reason, seen);
  assert.doesNotMatch(plain.preflight(plain.capture()).reason || '', seen);
});

// ---- the fill itself ----
function fillHarness({labels = [], boxes = [], profile = {}, role = {}, runner = false, menu = () => [], reply = {index: -1}}) {
  const sent = [], clicked = [], logs = [];
  const control = {innerText: '', closest: () => null};
  const options = names => names.map(textContent => ({textContent, offsetParent: {}}));
  const inputs = labels.map(item => {
    const entry = {querySelector: () => ({className: item.required === false ? '_label' : '_required_x'})};
    return {label: item.label, value: '', placeholder: '', attrs: {'aria-autocomplete': 'list', 'aria-controls': 'menu', role: 'combobox'},
      getAttribute(name) { return this.attrs[name] ?? null; }, closest: selector => (selector.includes('_fieldEntry') ? entry : control),
      focus() {}, dispatchEvent() {}};
  });
  let active = null;
  const context = {
    console, Set, Map, RegExp, KeyboardEvent: class {}, CSS: {escape: s => s}, control,
    runnerDeadline: runner ? Date.now() + 60000 : 0, IS_ASHBY: true, DEMOGRAPHIC: /gender|race/i,
    document: {
      getElementById: id => (id === 'menu' ? {querySelectorAll: () => options(menu(active ? active.value : ''))} : null),
      querySelectorAll: selector => (selector === 'input[role=combobox]' ? inputs : selector.startsWith('textarea') ? boxes : []),
    },
    sleep: async () => {}, humanPause: async () => {}, checkDeadline() {}, isContextDead: () => false, escapeRe: s => s,
    setNativeValue: (el, value) => { el.value = value; active = el; },
    coordSeq: el => { if ('textContent' in el) { clicked.push(el.textContent); if (active) active.value = el.textContent; } },
    send: async message => { sent.push(message); return reply; },
    controlLabel: () => '', fillSelect: () => false, fillRadio: () => false, radioLabel: () => '', comboLabel: input => input.label,
    pickReactSelect: async () => ({ok: false}), pickAnyOption: async () => ({ok: false}),
    classify: () => null, isDropdownish: () => false, questionLabel: el => el.label, fieldText: () => '',
  };
  vm.createContext(context);
  const exclude = source.match(/^ {2}const EXCLUDE = .*$/m)[0];
  const where = source.match(/^ {2}const WHERE_SHE_IS = .*$/m)[0];
  const fn = source.indexOf('  function commonAnswer(');
  vm.runInContext(`const US_STATES = {}; ${exclude}\n${where}\n${source.slice(source.indexOf('  const clean = (s) =>'), source.indexOf('\n  }\n', fn) + 5)}
    ${section('  async function pickTypeahead(', '  // EEO: pick the option matching')}
    ${section('  // A required Ashby choice no rule can answer', '  // Fills one standard field at a time')}
    ${section('  async function fillCoverLetterBox(', '  // Desired salary.')}
    globalThis.fillCommon = fillCommon; globalThis.pickTypeahead = pickTypeahead; globalThis.fillCoverLetterBox = fillCoverLetterBox; globalThis.essaysHeld = essaysHeld;`, context);
  const fullRole = {job_id: 'j', ai_enabled: true, ...role};
  const fullProfile = {location: 'San Jose, CA', work_authorized: true, needs_sponsorship: false, ...profile};
  return {context, sent, clicked, logs, inputs, control,
    fill: () => context.fillCommon(fullProfile, text => logs.push(text), fullRole),
    cover: extra => context.fillCoverLetterBox({job_id: 'j', ai_enabled: true, auto_cover_letter: true, profile: fullProfile, ...extra}, text => logs.push(text))};
}
const filtering = names => typed => names.filter(name => name.toLowerCase().includes(typed.toLowerCase()));

test('an answer to the bare title fills nothing under a statement Filler can read on the page', async () => {
  const topic = '^\\W*acknowledgement\\W*(?:required\\W*)?$';
  const run = async statement => {
    const harness = fillHarness({labels: [{label: 'Acknowledgement'}], profile: {confirmed_answers: [{topic, answer: 'Yes', yesno: 'yes'}]}, role: {ai_enabled: false}, menu: filtering(['Yes', 'No'])});
    harness.context.JobScanFormAgent = {statementOf: () => statement};
    await harness.fill();
    return [...harness.clicked];
  };
  assert.deepEqual(await run(''), ['Yes'], 'the same bare title still gets it');
  assert.deepEqual(await run('I authorize this company to run a credit check.'), []);
});

// Ashby's Yes/No buttons, with the statement in the block above them.
function yesNoBlock(text, known) {
  const pressed = [];
  const block = {className: '_fieldEntry_x', innerText: `${text}\nYes\nNo`};
  const button = word => ({textContent: word, offsetParent: {}, className: '', parentElement: block, getAttribute: () => null, closest: () => null, click() { pressed.push(word); }});
  const context = vm.createContext({console, Map, RegExp, sleep: async () => {}, mayAskAI: () => false, document: {querySelectorAll: () => [button('Yes'), button('No')]}});
  const exclude = source.match(/^ {2}const EXCLUDE = .*$/m)[0];
  const fn = source.indexOf('  function commonAnswer(');
  vm.runInContext(`const US_STATES = {}; ${exclude}\n${source.slice(source.indexOf('  const clean = (s) =>'), source.indexOf('\n  }\n', fn) + 5)}
    ${section('  async function fillAshbyButtons(', '  // All Nth-entry fields for one suffix')}
    globalThis.fillAshbyButtons = fillAshbyButtons;`, context);
  return context.fillAshbyButtons({confirmed_answers: known}, () => {}, {}).then(() => pressed);
}
const noted = words => ({topic: `^\\W*${words.toLowerCase().match(/\w+/g).join('\\W*')}\\W*(?:required\\W*)?$`, answer: 'Yes', yesno: 'yes'});
test('a Yes/No statement keeps its own yes and no: only the buttons\' words come off the end', async () => {
  const own = noted('Family notice I have relatives working here.');
  assert.deepEqual(await yesNoBlock('Family notice\nI have relatives working here.', [own]), ['Yes']);
  assert.deepEqual(await yesNoBlock('Family notice\nI have no relatives working here.', [own]), [], 'that is the opposite statement');
  const says = noted('Notice: answer yes');
  assert.deepEqual(await yesNoBlock('Notice: answer yes', [says]), ['Yes'], 'its last word is not one of the buttons');
});

test('typing No into a filter that matches anywhere does not take "Yes, now"', async () => {
  const label = 'Will you now or in the future require immigration sponsorship?';
  const h = fillHarness({labels: [{label}], menu: filtering(['Yes, now', 'Yes, in the future', 'No'])});
  await h.fill();
  assert.deepEqual([...h.clicked], ['No']);
  assert.equal(h.inputs[0].value, 'No');
});

test('with no option starting with the typed word it stays blank, with nothing typed left behind', async () => {
  const h = fillHarness({labels: [{label: 'Will you now or in the future require immigration sponsorship?'}], menu: filtering(['Yes, now', 'Yes, in the future'])});
  await h.fill();
  assert.equal(h.clicked.length, 0);
  assert.equal(h.inputs[0].value, '');
  assert.equal(h.sent.length, 0, 'a rule that said No does not become a model question');
});

test('a plain Yes takes only an option starting with Yes', async () => {
  const label = 'Are you currently based in or open to relocating to the San Francisco Bay Area?';
  let h = fillHarness({labels: [{label}], menu: filtering(['No thanks', 'Yes, today'])});
  await h.fill();
  assert.deepEqual([...h.clicked], ['Yes, today']);
  h = fillHarness({labels: [{label}], menu: filtering(['Yesterday', 'Noon'])});
  await h.fill();
  assert.equal(h.clicked.length, 0, 'a word boundary, not a prefix');
  assert.equal(h.inputs[0].value, '');
});

test('the wrong sentence twin loses when a legal answer is typed', async () => {
  const h = fillHarness({labels: [{label: 'Are you authorized to work in the United States?'}],
    menu: filtering(['Yes, but I will require sponsorship', 'Yes, and I do not require sponsorship', 'No'])});
  await h.fill();
  assert.deepEqual([...h.clicked], ['Yes, and I do not require sponsorship']);
});

test('recording opt-out still lands when the list has no option starting with No', async () => {
  const h = fillHarness({labels: [{label: 'May we record your interview?'}], menu: filtering(['I consent', 'Opt out of recording'])});
  await h.fill();
  assert.deepEqual([...h.clicked], ['Opt out of recording']);
});

test('a place or a school still takes the first suggestion', async () => {
  const h = fillHarness({menu: () => ['San Jose, CA, USA', 'San Jose, Costa Rica']});
  const input = {value: '', getAttribute: name => (name === 'aria-controls' ? 'menu' : null), focus() {}, dispatchEvent() {}};
  const result = await h.context.pickTypeahead(h.control, input, 'San Jose');
  assert.equal(result.ok, true);
  assert.deepEqual([...h.clicked], ['San Jose, CA, USA']);
});

test('a place with no suggestion keeps its text, as it did: only a Yes/No or a model pick is cleared', async () => {
  const h = fillHarness({menu: () => []});
  const input = {value: '', getAttribute: name => (name === 'aria-controls' ? 'menu' : null), focus() {}, dispatchEvent() {}};
  assert.equal((await h.context.pickTypeahead(h.control, input, 'San Jose')).ok, false);
  assert.equal(input.value, 'San Jose');
  assert.equal((await h.context.pickTypeahead(h.control, input, 'No')).ok, false);
  assert.equal(input.value, '');
});

test('a label saying "based" types the city only when it asks where she is', async () => {
  let h = fillHarness({labels: [{label: 'Are you currently based in or open to relocating to the San Francisco Bay Area?'}], menu: filtering(['Yes', 'No'])});
  await h.fill();
  assert.deepEqual([...h.clicked], ['Yes']);
  h = fillHarness({labels: [{label: 'Where are you currently based?'}], menu: () => ['San Jose, CA, USA']});
  await h.fill();
  assert.deepEqual([...h.clicked], ['San Jose, CA, USA']);
  assert.equal(h.inputs[0].value, 'San Jose, CA, USA');
  for (const label of ['Current location', 'Location', 'City', 'Where do you live?']) {
    h = fillHarness({labels: [{label}], menu: () => ['San Jose, CA, USA']});
    await h.fill();
    assert.deepEqual([...h.clicked], ['San Jose, CA, USA'], label);
  }
});

test('a label naming an office location is not asking where she is', async () => {
  const label = 'This role is tied to the office location listed in the job posting. Which of these fits?';
  const names = ['Yes, I am based there', 'No, I am remote'];
  const h = fillHarness({labels: [{label}], menu: filtering(names), reply: {index: 0}});
  await h.fill();
  assert.equal(h.sent.length, 1, 'goes to the model instead of typing a city');
  assert.ok(!h.sent[0].options.includes('San Jose'));
});

test('an autocomplete with no rule reaches the model as a radio does, with the whole list', async () => {
  const names = ['Blue', 'Green', 'Red'];
  const h = fillHarness({labels: [{label: 'Favorite colour'}], menu: filtering(names), reply: {index: 1}});
  await h.fill();
  assert.equal(h.sent[0].type, 'pickOption');
  assert.deepEqual([...h.sent[0].options].sort(), names);
  assert.deepEqual([...h.clicked], ['Green']);
  assert.equal(h.inputs[0].value, 'Green');
});

test('a list that opens on click is sent as it stands, with no letters typed', async () => {
  const names = ['Blue', 'Green'];
  const h = fillHarness({labels: [{label: 'Favorite colour'}], menu: typed => (typed ? filtering(names)(typed) : names), reply: {index: 0}});
  await h.fill();
  assert.deepEqual([...h.sent[0].options], names);
  assert.deepEqual([...h.clicked], ['Blue']);
});

test('a prefix-only filter still gives the model every option', async () => {
  const names = ['Green', 'Other', 'Zebra'];
  const h = fillHarness({labels: [{label: 'Favorite colour'}], menu: typed => (typed ? names.filter(name => name.toLowerCase().startsWith(typed.toLowerCase())) : []), reply: {index: 2}});
  await h.fill();
  assert.deepEqual([...h.sent[0].options].sort(), names);
});

test('the model declining leaves it blank', async () => {
  const h = fillHarness({labels: [{label: 'Favorite colour'}], menu: filtering(['Blue']), reply: {index: -1}});
  await h.fill();
  assert.equal(h.clicked.length, 0);
  assert.equal(h.inputs[0].value, '');
});

test('a model pick the list no longer offers is not replaced by the first suggestion', async () => {
  const names = ['Blue', 'Green'];
  let calls = 0;
  const h = fillHarness({labels: [{label: 'Favorite colour'}], reply: {index: 1},
    menu: typed => { calls++; return typed.length > 1 ? ['Blue'] : names.filter(name => name.toLowerCase().includes(typed.toLowerCase())); }});
  await h.fill();
  assert.ok(calls);
  assert.equal(h.clicked.length, 0);
  assert.equal(h.inputs[0].value, '');
});

test('the model is not asked for an optional, sensitive or AI-off question, or for a rule that said Yes or No', async () => {
  const menu = filtering(['Maybe']);
  for (const options of [{labels: [{label: 'Favorite colour', required: false}]}, {labels: [{label: 'Desired salary'}]},
    {labels: [{label: 'Favorite colour'}], role: {ai_enabled: false}}, {labels: [{label: 'Are you able to commute to the office?'}]}]) {
    const h = fillHarness({...options, menu, reply: {index: 0}});
    await h.fill();
    assert.equal(h.sent.length, 0, options.labels[0].label);
  }
});

test('a batch skips optional free text unless essays are allowed; required ones and attended fills are untouched', async () => {
  const box = (label, required) => ({label, value: '', disabled: false, readOnly: false, required,
    getAttribute: () => null, closest: () => null});
  const boxes = [box('Preferred Pronouns', false), box('Why do you want to work here?', true), box('Anything else?', false)];
  const asked = async options => (await fillHarness({boxes, ...options}).fill()).map(item => item.label);
  assert.deepEqual([...await asked({runner: true})], ['Why do you want to work here?']);
  assert.deepEqual([...await asked({runner: true, profile: {allow_unread_essays: true}})], boxes.map(item => item.label));
  assert.deepEqual([...await asked({})], boxes.map(item => item.label));
});

test('Ashby marks a required box on the label, and aria-required counts', async () => {
  const entry = {querySelector: () => ({className: '_heading _required_f7'})};
  const boxes = [{label: 'On the label', value: '', getAttribute: () => null, closest: () => entry},
    {label: 'By aria', value: '', getAttribute: name => (name === 'aria-required' ? 'true' : null), closest: () => null}];
  const left = await fillHarness({boxes, runner: true}).fill();
  assert.deepEqual([...left].map(item => item.label), ['On the label', 'By aria']);
});

test('a batch leaves the cover letter box alone unless essays are allowed', async () => {
  const box = {label: 'Additional information', value: '', offsetParent: {}, focus() {}, blur() {}};
  const withBox = options => {
    const h = fillHarness(options);
    h.context.document.querySelectorAll = selector => (selector === 'textarea' ? [box] : []);
    return h;
  };
  let h = withBox({runner: true});
  await h.cover();
  assert.equal(h.sent.length, 0);
  h = withBox({runner: true, profile: {allow_unread_essays: true}, reply: {ok: false, error: 'x'}});
  await h.cover();
  assert.equal(h.sent[0].type, 'coverLetter');
  h = withBox({reply: {ok: false, error: 'x'}});
  await h.cover();
  assert.equal(h.sent[0].type, 'coverLetter');
});

test('a batch ticks no consent box; her own Fill still does', () => {
  const box = {type: 'checkbox', checked: false, disabled: false, offsetParent: {}, name: 'i_agree', id: '', innerText: 'I agree', parentElement: null,
    closest: () => null, click() { this.checked = true; }};
  const context = {document: {querySelectorAll: () => [box], querySelector: () => null}, CSS: {escape: s => s}, runnerDeadline: Date.now() + 1000};
  vm.createContext(context);
  vm.runInContext(`${section('  const CONSENT_RE', '  function wdBlur()')}; globalThis.tickConsents = tickConsents;`, context);
  assert.equal(context.tickConsents(() => {}), 0);
  assert.equal(box.checked, false);
  context.runnerDeadline = 0;
  assert.equal(context.tickConsents(() => {}), 1);
  assert.equal(box.checked, true);
});

// ---- a legal answer never falls back to a first-word click ----
function choosers() {
  const clicked = [], context = {document: {querySelector: () => null}, CSS: {escape: s => s}, Event: class {}, clicked};
  vm.createContext(context);
  const exclude = source.match(/^ {2}const EXCLUDE = .*$/m)[0];
  const fn = source.indexOf('  function commonAnswer(');
  vm.runInContext(`const US_STATES = {}; ${exclude}\n${source.slice(source.indexOf('  const clean = (s) =>'), source.indexOf('\n  }\n', fn) + 5)}
    ${section('  function fillSelect(', '  // Best-effort for custom dropdowns')}
    globalThis.commonAnswer = commonAnswer; globalThis.fillRadio = fillRadio; globalThis.fillSelect = fillSelect;`, context);
  return context;
}
const AUTH_LABEL = 'Are you authorized to work in the United States?', profileFacts = {work_authorized: true, needs_sponsorship: false};

test('fillRadio and fillSelect click no option claiming a status, though it starts with the right word', () => {
  const c = choosers();
  const radio = text => ({value: text, getAttribute: () => null, closest: () => ({innerText: text}), click() { c.clicked.push(text); }});
  const select = texts => ({options: texts.map(text => ({text, value: text})), dispatchEvent() {}, value: ''});
  const auth = c.commonAnswer(AUTH_LABEL, profileFacts), spon = c.commonAnswer('Will you now or in the future require sponsorship?', profileFacts);
  assert.equal(c.fillRadio(['Yes - I have a green card', 'No'].map(radio), auth), false);
  assert.equal(c.fillRadio(['Yes', 'No - I have a green card'].map(radio), spon), false);
  assert.equal(c.clicked.length, 0);
  assert.equal(c.fillSelect(select(['Yes - GC', 'No']), auth), false);
  assert.equal(c.fillRadio(['Yes', 'No'].map(radio), auth), true);
  assert.deepEqual([...c.clicked], ['Yes']);
  // A plain rule's Yes still clicks by its first word: only legal answers carry a status.
  c.clicked.length = 0;
  assert.equal(c.fillRadio(['Yes, I can', 'No'].map(radio), {yesno: 'yes'}), true);
  assert.deepEqual([...c.clicked], ['Yes, I can']);
});

test('a react-select legal answer with no score clicks nothing', async () => {
  const clicked = [], options = ['Yes - GC', 'No'].map(textContent => ({textContent}));
  const input = {getAttribute: name => (name === 'aria-controls' ? 'menu' : null), dispatchEvent() {}};
  const context = {document: {getElementById: () => ({querySelectorAll: () => options})}, KeyboardEvent: class {}, sleep: async () => {},
    coordSeq: el => { if (el.textContent) clicked.push(el.textContent); }};
  vm.createContext(context);
  vm.runInContext(`${section('  async function pickReactSelect(', '  // Pick the FIRST real option')}\nglobalThis.pick = pickReactSelect;`, context);
  const control = {innerText: ''};
  assert.equal((await context.pick(control, input, /^\s*yes\b/i, () => null)).ok, false);
  assert.equal(clicked.length, 0);
  assert.equal((await context.pick(control, input, /^\s*no\b/i, null)).ok, true, 'a plain answer still takes its word');
  assert.deepEqual(clicked, ['No']);
});

test('a text box gets a rule\'s bare Yes or No only under a yes/no question', async () => {
  const box = label => ({label, value: '', disabled: false, readOnly: false, getAttribute: () => null, closest: () => null, focus() {}, blur() {}});
  const boxes = [box('Marketing opt-in'), box('Are you at least 18 years of age?'), box('Commute distance (miles)')];
  const h = fillHarness({boxes});
  const left = await h.fill();
  assert.deepEqual(boxes.map(item => item.value), ['', 'Yes', '']);
  assert.deepEqual([...left].map(item => item.label), ['Marketing opt-in', 'Commute distance (miles)']);
});
