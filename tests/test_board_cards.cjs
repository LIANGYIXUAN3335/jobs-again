// What the owner reads on the board, run through the real page script over a small stand-in for the DOM.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), test = require('node:test'), vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/agent_runner.js'), 'utf8');
const template = fs.readFileSync(path.join(__dirname, '../templates/agent_runner.html'), 'utf8');
const IDS = [...template.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);

class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ''; this._text = ''; this.value = ''; this.hidden = false; this.dataset = {}; this.style = {}; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...nodes) { for (const node of nodes) this.children.push(typeof node === 'string' ? Object.assign(new Node('#text'), {_text: node}) : node); }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  setAttribute() {}
  querySelector() { return null; }
  get childElementCount() { return this.children.length; }
  walk() { return [this, ...this.children.flatMap(child => child.walk())]; }
}

function board(routes = {}) {
  const nodes = Object.fromEntries(IDS.map(id => [id, new Node('div')]));
  const calls = [], reply = {
    'runner/status': {phase: 'paused', pause_reason: '', jobs: [], events: [], worker: {online: true, version: '1', expected_version: '1', identity: 'casey'}},
    answers: {batch: [], paused: [], answers: []},
    preflight: {counts: {ready: 0, essay: 0, ask: 0, never: 0, unread: 0}, companies: {ready: 0, ask: 0}, questions: [], more_questions: 0, never: [], allow_unread_essays: false, reading: false, error: '', model_warning: ''},
    interviews: {calendar: {age_days: 0.1, stale: false}, blocked: [], allowed: [], recruiter_only: [], unknown: []},
    logins: {logins: [], sites: []},
    'runner/start': {}, 'runner/retry': {}, 'runner/requeue': {requeued: 0}, 'requeue-places': {runnable: [], cannot: 0},
    ...routes,
  };
  const context = {
    document: {
      getElementById: id => { assert.ok(nodes[id], `the template has no #${id}`); return nodes[id]; },
      querySelector: () => ({content: 'session'}),
      querySelectorAll: selector => Object.values(nodes).flatMap(node => node.walk()).filter(node => selector === '.retry' && node.className.split(' ').includes('retry')),
      createElement: tag => new Node(tag),
    },
    fetch: async (url, init = {}) => {
      const route = url.replace('/api/agent-lab/', '');
      calls.push({route, body: init.body ? JSON.parse(init.body) : undefined});
      const data = typeof reply[route] === 'function' ? reply[route]() : reply[route];
      return {status: 200, ok: true, json: async () => data ?? {}};
    },
    sessionStorage: {getItem: () => null, setItem() {}, removeItem() {}}, location: {reload() {}},
    setInterval: () => 0, setTimeout: () => 0, clearTimeout: () => {}, confirm: () => true, window: {open() {}}, Intl, Date, encodeURIComponent, JSON, Number, String,
  };
  vm.runInNewContext(source, context);
  const texts = (...ids) => ids.flatMap(id => nodes[id].walk()).map(node => node.textContent);
  const button = (id, label) => nodes[id].walk().find(node => node.tag === 'button' && node.textContent === label);
  return {nodes, calls, reply, texts, button, settle: async () => { for (let i = 0; i < 12; i++) await new Promise(setImmediate); }};
}

const STOPPED = {job_id: 'j', company: 'Crusoe', title: 'Engineer', status: 'needs_user', evidence: '必填的居住州为空。', questions: [], page_issues: [], note: '', can_retry: true, finished_at: 1};
const answers = (paused, batch = []) => ({batch, paused, answers: []});

test('the retry button says only what it does and appears only where it can work', async () => {
  const fixed = {...STOPPED, note: '这次停下之后程序修过了，现在这张表没有要你答的题。不用答，直接点重跑。'};
  const held = {...STOPPED, job_id: 'k', company: 'Applied Intuition', note: '正在技术面，这家公司先不投。想投就到下面「正在面的公司」放行。', can_retry: false};
  const page = board({answers: answers([fixed, held])});
  await page.settle();
  const buttons = page.nodes.paused.walk().filter(node => node.tag === 'button');
  assert.deepEqual(buttons.map(node => node.textContent), ['重跑这一份']);
  assert.ok(!source.includes('用新答案重跑'), 'no answer is needed to press it');
  assert.ok(page.texts('paused').some(text => text === fixed.note));
  assert.ok(page.texts('paused').some(text => text === held.note));
  assert.ok(!page.texts('paused').some(text => text === STOPPED.evidence), 'the old reason does not sit beside what is true now');
});

test('a stop with no note still shows why it stopped', async () => {
  const page = board({answers: answers([STOPPED])});
  await page.settle();
  assert.ok(page.texts('paused').includes(STOPPED.evidence));
});

test('retry and start read the forms again so the reading text appears', async () => {
  const page = board({answers: answers([STOPPED]), preflight: () => ({...board().reply.preflight, reading: true})});
  await page.settle();
  const seen = () => page.calls.filter(call => call.route === 'preflight').length;
  let before = seen();
  page.button('paused', '重跑这一份').onclick();
  await page.settle();
  assert.ok(page.calls.some(call => call.route === 'runner/retry') && seen() > before);
  assert.match(page.nodes['preflight-summary'].textContent, /正在读表单/);
  before = seen();
  page.nodes.start.onclick();
  await page.settle();
  assert.ok(page.calls.some(call => call.route === 'runner/start') && seen() > before);
});

test('while the forms are read an idle batch says so, not that it waits for a resume', async () => {
  const idle = {...board().reply['runner/status'], phase: 'idle', enabled: true};
  const page = board({'runner/status': idle, preflight: {...board().reply.preflight, reading: true}});
  await page.settle();
  page.nodes.pause.onclick();   // any later status read
  await page.settle();
  assert.equal(page.nodes.state.textContent, '正在读表单，读完就开始');
});

test('the batch card says when the batch began', async () => {
  const at = new Date(2026, 10, 5, 23, 11).getTime() / 1000;
  const page = board({'runner/status': {...board().reply['runner/status'], started_at: at}, answers: answers([], [{job_id: 'j', company: 'A', title: 'B', status: 'needs_user', evidence: 'x', clicked: false, unanswered: 0, note: ''}])});
  await page.settle();
  const line = page.nodes['batch-started'].textContent;
  assert.match(line, /开始/);
  assert.match(line, /23:11/);
});

const row = changes => ({job_id: 'j', company: 'A', title: 'B', status: 'blocked', evidence: 'x', clicked: false, needs_code: false, unanswered: 0, note: '', ...changes});

test('a stopped row tells what is true now, not to answer and rerun', async () => {
  const page = board({answers: answers([], [row({status: 'needs_user', note: '这份已经不是待投状态了（比如你自己投完了），不用再管。'})])});
  await page.settle();
  const texts = page.texts('batch-rows');
  assert.ok(texts.some(text => text.includes('不用再管')));
  assert.ok(!texts.some(text => text.includes('答完点重跑')));
});

test('a click that stopped on an email code says to enter it on that page', async () => {
  const page = board({answers: answers([], [row({clicked: true, needs_code: true, evidence: '页面显示验证码已发送，并有验证码输入框；需要处理验证步骤。'})])});
  await page.settle();
  const texts = page.texts('batch-rows');
  assert.ok(texts.some(text => text.includes('Get email code') && text.includes('再点一次提交')));
  assert.ok(texts.some(text => text.includes('等验证码')));
  assert.ok(!texts.some(text => text.startsWith('已经点过提交，不能重跑')));
});

test('any other stop after a click still warns against a second send', async () => {
  const page = board({answers: answers([], [row({clicked: true})])});
  await page.settle();
  assert.ok(page.texts('batch-rows').some(text => text.startsWith('已经点过提交，不能重跑')));
});

const question = changes => ({question: 'Acknowledgement', description: 'I acknowledge the GDPR notice.', options: ['Yes', 'No'], forms: 3, companies: 2, job_id: 'j', company: 'Acme', ...changes});
const preflightWith = (questions, more = {}) => ({...board().reply.preflight, questions, ...more});

test('a statement only the application page shows gets no answer buttons on the card, and a way to the page', async () => {
  const page = board({preflight: preflightWith([question({question: 'GDPR Acknowledgement', description: '', options: ['I Acknowledge/Accept this Statement'], on_page: true, url: 'https://jobs.ashbyhq.com/Illumio/1/application'})])});
  await page.settle();
  const shown = page.nodes['preflight-questions'].walk();
  assert.equal(shown.some(node => node.tag === 'button' || node.tag === 'textarea'), false, 'nothing to press: she cannot see what it would sign');
  assert.ok(shown.some(node => node.textContent.includes('只写在申请页上')));
  assert.ok(shown.some(node => node.tag === 'a' && node.href === 'https://jobs.ashbyhq.com/Illumio/1/application'));
});

test('a code stop she finished by hand reads as sent, not as a page to go back to', async () => {
  const row = {job_id: 'j', company: 'Crusoe', title: 'Engineer', status: 'needs_user', evidence: '页面显示验证码已发送，并有验证码输入框；需要处理验证步骤。', clicked: true, needs_code: false, went_out: true, note: '', unanswered: 0};
  const page = board({answers: answers([], [row])});
  await page.settle();
  const shown = page.texts('batch-rows').join('\n');
  assert.match(shown, /已投出/);
  assert.doesNotMatch(shown, /等验证码|再点一次提交|卡在/);
  assert.match(page.nodes['batch-summary'].textContent, /独立投出 1 份/);
  assert.match(page.nodes['batch-summary'].textContent, /停下 0 份/);
});

test('a refusal on a stopped job counts as one in the summary and says nothing waits', async () => {
  const row = {job_id: 'j', company: 'Crusoe', title: 'Engineer', status: 'needs_user', evidence: '雇主拒收：You have already applied.', clicked: false, needs_code: false, went_out: false, note: '雇主说不收这份，这家公司已经按它写的时间冷却了。不用再管。', unanswered: 0};
  const page = board({answers: answers([], [row])});
  await page.settle();
  assert.match(page.nodes['batch-summary'].textContent, /雇主拒收 1 份/);
  assert.match(page.nodes['batch-summary'].textContent, /停下 0 份/);
  assert.match(page.texts('batch-rows').join('\n'), /不用再管/);
});

test('the question card shows the statement, says what the count is, and saves the statement with the answer', async () => {
  const page = board({preflight: preflightWith([question()])});
  await page.settle();
  const shown = page.nodes['preflight-questions'].walk();
  const hint = shown.find(node => node.className === 'hint');
  assert.equal(hint.textContent, 'I acknowledge the GDPR notice.');
  const texts = shown.map(node => node.textContent);
  assert.ok(texts.includes('有 3 份带这道题（2 家）'));
  assert.ok(!source.includes('答了能放行'), 'answering does not always unlock a form');
  shown.find(node => node.tag === 'button' && node.textContent === 'Yes').onclick();
  await page.settle();
  const saved = page.calls.find(call => call.route === 'answers' && call.body);
  assert.deepEqual(JSON.parse(JSON.stringify(saved.body)), {question: 'Acknowledgement', description: 'I acknowledge the GDPR notice.', answer: 'Yes', job_id: 'j', company: 'Acme'});
});

test('the stuck card saves the statement too, and each statement keeps its own draft', async () => {
  const q = {label: 'Acknowledgement', description: 'First.', kind: 'textarea', options: [], value: '', reason: '必填', answer: ''};
  const page = board({answers: answers([{...STOPPED, questions: [q, {...q, description: 'Second.'}]}])});
  await page.settle();
  const boxes = page.nodes.paused.walk().filter(node => node.tag === 'textarea');
  boxes[0].value = 'Yes';
  boxes[0].oninput();
  boxes[1].value = 'No';
  boxes[1].oninput();
  page.nodes.paused.walk().filter(node => node.tag === 'button' && node.textContent === '记住')[1].onclick();
  await page.settle();
  const saved = page.calls.find(call => call.route === 'answers' && call.body);
  assert.equal(saved.body.description, 'Second.');
  assert.equal(saved.body.answer, 'No');
});

test('a kept answer shows the statement it covers', async () => {
  const page = board({answers: {batch: [], paused: [], answers: [{id: 'q', question: 'Acknowledgement', description: 'I acknowledge the GDPR notice.', answer: 'Yes', company: ''}]}});
  await page.settle();
  assert.ok(page.texts('answers').includes('I acknowledge the GDPR notice.'));
});

test('the switch is called below, where it is', async () => {
  const page = board({preflight: preflightWith([], {counts: {ready: 1, essay: 13, ask: 0, never: 0, unread: 0}})});
  await page.settle();
  assert.match(page.nodes['preflight-summary'].textContent, /等你打开下面的开关/);
});

test('the requeue note counts only what can run and says where it lands', async () => {
  const run = async places => {
    const page = board({'requeue-places': places, 'runner/requeue': {requeued: 3}});
    await page.settle();
    page.nodes.requeue.onclick();
    await page.settle();
    return page.nodes['preflight-note'].textContent;
  };
  const far = await run({runnable: [{company: 'Crusoe', title: 'E', place: 14}], cannot: 2});
  assert.match(far, /放回了 1 份能跑的/);
  assert.match(far, /Crusoe 排第 14 个/);
  assert.match(far, /一轮至少投 14 份/);
  assert.match(far, /重跑这一份/);
  assert.match(far, /另外 2 份现在跑不了/);
  assert.doesNotMatch(far, /放回了 3 份/);
  assert.match(await run({runnable: [{company: 'A', title: 'E', place: 25}], cannot: 0}), /一轮最多 20 份/);
  const first = await run({runnable: [{company: 'A', title: 'E', place: 1}], cannot: 0});
  assert.doesNotMatch(first, /至少投|重跑这一份/);
  assert.match(await run({runnable: [], cannot: 3}), /没有能跑的可以放回。另外 3 份/);
});

test('a seed calendar says it is a copy, links to settings and is not called merely old', async () => {
  const note = '这份日历是副本，只有 candidate 这一个账号的。重新授权 Google 之前，新排的面试看不到。';
  const page = board({interviews: {...board().reply.interviews, calendar: {age_days: 0.1, stale: true, source: 'seed', copy_note: note}}});
  await page.settle();
  const summary = page.nodes['interviews-summary'];
  assert.ok(summary.textContent.includes(note));
  assert.doesNotMatch(summary.textContent, /太旧了|今天读过/);
  assert.equal(summary.walk().find(node => node.tag === 'a').href, '/settings');
});

test('a calendar read from Google keeps saying how old it is', async () => {
  const page = board({interviews: {...board().reply.interviews, calendar: {age_days: 9, stale: true, source: 'google', copy_note: ''}}});
  await page.settle();
  assert.match(page.nodes['interviews-summary'].textContent, /日历是 9 天前读的，太旧了/);
});

test('the start hint names every way a whole batch stops', () => {
  const hint = template.match(/<p class="hint start-hint">([^<]*)<\/p>/)[1];
  for (const words of ['结果不明', '程序出错', '连续 3 份', '垃圾申请', '申请太多', '15 分钟']) assert.ok(hint.includes(words), words);
});

test('the logins card says a batch never uses them', () => {
  assert.ok(template.includes('自动批量不会用这些账号，只有你手动申请时才用'));
});

test('a long unbroken word in a hint cannot push the page sideways', () => {
  assert.match(template, /\.hint\{[^}]*overflow-wrap:anywhere/);
});

test('the stopped card says it is not only about answering', () => {
  const hint = template.match(/<h2>需要你处理<\/h2><p class="hint">([^<]*)<\/p>/)[1];
  assert.ok(hint.includes('重跑这一份') && hint.includes('都还没提交'));
});
