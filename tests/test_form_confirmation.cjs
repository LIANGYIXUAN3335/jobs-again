const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../extension/form_agent.js'), 'utf8');

// Exercise the public browser submit path with an Ashby confirmation region.
// Time advances locally so an ambiguous response can reach its real timeout branch.
function application({confirmation = '', disabled = false, alreadySubmitted = false} = {}) {
  let clicked = 0, now = 0, responseVisible = alreadySubmitted;
  const element = {
    getClientRects: () => [{}],
    getAttribute: () => null,
    hasAttribute: () => false,
    closest: () => null,
  };
  const name = {
    ...element, tagName: 'INPUT', type: 'text', name: 'name', id: 'name',
    value: 'Casey Example', labels: [{textContent: 'Name'}], validity: {valid: true},
  };
  const button = {
    ...element, innerText: 'Submit Application', disabled,
    form: {innerText: 'Name Submit Application', checkValidity: () => true},
    click() { clicked++; this.disabled = true; responseVisible = true; },
  };
  const heading = {
    ...element, textContent: 'Success', matches: selector => selector === 'h2',
    parentElement: {textContent: `Success ${confirmation}`},
  };
  const context = {
    crypto: {randomUUID: () => 'confirmation-test-document'},
    location: {hostname: 'jobs.ashbyhq.com', pathname: '/baseten/role/application',
      href: 'https://jobs.ashbyhq.com/baseten/role/application'},
    getComputedStyle: () => ({visibility: 'visible'}),
    Date: {now: () => now},
    setTimeout: (fn, delay) => { now += delay; fn(); },
    document: {
      body: {get innerText() { return responseVisible ? confirmation : 'Application'; }},
      querySelectorAll(selector) {
        if (selector === 'button,input[type=submit]') return [button];
        if (selector === 'h2') return responseVisible && confirmation ? [heading] : [];
        if (selector.startsWith('input,textarea,select,')) return [name];
        return [];
      },
    },
  };
  vm.runInNewContext(source, context);
  const agent = context.JobScanFormAgent;
  return {submit: () => agent.submit(agent.capture()), clicks: () => clicked};
}

for (const confirmation of [
  "Your application was successfully submitted. We'll contact you if there are next steps.",
  'Your application was submitted successfully.',
  'Your application has been received.',
]) {
  test(`accept explicit employer confirmation: ${confirmation}`, async () => {
    const page = application({confirmation});
    const result = await page.submit();
    assert.equal(result.outcome, 'submitted');
    assert.ok(result.evidence.includes(confirmation));
    assert.equal(page.clicks(), 1);
  });
}

for (const confirmation of [
  'Your application was successfully submitted. You may reapply after 6 months.',
  'Your application has been received. You have already applied to this role once this year, and you can reapply in 12 months.',
]) {
  test(`a real receipt stays a success, whatever else it says about reapplying: ${confirmation}`, async () => {
    const page = application({confirmation});
    const result = await page.submit();
    assert.equal(result.outcome, 'submitted');
    assert.ok(result.evidence.includes(confirmation));
    assert.equal(page.clicks(), 1);
  });
}

for (const confirmation of [
  'Your application is pending submission.',
  'Your application has not been submitted.',
  'Your application was not successfully submitted.',
  'Your application is still processing.',
]) {
  test(`do not treat an unfinished/failed response as success: ${confirmation}`, async () => {
    const page = application({confirmation});
    assert.equal((await page.submit()).outcome, 'unknown');
    assert.equal(page.clicks(), 1);
  });
}

test('a disabled submit button is blocked and is not a success receipt', async () => {
  const page = application({disabled: true});
  assert.equal((await page.submit()).outcome, 'blocked');
  assert.equal(page.clicks(), 0);
});

test('a button becoming disabled after a click is not success', async () => {
  const page = application();
  assert.equal((await page.submit()).outcome, 'unknown');
  assert.equal(page.clicks(), 1);
});

test('an existing Baseten success receipt prevents another click', async () => {
  const page = application({alreadySubmitted: true,
    confirmation: 'Your application was successfully submitted.'});
  assert.equal((await page.submit()).outcome, 'blocked');
  assert.equal(page.clicks(), 0);
});
