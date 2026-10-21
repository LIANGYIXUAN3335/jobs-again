const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../extension/content.js'), 'utf8');
function section(start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `Missing production section: ${start}`);
  return source.slice(a, b);
}
const profile = {
  first_name: 'Casey', preferred_name: 'Casey', legal_first_name: 'Alex',
  last_name: 'Example', email: 'casey@example.test', address: '',
};
function fixture(fields) {
  const elements = fields.map((f, i) => ({
    id: `question-${i}-random-uuid`, name: '', placeholder: 'Type here...', value: '',
    offsetParent: {}, ...f,
    getAttribute(name) { return (this.attrs || {})[name] || null; },
    closest(selector) { return selector === 'label' && this.wrapped ? {innerText: this.label} : null; },
    focus() {}, blur() {},
  }));
  const ctx = {
    document: {
      querySelector(selector) {
        const element = elements.find(e => selector === `label[for="${e.id}"]` && !e.ariaOnly);
        return element ? {innerText: element.label} : null;
      },
      getElementById(id) {
        const element = elements.find(e => e.attrs?.['aria-labelledby'] === id);
        return element ? {textContent: element.label} : null;
      },
      querySelectorAll: () => elements,
    },
    CSS: {escape: s => s},
    setNativeValue: (el, value) => { el.value = value; },
    humanPause: async () => {}, isDropdownish: () => false,
    fixPhoneCountryCode: () => false,
  };
  vm.createContext(ctx);
  vm.runInContext(section('  function fieldLabels(', '  // The phone box always'), ctx);
  vm.runInContext(section('  function staleIdentityValue(', '  // Greenhouse custom forms'), ctx);
  return {ctx, elements};
}

test('Ashby random ID + bare Name is filled from the selected card before AI fallback', async () => {
  const {ctx, elements} = fixture([{label: 'Name *'}, {label: 'Current Company Name'}]);
  const result = await ctx.fillFields(profile);
  assert.equal(elements[0].value, 'Casey Example');
  assert.equal(ctx.classify(elements[0]), 'full_name');
  assert.equal(elements[1].value, '');
  assert.ok(result.filled.includes('full_name'));
});

test('ordinary, preferred, and legal name fields retain distinct meanings', async () => {
  const {ctx, elements} = fixture([
    {label: 'First name'}, {label: 'Full name'}, {label: 'Legal first name'},
    {label: 'Legal full name'}, {label: 'Preferred first name'},
  ]);
  await ctx.fillFields(profile);
  assert.deepEqual(elements.map(e => e.value), ['Casey', 'Casey Example', 'Alex', 'Alex Example', 'Casey']);
  assert.equal(ctx.valueFor('first_name', {first_name: 'Jordan', legal_first_name: 'Jamie'}), 'Jordan');
});

test('associated accessible labels work without treating pronouns or company names as names', () => {
  const {ctx, elements} = fixture([
    {label: 'Name:', ariaOnly: true, attrs: {'aria-label': 'Name:'}},
    {label: 'Name *', ariaOnly: true, attrs: {'aria-labelledby': 'visible-name'}},
    {label: 'Preferred Pronouns'}, {label: 'Company Name'}, {label: 'Name of referral'},
  ]);
  assert.deepEqual(elements.map(e => ctx.classify(e)), ['full_name', 'full_name', null, null, null]);
});

test('existing user-entered name remains unchanged', async () => {
  const {ctx, elements} = fixture([{label: 'Name', value: 'My chosen name'}]);
  await ctx.fillFields(profile);
  assert.equal(elements[0].value, 'My chosen name');
});

test('Google ordinary first name uses Casey, explicit legal first name uses Alex', async () => {
  const {ctx, elements} = fixture([{label: 'First name'}, {label: 'Legal first name'}]);
  ctx.document.querySelectorAll = selector => selector === 'input[jsname="YPqjbf"]' ? elements : [];
  ctx.gLabel = el => el.label;
  ctx.tickConsents = () => {};
  vm.runInContext(section('  async function fillGoogle(', '  // ================= SmartRecruiters'), ctx);
  await ctx.fillGoogle(profile, () => {});
  assert.deepEqual(elements.map(e => e.value), ['Casey', 'Alex']);
});

test('SmartRecruiters ordinary first-name input uses the selected card', async () => {
  const {ctx} = fixture([]);
  const first = {value: ''};
  Object.assign(ctx, {
    location: {pathname: '/apply'}, deepAll: () => [],
    srInner: id => id === 'first-name-input' ? first : null,
    srVis: () => true,
    srNativeSet: (el, value) => { el.value = value; return true; },
  });
  vm.runInContext(section('  async function fillSmartRecruiters(', '  // SmartRecruiters page 2'), ctx);
  await ctx.fillSmartRecruiters({profile}, () => {});
  assert.equal(first.value, 'Casey');
});

test('Workday explicit legalName controls still use the legal name', async () => {
  const {ctx} = fixture([]);
  const first = {value: ''};
  Object.assign(ctx, {
    wdById: id => id === 'legalName--firstName' ? first : null,
    wdSetText: (el, value) => { if (!el || !value) return false; el.value = value; return true; },
    nationalPhone: () => '', stateName: () => '', escapeRe: s => s,
    wdPrompt: async () => '', wdHowDidYouHear: async () => {}, wdChoice: async () => '',
  });
  vm.runInContext(section('  async function wdMyInformation(', '  async function wdExperience('), ctx);
  await ctx.wdMyInformation(profile, {}, () => {});
  assert.equal(first.value, 'Alex');
});

test('a field asking for both names gets both, the legal pair when it says legal', async () => {
  const {ctx, elements} = fixture([{label: 'Legal First & Last Name'}, {label: 'Legal First and Last Name'}, {label: 'Preferred First & Last Name'},
    {label: 'First and Last Legal Name'}, {label: 'First & Last Name'}, {label: 'First and Last Name'}, {label: 'First name'}, {label: 'Last name'}]);
  await ctx.fillFields(profile);
  assert.deepEqual(elements.map(e => e.value), ['Alex Example', 'Alex Example', 'Casey Example', 'Alex Example', 'Casey Example', 'Casey Example', 'Casey', 'Example']);
});

const here = {...profile, location: 'San Jose, CA', country: 'United States'};

test('a place field only takes where she lives', async () => {
  const notHers = ['Country of citizenship', 'What is your country of citizenship?', "Which country's passport do you hold?", 'Country of birth', 'Native country',
    'City of birth', 'Is this location acceptable?', 'Are you willing to relocate to this location?', 'Do you live in the same city as our office?',
    'Are you available to work on-site at the office location listed in the job posting?', 'Office location',
    'Do you require Visa sponsorship to work in your selected location? If so, which one? And when does your Visa expire?',
    'Country of previous residence', 'Country of visa issuance'];
  const {ctx, elements} = fixture(notHers.map(label => ({label})));
  await ctx.fillFields(here);
  assert.deepEqual(elements.map(e => e.value), notHers.map(() => ''));
  assert.deepEqual(elements.map(e => ctx.classify(e)), notHers.map(() => null));
  const hers = ['Location', 'City', 'Current City & State', 'Current location', 'Your Location', 'Where are you based?', 'City of residence', 'Country', 'Country *', 'Country of residence', 'Country you live in', 'Country/Region'];
  const {ctx: other, elements: boxes} = fixture(hers.map(label => ({label})));
  assert.deepEqual(boxes.map(e => other.classify(e)), hers.map(label => (/country/i.test(label) ? 'country' : 'location')));
});

test('a combobox with such a label is not given her city either', async () => {
  const {ctx, elements} = fixture([{label: 'Country of citizenship'}, {label: 'The office location listed in the job posting'}, {label: 'Location'}]);
  const typed = [];
  ctx.isDropdownish = () => true;
  ctx.pickTypeahead = async (control, el, text) => { typed.push(text); return {ok: true}; };
  ctx.pickReactSelect = async () => ({ok: false});
  ctx.cityOnly = p => p.location.split(',')[0];
  ctx.escapeRe = s => s;
  for (const element of elements) element.parentElement = {};
  await ctx.fillFields(here);
  assert.deepEqual(typed, ['San Jose']);
});

test('a box with no visible question goes by its id, unless that says otherwise', () => {
  const {ctx, elements} = fixture([{label: '', ariaOnly: true, id: 'candidate_location'}, {label: '', ariaOnly: true, id: 'country_of_birth'}]);
  elements[0].id = 'candidate location'; elements[1].id = 'country of birth';
  assert.equal(ctx.classify(elements[0]), 'location');
  assert.equal(ctx.classify(elements[1]), null);
});
