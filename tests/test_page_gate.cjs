const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../extension/page_gate.js'), 'utf8');

// Like the real thing: iterable, but no .map — that gap crashed inspect() on every live page.
const nodeList = (items = []) => ({length: items.length, [Symbol.iterator]: () => items[Symbol.iterator]()});
function node(tag, content = '', attrs = {}, children = []) {
  const el = {
    tag, attrs, children, id: attrs.id || '', name: attrs.name || '', type: attrs.type || '',
    value: attrs.value || '', disabled: !!attrs.disabled, labels: nodeList(),
    getAttribute: key => attrs[key] ?? null,
    getClientRects: () => attrs.hidden ? [] : [{}],
    closest: () => null,
    get innerText() { return [content, ...children.filter(child => !child.attrs.hidden).map(child => child.innerText)].join(' '); },
    get textContent() { return this.innerText; },
    querySelectorAll(selector) { return descendants(children).filter(child => matches(child, selector)); },
  };
  for (const child of children) child.parentElement = el;
  return el;
}
function descendants(nodes) { return nodes.flatMap(el => [el, ...descendants(el.children)]); }
function matches(el, selectors) {
  return selectors.split(',').some(selector => {
    const match = selector.match(/^(\w+)?(?:\[([\w-]+)(?:="([^"]+)")?\])?$/);
    return match && (!match[1] || match[1] === el.tag) &&
      (!match[2] || (match[3] === undefined ? match[2] in el.attrs : el.attrs[match[2]] === match[3]));
  });
}
function inspect(nodes) {
  const ctx = {document: {querySelectorAll: selector => descendants(nodes).filter(el => matches(el, selector))},
    getComputedStyle: el => ({display: el.attrs.display || 'block', visibility: el.attrs.visibility || 'visible', opacity: el.attrs.opacity || '1'})};
  vm.runInNewContext(source, ctx);
  return ctx.JobScanPageGate.inspect();
}
function application(extra = []) {
  return node('form', '', {id:'application_form'}, [
    node('input', '', {type:'text', name:'name'}),
    node('input', '', {type:'email', name:'email'}),
    node('button', 'Submit Application'), ...extra,
  ]);
}

test('a visible application is ready without clicking anything', () => {
  assert.equal(inspect([application()]).kind, 'ready');
});
test('an Ashby application with no <form> element is ready', () => {
  const page = node('div', '', {}, [
    node('div', '', {}, [node('input', '', {type:'text', 'aria-label':'Full Name'})]),
    node('div', '', {}, [node('button', 'Submit Application')]),
  ]);
  assert.equal(inspect([page]).kind, 'ready');
});
test('a formless page needs both Submit and an applicant field', () => {
  assert.equal(inspect([node('div', '', {}, [node('button', 'Submit Application')])]).kind, 'unknown');
  assert.equal(inspect([node('div', '', {}, [node('input', '', {type:'text', 'aria-label':'Full Name'}),
    node('button', 'Apply for this Job')])]).kind, 'unknown');
});
for (const notice of [
  'This job is no longer available.', 'This job posting is no longer active.',
  'The position has been filled.', 'Job not found', 'This job has expired.',
  'The job you are looking for is no longer available.', 'We are no longer accepting applications.',
]) {
  test(`explicit unavailable heading stops filling: ${notice}`, () => {
    const result = inspect([node('h1', notice)]);
    assert.equal(result.kind, 'expired');
    assert.equal(result.evidence, notice);
  });
}
test('an explicit visible alert can establish expiration', () => {
  assert.equal(inspect([node('div', 'This position is closed.', {role:'alert'})]).kind, 'expired');
});
test('JD paragraphs mentioning expired/closed do not establish expiration', () => {
  assert.equal(inspect([node('h1', 'Backend Engineer'), node('p', 'Handle expired sessions and closed connections. This job is no longer available in an old example.')]).kind, 'unknown');
});
test('JD headings that merely contain a status word are not expiry notices', () => {
  assert.equal(inspect([node('h2', 'Role filled with challenging projects')]).kind, 'unknown');
  assert.equal(inspect([node('h2', 'Is this job closed?')]).kind, 'unknown');
});
test('generic network errors and a generic 404 are unknown', () => {
  for (const message of ['404 Not Found', 'Page not found', 'Network error', 'This site can’t be reached', 'Error loading application']) {
    assert.equal(inspect([node('h1', message)]).kind, 'unknown');
  }
});
test('an available application wins over an old footer expiry heading', () => {
  assert.equal(inspect([application(), node('footer', '', {}, [node('h2', 'This job has expired.')])]).kind, 'ready');
});
test('hidden and invisible expiry notices do not establish expiration', () => {
  for (const attrs of [{hidden:true}, {visibility:'hidden'}, {opacity:'0'}, {display:'none'}]) {
    assert.equal(inspect([node('h1', 'This job has expired.', attrs)]).kind, 'unknown');
  }
  assert.equal(inspect([node('div', '', {opacity:'0'}, [node('h1', 'This job has expired.')])]).kind, 'unknown');
});
test('a generic newsletter form is not an application', () => {
  assert.equal(inspect([node('form', '', {}, [node('input', '', {type:'email', name:'email'}), node('button', 'Subscribe')])]).kind, 'unknown');
});
test('disabled submit does not hide an otherwise fillable application', () => {
  assert.equal(inspect([node('form', '', {id:'application_form'}, [node('input', '', {name:'email', type:'email'}), node('button', 'Submit Application', {disabled:true})])]).kind, 'ready');
});
test('visible password and explicit sign-in walls are login gates', () => {
  assert.equal(inspect([node('input', '', {type:'password'})]).kind, 'login');
  assert.equal(inspect([node('h1', 'Sign in to apply')]).kind, 'login');
  assert.equal(inspect([node('a', 'Log in to continue')]).kind, 'login');
});
test('an optional sign-in link does not block a visible application', () => {
  assert.equal(inspect([application(), node('a', 'Sign in')]).kind, 'ready');
});
test('email OTP requires both an input and a sent-code message', () => {
  const otp = node('input', '', {autocomplete:'one-time-code'});
  assert.equal(inspect([otp, node('p', 'A verification code has been sent to your email.')]).kind, 'verification');
  assert.equal(inspect([otp]).kind, 'unknown');
  assert.equal(inspect([node('p', 'We emailed you a verification code.')]).kind, 'unknown');
  assert.equal(inspect([node('input', '', {'aria-label':'Email verification code'}), node('p', 'We emailed you a code.')]).kind, 'verification');
});
test('after a click these are code steps too, the same boxes form_agent keeps from the Reviewer', () => {
  const sent = node('p', 'We sent a verification code to your email.');
  assert.equal(inspect([node('input', '', {'aria-label':'Enter the 8-character code'}), sent]).kind, 'verification');
  assert.equal(inspect([node('input', '', {name:'security_code'}), sent]).kind, 'verification');
  const six = Array.from({length: 6}, (_, i) => node('input', '', {maxlength:'1', name:`security-input-${i}`}));
  assert.equal(inspect([...six, sent]).kind, 'verification');
  assert.equal(inspect(six).kind, 'unknown', 'no words about a code: no code step');
  assert.equal(inspect([...Array.from({length: 3}, (_, i) => node('input', '', {maxlength:'1', name:`digit${i}`})), sent]).kind, 'unknown', 'too few to be one box per digit');
  assert.equal(inspect([node('input', '', {type:'checkbox', 'aria-label':'Send me a verification code'}), sent]).kind, 'unknown', 'a tick box is no code box');
  assert.equal(inspect([node('input', '', {'aria-label':'Enter a link to your code sample'}), sent]).kind, 'unknown', 'a real question is no code box');
});
test('postal code and ordinary email fields are not verification gates', () => {
  assert.equal(inspect([application([node('input', '', {name:'postal_code'})]), node('p', 'We will send a confirmation email after applying.')]).kind, 'ready');
});
test('captcha is distinct from email verification', () => {
  assert.equal(inspect([application(), node('iframe', '', {src:'https://captcha.example/challenge', title:'hcaptcha'})]).kind, 'captcha');
  assert.equal(inspect([node('h2', 'Verify you are human')]).kind, 'captcha');
});
