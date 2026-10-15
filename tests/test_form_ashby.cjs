const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../extension/form_agent.js'), 'utf8');

// Shaped after the live Ashby page: no <form> anywhere, so every control has form === null.
function el(tagName, props = {}) {
  return {tagName, type: '', value: '', id: '', name: '', labels: [], textContent: '', className: '', form: null,
    disabled: false, required: false, validity: {valid: true}, attrs: {}, entry: null, autofill: false,
    getClientRects: () => [{}],
    getAttribute(key) { return this.attrs[key] ?? null; },
    hasAttribute(key) { return key in this.attrs; },
    closest(selector) {
      if (selector.includes('_fieldEntry')) return this.entry;
      if (selector.includes('autofill-input-root')) return this.autofill ? this : null;
      return null;
    },
    checkValidity() { return this.validity.valid; },
    dispatchEvent() {}, ...props};
}
function entry(question, required, children, statement = '', own = []) {
  const label = {textContent: question, className: required ? '_heading_f7 _required_f7 _label_1e' : '_heading_f7 _label_1e'};
  const shown = [question, statement, ...own.map(item => item.textContent), ...children.filter(child => child.yesno).map(child => child.textContent)];
  const box = {label, children, innerText: shown.filter(Boolean).join('\n'),
    querySelector(selector) {
      if (selector === 'label') return label;
      if (selector.includes('yesno-option')) return children.find(child => child.yesno) || null;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === 'button[aria-pressed]') return children.filter(child => child.yesno);
      if (selector.startsWith('label,button')) return [label, ...own];
      return selector === 'input,select,textarea' ? children.filter(child => !child.yesno) : [];
    }};
  for (const child of children) child.entry = box;
  return box;
}
// One required question asked as checkboxes, optionally beside other controls.
function checkboxEntry({question = 'How did you hear about Commure?', required = true, options = ['LinkedIn', 'Referral'], ticked = [], extra = []}) {
  const boxes = options.map(option => el('INPUT', {type: 'checkbox', name: option, labels: [{textContent: option}], checked: ticked.includes(option)}));
  entry(question, required, [...boxes, ...extra]);
  return [...boxes, ...extra];
}
function page({answer = '', invalid = false, hostname = 'jobs.ashbyhq.com', submits = 1, afterClick = null, bodyText = 'Application Submit Application',
               bodyAfterClick = '', shownHeadings = [], yesNoTitle = 'Will you require sponsorship?', statement = '', choices = null, extra = [], receipts = [], sentNote = '', alerts = []} = {}) {
  const headings = [...shownHeadings];
  let clicks = 0;
  const yes = el('BUTTON', {type: 'submit', yesno: true, textContent: 'Yes', attrs: {'aria-pressed': String(answer === 'Yes')},
    click() { yes.attrs['aria-pressed'] = 'true'; no.attrs['aria-pressed'] = 'false'; }});
  const no = el('BUTTON', {type: 'submit', yesno: true, textContent: 'No', attrs: {'aria-pressed': String(answer === 'No')},
    click() { no.attrs['aria-pressed'] = 'true'; yes.attrs['aria-pressed'] = 'false'; }});
  const name = el('INPUT', {type: 'text', id: '_systemfield_name', name: '_systemfield_name', value: 'Casey Example',
    labels: [{textContent: 'Full Name'}], required: true, validity: {valid: !invalid, validationMessage: ''}});
  const location_ = el('INPUT', {type: 'text', value: 'San Jose, California', attrs: {role: 'combobox'}});
  const autofill = el('INPUT', {type: 'file', autofill: true});
  entry('Full Name', true, [name]);
  entry('Location', true, [location_]);
  entry(yesNoTitle, true, [yes, no], statement);
  const submit = el('BUTTON', {type: 'submit', innerText: 'Submit Application', click() {
    clicks++;
    if (afterClick) headings.push(afterClick);
    if (bodyAfterClick) bodyText = bodyAfterClick;
  }});
  const controls = [autofill, name, location_, yes, no, ...(choices ? checkboxEntry(choices) : []), ...extra];
  const ctx = {
    setTimeout, Date: fakeClock(), crypto: {randomUUID: () => 'doc'}, Event: class { constructor(type) { this.type = type; } },
    location: {hostname, pathname: '/acme/1/application', href: `https://${hostname}/acme/1/application`},
    getComputedStyle: () => ({visibility: 'visible'}),
    document: {
      body: {get innerText() { return bodyText; }},
      getElementById: () => null,
      querySelectorAll(selector) {
        if (selector.startsWith('input,textarea,select,[role=combobox]')) return controls;
        if (selector === 'button,input[type=submit]') return Array(submits).fill(submit);
        if (selector === 'input,textarea,select') return [autofill, name, location_];
        if (selector === 'h1,h2,h3') return headings;
        if (selector === 'h2') return receipts;
        if (selector.startsWith('input[maxlength')) return extra.filter(box => box.attrs.maxlength === '1' || box.attrs.autocomplete === 'one-time-code');
        if (selector.startsWith('input[type=text]')) return extra;
        if (selector === 'input[type=radio]') return extra.filter(box => box.type === 'radio');
        if (selector === '[role=alert]') return alerts.map(textContent => el('DIV', {textContent}));
        if (selector.startsWith('p,h1')) return sentNote ? [el('P', {textContent: sentNote})] : [];
        return [];
      },
    },
  };
  vm.runInNewContext(source, ctx);
  return {agent: ctx.JobScanFormAgent, ctx, yes, no, clicks: () => clicks};
}

// Lets the 45-second wait after Submit pass at once.
function fakeClock() {
  let now = 1000;
  return class extends Date { static now() { now += 20000; return now; } };
}
const refusal = el('H2', {textContent: "We couldn't submit your application",
  parentElement: {innerText: "We couldn't submit your application We've noticed that you've recently applied to this role."}});

test('an employer refusal after Submit is a plain no, in its own words', async () => {
  const {agent} = page({afterClick: refusal});
  const result = await agent.submit(agent.capture());
  assert.equal(result.outcome, 'blocked');
  assert.match(result.evidence, /^雇主拒收：We couldn't submit your application We've noticed/);
});
test('a page already showing a refusal is never clicked again', async () => {
  const {agent} = page({afterClick: refusal});
  assert.equal(agent.preflight(agent.capture()).ok, true);
  await agent.submit(agent.capture());
  const gate = agent.preflight(agent.capture());
  assert.equal(gate.ok, false);
  assert.match(gate.reason, /^雇主拒收/);
});
test("the refusal seen on a real form, after Submit, still closes as the employer's no", async () => {
  const words = "We couldn't submit your application We've noticed that you've recently applied to this role, and it's not the right fit.";
  const {agent} = page({afterClick: {...refusal, parentElement: {innerText: words}}});
  const result = await agent.submit(agent.capture());
  assert.equal(result.outcome, 'blocked');
  assert.equal(result.evidence, '雇主拒收：' + words);
});
test('limit words in the form text before any click stop for the owner and refuse nothing', async () => {
  const {agent, clicks} = page({answer: 'No', bodyText: 'We have too many applications from you.'});
  const gate = agent.preflight(agent.capture());
  assert.equal(gate.ok, false);
  assert.doesNotMatch(gate.reason, /^雇主拒收/);
  assert.match(gate.reason, /too many applications from you/);
  const result = await agent.submit(agent.capture());
  assert.equal(result.outcome, 'blocked');
  assert.doesNotMatch(result.evidence, /^雇主拒收/);
  assert.equal(clicks(), 0);
});
test('a refusal heading on a page we never clicked refuses nothing either', async () => {
  const {agent, clicks} = page({answer: 'No', shownHeadings: [refusal]});
  const gate = agent.preflight(agent.capture());
  assert.equal(gate.ok, false);
  assert.doesNotMatch(gate.reason, /^雇主拒收/);
  assert.match(gate.reason, /couldn't submit your application/);
  assert.equal(clicks(), 0);
});
test('limit words that appear only after the click are the employer answering', async () => {
  const {agent, clicks} = page({answer: 'No', bodyAfterClick: 'Application limit reached. Reapply in 6 months.'});
  const result = await agent.submit(agent.capture());
  assert.equal(result.outcome, 'blocked');
  assert.equal(result.evidence, '雇主拒收：Application limit reached. Reapply in 6 months.');
  assert.equal(clicks(), 1);
});
test('limit words keep the block around them, so a period on the next line still reaches the cooldown', async () => {
  const {agent} = page({answer: 'No', bodyAfterClick: 'Name\n\nApplication limit reached.\n\nReapply in 6 months.\nEmail'});
  const result = await agent.submit(agent.capture());
  assert.equal(result.outcome, 'blocked');
  assert.equal(result.evidence, '雇主拒收：Application limit reached.\nReapply in 6 months.', 'its own line, so the service can tell the two apart');
});
for (const [first, line] of [['You recently applied to this position.', 'Similar role posted 2 days ago'],
  ['Application limit reached.', 'Backend Engineer · 3 days a week in office'], ['You have already applied to this job.', 'This role needs 5 years of experience.']]) {
  test(`a line after the refusal that only happens to name a period is not the employer's period: ${line}`, async () => {
    const {agent} = page({answer: 'No', bodyAfterClick: `${first}\n${line}`});
    assert.equal((await agent.submit(agent.capture())).evidence, '雇主拒收：' + first);
  });
}
test('we receive too many applications is about the employer\'s inbox, and it stops nobody', async () => {
  const before = page({answer: 'No', bodyText: 'We receive too many applications to respond to everyone.'});
  assert.equal(before.agent.preflight(before.agent.capture()).ok, true);
  assert.equal(before.agent.refusalShown(), '');
});
test('with no period near them the words stand alone: a question further down the form is no period', async () => {
  const {agent} = page({answer: 'No', bodyAfterClick: 'You have already applied to this job.\nDo you have 5 years of experience?\nFull Name'});
  assert.equal((await agent.submit(agent.capture())).evidence, '雇主拒收：You have already applied to this job.');
});
test('a period in the same line is the whole block: nothing further down can change it', async () => {
  const {agent} = page({answer: 'No', bodyAfterClick: 'Too many applications from this person. Try again in 3 months.\nWe keep your data for 5 years.'});
  const result = await agent.submit(agent.capture());
  assert.equal(result.evidence, '雇主拒收：Too many applications from this person. Try again in 3 months.');
});

// ---- an employer turning her away in other words than the limit phrases ----
const TURNED_AWAY = ['You recently applied to this position. Please wait 6 months before applying again.',
  'Your application is frozen. You may reapply after 90 days.', 'You have already applied to this job.',
  'Applications from you are frozen for 6 months.', "We've noticed that you've recently applied to this role.",
  'You applied to this role recently.', 'Already applied.', 'You are in a cooldown period of 90 days.', 'A 90-day cooldown applies to your application.',
  'There is a freeze on new applications.', 'Please wait before applying again.', 'You may apply again after January 2027.', 'You cannot reapply at this time.',
  // The wordings the owner named, and other plain ways to say the same.
  'Recently applied. Please try again later.', 'Application frozen.', 'You submitted an application recently.', 'We already have an application from you for this role.',
  'You can apply again on March 3, 2027.', 'You may reapply after January 15, 2028.', 'You must wait 6 months.', 'You must wait 6 months before reapplying.', 'You may not reapply for 6 months.'];
for (const words of TURNED_AWAY) {
  test(`after a real click this is the employer saying no: ${words}`, async () => {
    const {agent, clicks} = page({answer: 'No', bodyAfterClick: words});
    const result = await agent.submit(agent.capture());
    assert.equal(result.outcome, 'blocked');
    assert.equal(result.evidence, '雇主拒收：' + words);
    assert.equal(clicks(), 1);
    assert.match(agent.preflight(agent.capture()).reason, /^雇主拒收/, 'and that page is never clicked again');
  });
  test(`before any click the same words stop for the owner and refuse nothing: ${words}`, async () => {
    const {agent, clicks} = page({answer: 'No', bodyText: words});
    const gate = agent.preflight(agent.capture());
    assert.equal(gate.ok, false);
    assert.doesNotMatch(gate.reason, /^雇主拒收/);
    assert.ok(gate.reason.includes(words));
    const result = await agent.submit(agent.capture());
    assert.equal(result.outcome, 'blocked');
    assert.doesNotMatch(result.evidence, /^雇主拒收/);
    assert.equal(clicks(), 0);
  });
}
test('the same wording in a heading: the whole block counts, whatever the body says', async () => {
  const heading = el('H2', {textContent: 'Your application is frozen', parentElement: {innerText: 'Your application is frozen\nYou may reapply after 90 days.'}});
  const {agent} = page({answer: 'No', afterClick: heading});
  const result = await agent.submit(agent.capture());
  assert.equal(result.outcome, 'blocked');
  assert.equal(result.evidence, '雇主拒收：Your application is frozen\nYou may reapply after 90 days.');
  const before = page({answer: 'No', shownHeadings: [heading]});
  assert.doesNotMatch(before.agent.preflight(before.agent.capture()).reason, /^雇主拒收/);
  assert.equal(before.clicks(), 0);
});
test('a refusal heading inside the form\'s own container does not take in the form\'s questions', async () => {
  const form = {innerText: 'You have already applied to this job\nName\nEmail\nDo you have at least 8 years of experience?\nSubmit Application', querySelector: selector => (selector.includes('input') ? {} : null)};
  const heading = el('H2', {textContent: 'You have already applied to this job', parentElement: form, nextElementSibling: {innerText: 'Please contact us with any questions.', querySelector: () => null}});
  const {agent} = page({answer: 'No', afterClick: heading});
  assert.equal((await agent.submit(agent.capture())).evidence, '雇主拒收：You have already applied to this job\nPlease contact us with any questions.');
});
test('polite wording around a real no does not hide it: thanks for your interest is no receipt', async () => {
  for (const [words, kept] of [['Thank you for your interest. You may not reapply for 6 months.', 'You may not reapply for 6 months.'],
    ['Thanks for your interest. Unfortunately you cannot reapply for 12 months.', 'Unfortunately you cannot reapply for 12 months.']]) {
    const {agent} = page({answer: 'No', bodyAfterClick: words});
    const result = await agent.submit(agent.capture());
    assert.equal(result.outcome, 'blocked', words);
    assert.equal(result.evidence, '雇主拒收：' + kept);
  }
});
// The first is real: 19 stored job ads say it, and a Greenhouse page shows the ad above the form.
const IN_PASSING = ['Candidates may not reapply to the same role while an application is active or within 90 days.',
  'If you have already applied, please wait 6 months before applying again.', 'Please note that if you recently applied, we still have your details.',
  'Have you recently applied to Acme?', 'Have you already applied to Acme (optional)', 'Already applied? Sign in here.',
  'Applicants may reapply after 12 months.', 'If not selected, you may reapply after 6 months.', 'We lifted our hiring freeze last year.',
  'You will debug applications that freeze under load.', 'You need to not freeze up when dropped into a new system.', 'Our retry cooldown logic needs work.',
  'Please wait.', 'Please wait while we submit your application.', 'Please wait 5 days for a response.', 'Please wait 30 seconds before applying again.',
  'We ship frozen food.', 'Thanks for your interest in Acme. You may reapply after 6 months.',
  // A real send can end on these: calling one a refusal would close the role and freeze the company for something that went out.
  'Please wait 6 months.', 'You can reapply in 6 months.', 'Reapply after 12 months.',
  'You may reapply in 12 months if you are not selected.', 'Please wait 5 days before contacting us.', 'Please wait 2 weeks and then check your email.',
  'Thanks! We got your application. You may reapply after 6 months.', 'We got it! You can apply again in 6 months.',
  'All done. Please wait 5 days and check your email.', 'We got it! Please wait 2 weeks before we get back to you.', "You're all set. You may apply again after 12 months.",
  'Application complete. You may reapply after 6 months.', "You've already submitted your application. We'll be in touch.",
  'Your application was received. You cannot reapply for 6 months.'];
for (const words of IN_PASSING) {
  test(`a page that merely mentions it is no refusal, before or after a click: ${words}`, async () => {
    const before = page({answer: 'No', bodyText: words});
    assert.equal(before.agent.preflight(before.agent.capture()).ok, true);
    assert.equal(before.agent.refusalShown(), '');
    const {agent} = page({answer: 'No', bodyAfterClick: words});
    assert.equal((await agent.submit(agent.capture())).outcome, 'unknown');
  });
}

// ---- what the watch on her own hand submit reads ----
test('refusal words read the same with no click of ours, and in the form the service records', () => {
  const {agent} = page({answer: 'No', bodyText: 'You have already applied to this job.'});
  assert.equal(agent.refusalShown(), '雇主拒收：You have already applied to this job.');
  assert.equal(page({answer: 'No'}).agent.refusalShown(), '');
  const limit = page({answer: 'No', bodyText: 'Application limit reached. Reapply in 6 months.'});
  assert.equal(limit.agent.refusalShown(), '雇主拒收：Application limit reached. Reapply in 6 months.');
});
test('a real receipt beats refusal words on the same page', () => {
  const receipt = el('H2', {textContent: 'Your application was submitted.', matches: () => false});
  const {agent} = page({answer: 'No', receipts: [receipt], bodyText: 'Your application was submitted. You may reapply after 6 months.'});
  assert.equal(agent.refusalShown(), '');
  assert.match(agent.preflight(agent.capture()).reason, /投递成功回执/);
});
test('on a site with no known receipt, thanks beside "you may reapply" still refuses nothing', () => {
  const {agent} = page({answer: 'No', hostname: 'careers.example.com', bodyText: 'Thank you for applying! You may reapply after 6 months.'});
  assert.equal(agent.refusalShown(), '');
});

test('a receipt in two blocks is read as two blocks: textContent runs them together as "SuccessThank you"', async () => {
  const words = 'Thank you for applying. If you are not selected you may reapply after 6 months.';
  const receipt = el('H2', {textContent: 'Success', matches: selector => selector === 'h2', parentElement: {innerText: `Success\n${words}`, textContent: `Success${words}`}});
  const {agent} = page({answer: 'No', receipts: [receipt], bodyText: `Success\n${words}`});
  assert.match(agent.preflight(agent.capture()).reason, /投递成功回执/);
  assert.equal(agent.refusalShown(), '');
});
test('silence after Submit stays unknown and says what the page shows', async () => {
  const {agent} = page({bodyText: 'Something else entirely'});
  const result = await agent.submit(agent.capture());
  assert.equal(result.outcome, 'unknown');
  assert.match(result.evidence, /页面现在显示：Something else entirely/);
});

test("an employer's own thanks under Ashby's Success heading is a receipt after our click, and only then", async () => {
  const words = 'Thanks for your application to Nestmed. It has been recieved and we will contact you if there are next steps.';
  const receipt = el('H2', {textContent: 'Success', matches: selector => selector === 'h2', parentElement: {innerText: `Success\n${words}`}});
  const before = page({answer: 'No', receipts: [receipt]});
  assert.equal(before.agent.preflight(before.agent.capture()).ok, true);
  const receipts = [];
  const after = page({answer: 'No', receipts, afterClick: null});
  const submitted = after.agent.submit(after.agent.capture());
  receipts.push(receipt);
  const result = await submitted;
  assert.equal(result.outcome, 'submitted');
  assert.match(result.evidence, /It has been recieved/);
});
// Numeral 2026-11-07: the banner Ashby showed after our click, with the address still on screen.
const corrections = 'Application\nYour form needs corrections\nMissing entry for required field: Email\nMissing entry for required field: Location\nSubmit Application';
test("Ashby's corrections banner after our click is a field error to fix, not an unclear result", async () => {
  const {agent, clicks} = page({answer: 'No', bodyAfterClick: corrections});
  const result = await agent.submit(agent.capture());
  assert.equal(clicks(), 1);
  assert.equal(result.outcome, 'validation_error');
  assert.equal(result.evidence, 'Your form needs corrections\nMissing entry for required field: Email\nMissing entry for required field: Location');
});
test('a corrections banner still up from the last click is not read as the answer to this one', async () => {
  const {agent} = page({answer: 'No', bodyText: corrections});
  const result = await agent.submit(agent.capture());
  assert.equal(result.outcome, 'unknown');
});
test('the banner answering our own click does not stop the second click, any other page error still does', async () => {
  const alerts = [];
  const {agent, ctx} = page({answer: 'No', bodyAfterClick: corrections, alerts});
  await agent.submit(agent.capture());
  alerts.push('Your form needs corrections Missing entry for required field: Email');
  assert.equal(agent.preflight(agent.capture()).ok, true);
  alerts.push('Something went wrong');
  assert.equal(agent.preflight(agent.capture()).ok, false);
  alerts.pop();
  ctx.location.hostname = 'boards.greenhouse.io';
  assert.equal(agent.preflight(agent.capture()).ok, false);
});

test('an evidence or label cut at an emoji keeps it whole', async () => {
  const {agent} = page({bodyText: '😀' + 'y'.repeat(599)});
  const result = await agent.submit(agent.capture());
  assert.equal(result.outcome, 'unknown');
  assert.ok(result.evidence.isWellFormed());
  assert.ok(result.evidence.endsWith('😀' + 'y'.repeat(599)));
  const label = page({yesNoTitle: 'x'.repeat(2999) + '😀 and more'}).agent.capture().fields[2].label;
  assert.ok(label.isWellFormed());
  assert.equal(label, 'x'.repeat(2999) + '😀');
});
test('the statement beside a Yes/No reaches the Reviewer and the label stays the short title', () => {
  const field = page({yesNoTitle: 'Acknowledgement', statement: 'I certify that\nall of this is true.'}).agent.capture().fields[2];
  assert.equal(field.label, 'Acknowledgement');
  assert.equal(field.description, 'I certify that all of this is true.');
});
test('a statement holding the word no keeps it, in the Reviewer\'s text as on the page', () => {
  const field = page({yesNoTitle: 'Family notice', statement: 'I have no relatives working here.'}).agent.capture().fields[2];
  assert.equal(field.description, 'I have no relatives working here.');
});
test('the statement under a choice, a select or a text box reaches the Reviewer as well', () => {
  const statement = 'I authorize this company to run a credit check.';
  const radio = el('INPUT', {type: 'radio', name: 'gdpr', value: 'a', labels: [{textContent: 'I Acknowledge/Accept this Statement'}]});
  entry('GDPR Acknowledgement', true, [radio], statement, [{textContent: 'I Acknowledge/Accept this Statement'}]);
  let field = page({extra: [radio]}).agent.capture().fields.at(-1);
  assert.equal(field.label, 'GDPR Acknowledgement');
  assert.equal(field.description, statement);
  const select = el('SELECT', {options: [{value: 'yes', textContent: 'Yes'}, {value: 'no', textContent: 'No'}], selectedOptions: [], labels: [{textContent: 'Acknowledgement'}]});
  entry('Acknowledgement', true, [select], `${statement}\nYes\nNo`);
  field = page({extra: [select]}).agent.capture().fields.at(-1);
  assert.equal(field.description, statement, 'the options are the control\'s own words');
  const text = box({name: 'q', value: 'I agree', labels: [{textContent: 'Acknowledgement'}]});
  entry('Acknowledgement', true, [text], statement);
  field = page({extra: [text]}).agent.capture().fields.at(-1);
  assert.equal(field.description, statement);
});
test('statementOf gives Filler the same words capture gives the Reviewer', () => {
  const statement = 'I authorize this company to run a credit check.';
  const radio = el('INPUT', {type: 'radio', name: 'gdpr', value: 'a', labels: [{textContent: 'Yes'}]});
  entry('GDPR Acknowledgement', true, [radio], statement, [{textContent: 'Yes'}]);
  const {agent} = page({extra: [radio]});
  assert.equal(agent.statementOf(radio), agent.capture().fields.at(-1).description);
  assert.equal(agent.statementOf(radio), statement);
});
test('an upload box\'s own text is no statement, and a plain box sends none', () => {
  const file = el('INPUT', {type: 'file', files: [], labels: [{textContent: 'Resume'}], getClientRects: () => [{}]});
  entry('Resume', true, [file], 'Upload File or drag and drop here');
  const plain = box({name: 'q', value: 'x', labels: [{textContent: 'Why us?'}]});
  entry('Why us?', true, [plain]);
  const fields = page({extra: [file, plain]}).agent.capture().fields;
  assert.equal('description' in fields.at(-2), false);
  assert.equal('description' in fields.at(-1), false);
});
test('a Yes/No with nothing beside its title sends no description at all', () => {
  assert.equal('description' in page().agent.capture().fields[2], false);
});
test('a required question asked as checkboxes reports an error when none is ticked', () => {
  const {agent} = page({answer: 'No', choices: {}});
  const snapshot = agent.capture();
  assert.deepEqual([...snapshot.errors], ['必填多选题「How did you hear about Commure?」一项都没选，需要你本人选。']);
  assert.equal(agent.preflight(snapshot).ok, false);
  assert.equal(snapshot.fields.filter(f => f.kind === 'checkbox').every(f => !f.required), true);
});
test('one ticked box answers a required checkbox question', () => {
  assert.deepEqual([...page({choices: {ticked: ['Referral']}}).agent.capture().errors], []);
});
test('an optional checkbox question may stay empty', () => {
  assert.deepEqual([...page({choices: {required: false}}).agent.capture().errors], []);
});
test('a checkbox beside other controls is not the question (Education: still a student?)', () => {
  const extra = [el('INPUT', {type: 'text', name: 'school', labels: [{textContent: 'School'}]})];
  assert.deepEqual([...page({choices: {options: ['Still a student', 'On leave'], extra}}).agent.capture().errors], []);
});
test('a single required box stays a required field, not a page error', () => {
  const snapshot = page({choices: {question: 'I understand', options: ['I understand']}}).agent.capture();
  assert.deepEqual([...snapshot.errors], []);
  assert.equal(snapshot.fields.find(f => f.kind === 'checkbox').required, true);
});

// ---- a code never reaches the Reviewer model ----
const CODE = '482913';
const box = props => el('INPUT', {type: 'text', value: CODE, ...props});
const SENT = 'We sent a verification code to your email.';
const holdsCode = snapshot => JSON.stringify(snapshot).includes(CODE);
for (const [name, props] of Object.entries({
  'a name page_gate misses': {name: 'security_code'}, 'Greenhouse\'s own box': {id: 'security-input-0'},
  'a dashed id': {id: 'verification-code'}, 'its label': {labels: [{textContent: 'Verification code'}]},
  'an aria label': {attrs: {'aria-label': 'Security code'}}, 'a placeholder': {attrs: {placeholder: 'Enter code'}},
  'a bare Code': {labels: [{textContent: 'Code'}]}, 'a digit count': {labels: [{textContent: 'Your 6-digit code'}]},
  'Chinese': {labels: [{textContent: '邮箱验证码'}]}, 'email code': {name: 'email_code'}, 'a passcode': {name: 'passcode'}, 'a one-time password': {labels: [{textContent: 'One-time password'}]},
  'an access code': {labels: [{textContent: 'Access code'}]}, 'one-time-code autocomplete': {attrs: {autocomplete: 'one-time-code'}},
})) {
  test(`once the page says a code was sent, a code box stays out of the snapshot: ${name}`, () => {
    const snapshot = page({extra: [box(props)], sentNote: SENT}).agent.capture();
    assert.equal(holdsCode(snapshot), false);
    assert.equal(snapshot.fields.length, 3, 'nothing else went with it');
  });
  // "verification-code" in an id is on the older list, which stays out whatever the page says.
  if (name !== 'a dashed id') test(`with no such words the box stays in front of the Reviewer, and only what it holds is hidden: ${name}`, () => {
    const snapshot = page({extra: [box(props)]}).agent.capture();
    assert.equal(holdsCode(snapshot), false);
    assert.equal(snapshot.fields.length, 4);
    assert.ok(snapshot.fields.at(-1).value.includes('已隐藏'));
  });
}
test('a code box left blank stays out once a code was sent, so typing the code later does not change the snapshot', () => {
  const code = box({name: 'security_code', value: ''});
  const {agent} = page({extra: [code], sentNote: SENT});
  const before = agent.capture();
  code.value = CODE;
  assert.equal(agent.fingerprint(agent.capture()), agent.fingerprint(before));
});
test('one box per digit stays out once a code was sent, is hidden before that, and fewer than four are ordinary fields', () => {
  const digits = count => Array.from({length: count}, (_, i) => el('INPUT', {type: 'text', id: `d${i}`, value: String(i + 1), labels: [{textContent: 'Digit'}], attrs: {maxlength: '1'}}));
  assert.equal(page({extra: digits(6), sentNote: SENT}).agent.capture().fields.length, 3);
  const before = page({extra: digits(6)}).agent.capture().fields.slice(3);
  assert.equal(before.length, 6);
  assert.ok(before.every(field => field.value.includes('已隐藏')));
  assert.equal(page({extra: digits(3)}).agent.capture().fields.length, 6);
  assert.equal(page({extra: digits(3)}).agent.capture().fields.at(-1).value, '3');
});
// A real question can start like a code box. Dropped from the snapshot, nobody would read it and a required blank would go unnoticed.
for (const label of ['Please enter your GitHub code repository', 'Enter a link to your code sample', 'Enter a link to your code samples', 'Please enter your GitHub code link',
  'Type your code sample URL', 'Enter your source code repository', 'Input your favourite code editor', 'Enter your code of conduct acknowledgement']) {
  test(`a question that only starts like a code box stays, even with a code sent: ${label}`, () => {
    const required = box({name: 'q', required: true, value: '', labels: [{textContent: label}]});
    for (const sentNote of ['', SENT]) {
      const field = page({extra: [required], sentNote}).agent.capture().fields.at(-1);
      assert.equal(field.label, label);
      assert.equal(field.required, true);
    }
  });
}
for (const name of ['input_code', 'enter_code']) {
  test(`a box named ${name} stays in the snapshot until a code was really sent`, () => {
    assert.equal(page({extra: [box({name, value: ''})]}).agent.capture().fields.length, 4);
    assert.equal(page({extra: [box({name, value: ''})], sentNote: SENT}).agent.capture().fields.length, 3);
  });
}
test('the three code readers in form_agent and page_gate are the same words', () => {
  const gate = fs.readFileSync(path.join(__dirname, '../extension/page_gate.js'), 'utf8');
  for (const name of ['CODE_BOX', 'NOT_A_CODE', 'CODE_SENT']) {
    const line = text => text.match(new RegExp(`const ${name} = (.*);$`, 'm'))[1];
    assert.equal(line(gate), line(source), name);
  }
});
test('an error on the page repeating the code does not carry it to the Reviewer', () => {
  const words = 'The code 481516 is incorrect.';
  const snapshot = page({extra: [box({name: 'security_code', value: ''})], sentNote: SENT, alerts: [words, 'Code AB12CD is wrong.']}).agent.capture();
  assert.doesNotMatch(JSON.stringify(snapshot.errors), /481516|AB12CD/);
  assert.equal(snapshot.errors.length, 2, 'the error itself still counts');
  assert.deepEqual([...page({alerts: ['Salary must be under 200000.']}).agent.capture().errors], ['Salary must be under 200000.'], 'with no code step around, numbers stay');
});
test('with a code on its way a plain word under the loose wording is hidden too', () => {
  const words = box({name: 'q', value: 'abcdef', labels: [{textContent: 'Please verify'}]});
  assert.equal(page({extra: [words]}).agent.capture().fields.at(-1).value, 'abcdef');
  assert.ok(page({extra: [words], sentNote: SENT}).agent.capture().fields.at(-1).value.includes('已隐藏'));
});
test('what fillCode wrote stays out whatever the box is called, and so does that code in any other box', () => {
  const content = fs.readFileSync(path.join(__dirname, '../extension/content.js'), 'utf8');
  const fill = content.slice(content.indexOf('  function codeField() {'), content.indexOf('  // ================= Workday'));
  const target = el('INPUT', {type: 'text', id: 'q7', labels: [{textContent: 'Answer'}], focus() {}, blur() {}});
  const pasted = el('INPUT', {type: 'text', id: 'q8', labels: [{textContent: 'Reference'}]});
  const {agent, ctx} = page({extra: [target, pasted]});
  vm.runInContext(`const fieldText = () => "enter code"; const setNativeValue = (el, value) => { el.value = value; };\n${fill}\nglobalThis.fillCode = fillCode;`, ctx);
  assert.equal(agent.capture().fields.length, 5);
  assert.equal(ctx.fillCode(CODE), true);
  assert.equal(target.value, CODE);
  let snapshot = agent.capture();
  assert.equal(holdsCode(snapshot), false);
  assert.equal(snapshot.fields.length, 4);
  pasted.value = CODE;
  snapshot = agent.capture();
  assert.equal(holdsCode(snapshot), false);
  assert.equal(snapshot.fields.length, 3);
});
test('a code she was only shown stays out too: fillCode found no box, so she pastes it herself', () => {
  const content = fs.readFileSync(path.join(__dirname, '../extension/content.js'), 'utf8');
  const fill = content.slice(content.indexOf('  function codeField() {'), content.indexOf('  // ================= Workday'));
  const pasted = el('INPUT', {type: 'text', id: 'q8', labels: [{textContent: 'Reference'}], value: ''});
  const {agent, ctx} = page({extra: [pasted]});
  vm.runInContext(`const fieldText = () => "reference"; const setNativeValue = () => {};\n${fill}\nglobalThis.fillCode = fillCode;`, ctx);
  assert.equal(ctx.fillCode(CODE), false);
  pasted.value = CODE;
  assert.equal(holdsCode(agent.capture()), false);
});
// codeField reads "\bcode\b" and "verify" as a code box because it only runs on her press. Dropping these from the
// snapshot would hide real questions from the Reviewer, and the first is on a stored form.
const CEREBRAS = 'Cerebras products, software, source code, technology, and/or services are subject to the U.S. Export Administration Regulations (EAR).';
for (const [label, value] of [[CEREBRAS, 'Yes'], ['Have you written production code in Go?', 'Yes'], ['Link to a code sample', 'https://example.test/a'],
  ['Can we verify your employment?', 'Yes'], ['Languages you code in', 'Python, Go'], ['Zip Code', '95112'], ['Referral code', 'ABC123'], ['Promo code', 'SPRING24'], ['Type of code you write most', 'Backend']]) {
  test(`a question holding those loose words stays in front of the Reviewer: ${label.slice(0, 40)}`, () => {
    const field = page({extra: [box({name: 'q', labels: [{textContent: label}], value})]}).agent.capture().fields.at(-1);
    assert.equal(field.label, label);
    assert.equal(field.value, value);
  });
}
test('only a value shaped like a code is hidden under those loose words, and the question stays', () => {
  const field = page({extra: [box({name: 'q', labels: [{textContent: 'Verify your account'}]})]}).agent.capture().fields.at(-1);
  assert.equal(field.label, 'Verify your account');
  assert.doesNotMatch(field.value, /482913/);
  assert.ok(field.value.trim());
});
test('a checkbox naming a code is a consent, not a code box', () => {
  const tick = el('INPUT', {type: 'checkbox', name: 'consent', labels: [{textContent: 'Send me a verification code'}], checked: true, value: 'on'});
  assert.equal(page({extra: [tick]}).agent.capture().fields.at(-1).value, 'Yes');
});

test('a formless Ashby page reads as a complete snapshot', () => {
  const snapshot = page().agent.capture();
  assert.equal(snapshot.coverage, 'complete');
  assert.deepEqual([...snapshot.fields].map(f => f.label), ['Full Name', 'Location', 'Will you require sponsorship?']);
});
test('Yes/No buttons are one required question, not two unreadable controls', () => {
  const field = page().agent.capture().fields[2];
  assert.equal(field.kind, 'radio');
  assert.equal(field.readable, true);
  assert.equal(field.required, true);
  assert.equal(field.value, '');
  assert.deepEqual([...field.options], ['Yes', 'No']);
});
test('the pressed button is the answer', () => {
  assert.equal(page({answer: 'No'}).agent.capture().fields[2].value, 'No');
  assert.equal(page({answer: 'Yes'}).agent.capture().fields[2].value, 'Yes');
});
test('a repair clicks the matching button and reads it back', () => {
  const {agent} = page({answer: 'Yes'});
  const snapshot = agent.capture();
  const changes = agent.patch(snapshot, [{field_id: snapshot.fields[2].id, auto_fix: true, replacement: 'No'}]);
  assert.equal(changes[0].after, 'No');
  assert.equal(agent.capture().fields[2].value, 'No');
});
test('a typeahead is read from its own text and marked required by its label', () => {
  const field = page().agent.capture().fields[1];
  assert.equal(field.kind, 'combobox');
  assert.equal(field.readable, true);
  assert.equal(field.required, true);
  assert.equal(field.value, 'San Jose, California');
});
test('the resume-parsing uploader is not a form answer', () => {
  assert.equal(page().agent.capture().fields.some(f => f.kind === 'file'), false);
});
test('the one Submit is usable with no form element', () => {
  const {agent} = page({answer: 'No'});
  assert.equal(agent.preflight(agent.capture()).ok, true);
});
test('native validity still blocks a formless Submit', () => {
  const {agent} = page({answer: 'No', invalid: true});
  assert.equal(agent.preflight(agent.capture()).ok, false);
});
test('two Submit buttons are never guessed between', () => {
  const {agent} = page({answer: 'No', submits: 2});
  assert.equal(agent.preflight(agent.capture()).ok, false);
});
test('formless handling is Ashby only', () => {
  const {agent} = page({answer: 'No', hostname: 'careers.example.com'});
  const snapshot = agent.capture();
  assert.equal(snapshot.coverage, 'partial');
  assert.equal(agent.preflight(snapshot).ok, false);
});
