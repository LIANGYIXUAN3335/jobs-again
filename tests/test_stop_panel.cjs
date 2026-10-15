// The panel a stopped application leaves on the page: what it says decides whether she sends a form twice.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), test = require('node:test'), vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../extension/stop_panel.js'), 'utf8');

class Fake {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ''; this.textContent = ''; this.value = ''; this.style = {}; }
  append(...nodes) { for (const node of nodes) { if (typeof node === 'string') { this.textContent += node; continue; } node.parent = this; this.children.push(node); } }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); this.parent = null; }
  attachShadow(init) { this.init = init; this.root = new Fake('#shadow'); return this.root; }
  closest() { return null; }
  // Everything in this node, depth first, shadow root included.
  walk() { return [this, ...this.children.flatMap(node => node.walk()), ...(this.root ? this.root.walk() : [])]; }
}

function open(args = {}) {
  const body = new Fake('body');
  const context = {document: {body, documentElement: new Fake('html'), getElementById: () => null, createElement: tag => new Fake(tag)}};
  vm.runInNewContext(source, context);
  const sent = [], send = async message => { sent.push(message); return {ok: true}; };
  const handle = context.JobScanStopPanel.show({company: 'Acme · Engineer', reason: '没有读到明确成功回执。', items: [], pageIssues: [],
    elementsFor: () => [], send, ...args});
  const host = body.children[0], nodes = () => host.walk();
  return {handle, host, sent, nodes,
    texts: () => nodes().map(node => node.textContent).filter(Boolean),
    button: text => nodes().find(node => node.tag === 'button' && node.textContent === text)};
}

const rows = args => {
  const context = {document: {}};
  vm.runInNewContext(source, context);
  return JSON.parse(JSON.stringify(context.JobScanStopPanel.rowsFrom({jobId: 'j', ...args})));
};
const asked = (id, label, description = '') => ({id, label, description, kind: 'textarea', value: '', options: []});

test('a field with two complaints is one question with one box', () => {
  const {items} = rows({fields: [asked('a', 'Why us?')], issues: [{field_id: 'a', reason: '必填项未填写。'}, {field_id: 'a', reason: '网页校验未通过。'}]});
  assert.equal(items.length, 1);
  assert.equal(items[0].reason, '必填项未填写。；网页校验未通过。');
  assert.equal(items[0].answerable, true);
});

test('a title the form repeats is one row with no box, even when only some of the rows complain', () => {
  const fields = [asked('a', 'Education History'), asked('b', 'Education History'), asked('c', 'Education History'), asked('d', 'Education History')];
  const both = rows({fields, issues: fields.flatMap(field => [{field_id: field.id, reason: '必填'}, {field_id: field.id, reason: '校验'}])});
  assert.equal(both.items.length, 1);
  assert.equal(both.items[0].answerable, false);
  assert.deepEqual(both.items[0].also, ['b', 'c', 'd']);
  const one = rows({fields, issues: [{field_id: 'b', reason: '必填'}]});
  assert.equal(one.items.length, 1);
  assert.equal(one.items[0].answerable, false, 'the other three rows are still on the form');
});

test('two statements under one title are two questions, and a complaint about the page has no row', () => {
  const fields = [asked('a', 'Acknowledgement', 'First.'), asked('b', 'Acknowledgement', 'Second.')];
  const {items, pageIssues} = rows({fields, issues: [{field_id: 'a', reason: 'x'}, {field_id: 'b', reason: 'y'}, {field_id: '__page__', reason: '有验证码'}]});
  assert.deepEqual(items.map(item => [item.description, item.answerable]), [['First.', true], ['Second.', true]]);
  assert.deepEqual(pageIssues, ['有验证码']);
});

test('a repeated row says to fill it in here and has no answer controls', () => {
  const item = {field_id: 'a', also: ['b'], job_id: 'j', label: 'Education History', description: '', reason: '必填', value: '', options: ['January'], answerable: false, repeated: true};
  const outlined = [];
  const panel = open({items: [item], elementsFor: id => (outlined.push(id), [])});
  assert.ok(panel.texts().some(text => text.includes('请在这页上手动填')));
  assert.equal(panel.button('January'), undefined);
  assert.equal(panel.nodes().some(node => node.tag === 'textarea'), false);
  assert.ok(outlined.includes('a') && outlined.includes('b'), 'all the rows are marked on the page');
});

test('the answer goes back with its statement, so two statements under one title stay apart', async () => {
  const item = {field_id: 'a', job_id: 'j', label: 'Acknowledgement', description: 'Second.', reason: '必填', value: '', options: ['Yes', 'No'], answerable: true};
  const panel = open({items: [item]});
  panel.button('Yes').onclick({isTrusted: true});
  await new Promise(setImmediate);
  assert.equal(panel.sent[0].body.description, 'Second.');
});

test('before any click the panel says not submitted, invites the hand submit and offers the re-run', () => {
  const panel = open();
  assert.ok(panel.texts().includes('这份停下了，还没有提交'));
  assert.ok(panel.texts().some(text => text.includes('自己填完并提交')));
  assert.ok(panel.button('答完了，去看板重跑这份'));
});

test('after a click it says it may be sent, and neither invites a second send nor offers a re-run', () => {
  const panel = open({clicked: true});
  assert.ok(panel.texts().includes('可能已投出，别再提交'));
  assert.ok(!panel.texts().includes('这份停下了，还没有提交'));
  assert.ok(!panel.texts().some(text => text.includes('自己填完并提交') || text.includes('I submitted this')));
  assert.equal(panel.button('答完了，去看板重跑这份'), undefined);
  assert.ok(panel.texts().includes('没有读到明确成功回执。'), 'she needs to know what the page showed');
});

test('a code step after the click says to enter the code and press Submit again, and names the button', () => {
  const panel = open({clicked: true, verification: true, reason: '页面显示验证码已发送，并有验证码输入框；需要处理验证步骤。'});
  const all = panel.texts().join('\n');
  assert.match(all, /输入验证码/);
  assert.match(all, /再点一次提交/);
  assert.ok(all.includes('📧 Get email code'));
  assert.doesNotMatch(all, /别再提交|不要在这页再提交|也不要重跑/);
  assert.ok(panel.texts().includes('页面显示验证码已发送，并有验证码输入框；需要处理验证步骤。'));
  assert.equal(panel.button('答完了，去看板重跑这份'), undefined, 'a re-run could send it twice');
});

test('with no mail reader set up it does not point at a button that is not there', () => {
  const all = open({clicked: true, verification: true, mailcode: false}).texts().join('\n');
  assert.match(all, /去邮箱/);
  assert.match(all, /再点一次提交/);
  assert.ok(!all.includes('Get email code'));
});

test('every other stop after a click keeps saying not to send it again', () => {
  const all = open({clicked: true, verification: false}).texts().join('\n');
  assert.match(all, /别再提交/);
  assert.match(all, /不要在这页再提交/);
  assert.ok(!all.includes('Get email code'));
});

test('once the employer has turned down her hand submit, nothing invites a re-run', () => {
  const panel = open();
  panel.handle.refused('雇主说不收这份。');
  assert.ok(panel.texts().includes('雇主没有收这份'));
  assert.ok(panel.texts().includes('雇主说不收这份。'));
  assert.ok(!panel.texts().some(text => text.includes('自己填完并提交')));
  assert.equal(panel.button('答完了，去看板重跑这份'), undefined);
});

test('after a click the reason still shows beside a question left over from the last review', () => {
  const item = {field_id: 'f', job_id: 'j', label: 'Why us?', reason: '空白', value: '', options: [], answerable: true};
  assert.ok(open({clicked: true, items: [item]}).texts().includes('没有读到明确成功回执。'));
  assert.ok(!open({items: [item]}).texts().includes('没有读到明确成功回执。'));
});

test('what a field holds is shown, never prefilled as her answer', async () => {
  const item = {field_id: 'f', job_id: 'j', label: 'Pronouns', reason: '模型写的', value: 'model text', options: [], answerable: true};
  const panel = open({items: [item]});
  const box = panel.nodes().find(node => node.tag === 'textarea');
  assert.equal(box.value, '');
  assert.ok(panel.texts().some(text => text.includes('model text')));
  panel.button('记住').onclick({isTrusted: true});
  assert.equal(panel.sent.length, 0, 'an empty box remembers nothing');
  box.value = 'she/her';
  panel.button('记住').onclick({isTrusted: true});
  await new Promise(setImmediate);
  assert.equal(panel.sent[0].body.answer, 'she/her');
});

test('a long field value does not swallow the panel', () => {
  const item = {field_id: 'f', job_id: 'j', label: 'Why?', reason: '', value: 'x'.repeat(5000), options: [], answerable: true};
  const line = open({items: [item]}).texts().find(text => text.startsWith('页面上现在是'));
  assert.ok(line.length < 300);
});

test('a script on the page can neither read the root nor press a button', async () => {
  const panel = open();
  assert.equal(panel.host.init.mode, 'closed');
  panel.button('答完了，去看板重跑这份').onclick({isTrusted: false});
  panel.button('×').onclick({isTrusted: false});
  await new Promise(setImmediate);
  assert.equal(panel.sent.length, 0);
  assert.ok(panel.host.parent, 'still on the page');
  panel.button('答完了，去看板重跑这份').onclick({isTrusted: true});
  await new Promise(setImmediate);
  assert.equal(panel.sent[0].type, 'openBoardAndClose');
});

test('once the employer has confirmed her hand submit, nothing invites another', () => {
  const panel = open();
  panel.handle.applied('读到了雇主的成功回执，已自动记为已投。');
  assert.ok(panel.texts().includes('这份已经投出'));
  assert.ok(panel.texts().includes('读到了雇主的成功回执，已自动记为已投。'));
  assert.ok(!panel.texts().some(text => text.includes('自己填完并提交')));
  assert.equal(panel.button('答完了，去看板重跑这份'), undefined);
});

test('it does not sit under the main panel, which holds the top right and paints above it', () => {
  const box = source.match(/\.box\{[^}]*\}/)[0];
  assert.match(box, /\bleft:16px/);
  assert.doesNotMatch(box, /\bright:/);
  assert.match(fs.readFileSync(path.join(__dirname, '../extension/content.js'), 'utf8'), /"position:fixed !important; top:20px !important; right:20px !important;"/);
});

test('a note is added beside what the panel already says', () => {
  const panel = open();
  panel.handle.note('没记上');
  assert.ok(panel.texts().includes('没记上'));
  assert.ok(panel.button('答完了，去看板重跑这份'));
});

const board = fs.readFileSync(path.join(__dirname, '../static/agent_runner.js'), 'utf8');
function boardQuestion() {
  const a = board.indexOf('  function question(job, q){'), b = board.indexOf('  // What each outcome means');
  const saved = [], context = {drafts: new Map(), make(tag, className, text) { const node = new Fake(tag); if (className) node.className = className; if (text != null) node.textContent = text; node.setAttribute = () => {}; return node; },
    button(className, text, onclick) { const node = context.make('button', className, text); node.onclick = onclick; return node; },
    changeAnswers: (route, body) => saved.push(body)};
  vm.createContext(context);
  vm.runInContext(board.slice(a, b) + '\nglobalThis.question = question;', context);
  return {context, saved};
}

test('the board\'s answer box shows what the page holds and does not offer it as her answer', () => {
  const {context, saved} = boardQuestion();
  const box = context.question({job_id: 'j', company: 'Acme'}, {label: 'Why us?', reason: '模型写的', value: 'model text'});
  const nodes = box.walk(), input = nodes.find(node => node.tag === 'textarea'), remember = nodes.find(node => node.textContent === '记住');
  assert.equal(input.value, '');
  assert.ok(nodes.some(node => node.textContent.includes('model text')));
  remember.onclick();
  assert.equal(saved.length, 0, 'an empty box remembers nothing');
  input.value = 'Because.';
  remember.onclick();
  assert.equal(saved[0].answer, 'Because.');
});

test('a draft she was typing comes back on the board', () => {
  const {context} = boardQuestion();
  context.drafts.set('j\nWhy us?', 'half written');
  const nodes = context.question({job_id: 'j'}, {label: 'Why us?', reason: '', value: 'model text'}).walk();
  assert.equal(nodes.find(node => node.tag === 'textarea').value, 'half written');
});

test('the board says when a batch stopped itself, and a spam flag is not "nothing to do"', () => {
  const pauses = vm.runInNewContext(`${board.match(/const pauses = \{[\s\S]*?\};/)[0]}; pauses`);
  assert.match(pauses.spam_flag, /这一轮停了/);
  assert.match(pauses.too_many_stops, /连续 3 份/);
  assert.match(board, /pauses\[state\.pause_reason\]\|\|phases\[state\.phase\]/);
  const a = board.indexOf('  function renderBatch(list){'), b = board.indexOf('  function renderPaused(list){');
  const box = new Fake('div'), summary = new Fake('p'), card = new Fake('section');
  const context = {outcomes: {}, $: id => ({'batch-rows': box, 'batch-summary': summary, 'batch-card': card}[id]),
    make(tag, className, text) { const node = new Fake(tag); if (text != null) node.textContent = text; return node; }, api: async () => ({})};
  vm.createContext(context);
  vm.runInContext(`${board.slice(a, b)}\nglobalThis.renderBatch = renderBatch;`, context);
  context.renderBatch([{company: 'Acme', title: 'SWE', status: 'blocked', clicked: true, job_id: 'j', unanswered: 0, evidence: '雇主拒收：Too many applications from this person.'},
    {company: 'Beta', title: 'SWE', status: 'blocked', clicked: true, job_id: 'k', unanswered: 0, evidence: '雇主拒收：We already have your application.'}]);
  const texts = box.walk().map(node => node.textContent);
  assert.ok(texts.some(text => text.includes('这一轮已经停了')));
  assert.ok(texts.some(text => text.includes('不用处理')), 'an ordinary refusal still needs nothing');
});
