const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {File} = require('node:buffer');
const {webcrypto, createHash} = require('node:crypto');
const source = fs.readFileSync(path.join(__dirname, '../extension/form_agent.js'), 'utf8');
const hash = bytes => createHash('sha256').update(bytes).digest('hex');

function form({hidden = false, bound = true, custom = false, files = [new File(['resume bytes'], 'resume.pdf')]} = {}) {
  const input = {
    type: 'file', tagName: 'INPUT', id: 'resume', name: 'resume', files,
    form: bound ? {id: 'application'} : null, required: true,
    labels: [{textContent: 'Resume'}], validity: {valid: true},
    getClientRects: () => hidden ? [] : [{}], closest: () => null,
    hasAttribute: name => custom && name === 'role',
    getAttribute: name => custom && name === 'role' ? 'combobox' : null,
  };
  const ctx = {
    crypto: webcrypto, getComputedStyle: () => ({visibility: 'visible'}),
    location: {href: 'https://jobs.ashbyhq.com/example/role/application'},
    document: {querySelectorAll: selector =>
      selector === 'input[type=file]' || selector.startsWith('input,textarea,select,') ? [input] : []},
  };
  vm.runInNewContext(source, ctx);
  return {agent: ctx.JobScanFormAgent, input};
}

test('unprepared File has no hash; preparation hashes actual bytes', async () => {
  const {agent} = form();
  assert.equal(agent.capture().attachments[0].sha256, '');
  await agent.prepareAttachments();
  const snapshot = agent.capture();
  assert.equal(snapshot.attachments[0].sha256, hash('resume bytes'));
  assert.equal(snapshot.attachments[0].size, Buffer.byteLength('resume bytes'));
  assert.equal(snapshot.attachments[0].field_id, snapshot.fields[0].id);
});

test('same filename and size with different bytes invalidates cached approval', async () => {
  const {agent, input} = form({files: [new File(['AAAA'], 'resume.pdf')]});
  await agent.prepareAttachments();
  const before = agent.capture();
  input.files = [new File(['BBBB'], 'resume.pdf')];
  assert.equal(agent.capture().attachments[0].sha256, '');
  assert.notEqual(agent.fingerprint(agent.capture()), agent.fingerprint(before));
  await agent.prepareAttachments();
  assert.equal(agent.capture().attachments[0].sha256, hash('BBBB'));
  assert.notEqual(agent.capture().attachments[0].sha256, before.attachments[0].sha256);
});

test('hidden populated native file input bound to a form is still audited', async () => {
  const {agent} = form({hidden: true});
  await agent.prepareAttachments();
  const snapshot = agent.capture();
  assert.equal(snapshot.fields.length, 1);
  assert.equal(snapshot.fields[0].kind, 'file');
  assert.equal(snapshot.attachments[0].sha256, hash('resume bytes'));
});

test('a hidden unbound file input is not claimed as an application attachment', async () => {
  const {agent} = form({hidden: true, bound: false});
  await agent.prepareAttachments();
  assert.equal(agent.capture().attachments.length, 0);
});

test('hashing files does not declare an unfamiliar custom widget readable', async () => {
  const {agent} = form({custom: true});
  await agent.prepareAttachments();
  const snapshot = agent.capture();
  assert.equal(snapshot.coverage, 'partial');
  assert.equal(snapshot.fields[0].readable, false);
  assert.equal(snapshot.fields[0].kind, 'custom');
});

test('every native File is reported so an extra attachment cannot disappear', async () => {
  const {agent} = form({files: [new File(['resume'], 'resume.pdf'), new File(['unknown'], 'extra.pdf')]});
  await agent.prepareAttachments();
  const attachments = agent.capture().attachments;
  assert.equal(attachments.length, 2);
  assert.equal(attachments[1].name, 'extra.pdf');
  assert.equal(attachments[1].sha256, hash('unknown'));
});

test('a byte-read failure leaves attachment evidence incomplete', async () => {
  const unreadable = {name: 'resume.pdf', size: 20, arrayBuffer: async () => { throw Error('unreadable'); }};
  const {agent} = form({files: [unreadable]});
  await agent.prepareAttachments();
  assert.equal(agent.capture().attachments[0].sha256, '');
});

test('a file replaced while hashing does not inherit the previous hash', async () => {
  let resolve;
  const previous = {name: 'resume.pdf', size: 4, arrayBuffer: () => new Promise(done => { resolve = done; })};
  const {agent, input} = form({files: [previous]});
  const preparation = agent.prepareAttachments();
  input.files = [new File(['BBBB'], 'resume.pdf')];
  resolve(new TextEncoder().encode('AAAA').buffer);
  await preparation;
  assert.equal(agent.capture().attachments[0].sha256, '');
});
