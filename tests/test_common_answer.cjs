// commonAnswer decides the deterministic answers Filler types into real forms.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), test = require('node:test'), vm = require('node:vm');

function load() {
  const src = fs.readFileSync(path.join(__dirname, '../extension/content.js'), 'utf8');
  const exclude = src.match(/^ {2}const EXCLUDE = .*$/m)[0];
  const start = src.indexOf('  const clean = (s) =>');
  const fn = src.indexOf('  function commonAnswer(');
  const end = src.indexOf('\n  }\n', fn) + 5;
  return vm.runInNewContext(`const US_STATES = {}; ${exclude}\n${src.slice(start, end)}\ncommonAnswer`);
}
const commonAnswer = load();
const profile = {work_authorized: true, needs_sponsorship: false, location: 'San Jose, CA'};
const TRAVEL = {id: 'international-travel', topic: 'travel\\w*\\s+internationally|international travel',
  answer: "My current visa status doesn't allow international travel right now."};

test('a follow-up asking for detail does not flip "may you work" to No', () => {
  const answer = commonAnswer('Can you legally work in the US? Please explain if you require a visa.', profile);
  assert.equal(answer.yesno, 'yes');
  assert.match(answer.text, /^Yes\. /);
  assert.equal(commonAnswer('Are you authorized to work in the US? If not, please explain.', profile).yesno, 'yes');
});

test('real sponsorship questions still answer No', () => {
  for (const label of ['Will you now or in the future require sponsorship?', 'Please explain if you require sponsorship',
    'Do you need a visa to work in the US?', 'Will you require sponsorship? Please explain.']) {
    const answer = commonAnswer(label, profile);
    assert.equal(answer.yesno, 'no', label);
    assert.match(answer.text, /^No\. /, label);
  }
});

test('where-did-you-hear is Job board however long the label is', () => {
  for (const label of ['How did you hear about us?', 'How did you find this job?', 'Where did you hear about this role? ' +
    'If you heard about this role from a current Close team member, please share their name here.']) {
    assert.equal(commonAnswer(label, profile).text, 'Job board', label);
  }
});

test('questions that only look like the source question go to the AI', () => {
  for (const label of ['Where did you learn to code?', 'How did you find the last bug you fixed?', 'How did you learn Python?'])
    assert.equal(commonAnswer(label, profile), null, label);
});

test('an answer the owner confirmed beats every rule, and never becomes a guessed Yes/No', () => {
  const owner = {...profile, confirmed_answers: [TRAVEL]};
  for (const label of ['Are there any circumstances preventing you from traveling internationally to a week-long team offsite?',
    'Are you able to travel internationally?', 'Are you willing to travel internationally for work?', 'Is international travel a problem?']) {
    const answer = commonAnswer(label, owner);
    assert.equal(answer.text, TRAVEL.answer, label);
    assert.equal(answer.yesno, undefined, label);
  }
  assert.equal(commonAnswer('Are you able to travel for work?', owner).yesno, 'yes');
  assert.equal(commonAnswer('Are you able to travel internationally?', profile), null);
});

test('a remembered Yes or No can click a Yes/No control', () => {
  const owner = {...profile, confirmed_answers: [{topic: '^\\W*are\\W*you\\W*able\\W*to\\W*commute', answer: 'No', yesno: 'no'}]};
  const answer = commonAnswer('Are you able to commute to San Francisco?', owner);
  assert.equal(answer.yesno, 'no');
  assert.equal(answer.text, 'No');
});

test('wrong answers found on the real queued forms', () => {
  // Needing authorization is the sponsorship question, not "are you authorized".
  assert.equal(commonAnswer('Will you now or in the future require authorization to work in the United States?', profile).yesno, 'no');
  // The facts are about the US.
  for (const label of ['Are you legally authorized to work in Canada?', 'Will you need sponsorship to work in Saudi Arabia now or anytime in the future?',
    'Do you or will you require sponsorship to work in Iceland'])
    assert.equal(commonAnswer(label, profile), null, label);
  assert.equal(commonAnswer('Will you require relocation?', profile).yesno, 'no');
  assert.equal(commonAnswer('Are you willing to relocate to San Francisco?', profile).yesno, 'yes');
  // A source question that also asks for an essay is not a one-word answer.
  assert.notEqual(commonAnswer('How did you hear about Column? Why are you interested in this role here?', profile)?.text, 'Job board');
});

test('being somewhere outside the Bay Area is not a default yes', () => {
  for (const label of ['Are you able to work in our NYC office?', 'Are you able and willing to work onsite at our Bellevue office 4 days a week?',
    'Are you willing to relocate to Seattle?'])
    assert.equal(commonAnswer(label, profile), null, label);
  for (const label of ['Are you able to work in-person in San Francisco? (Presidio)', 'Are you open to working in-person in either San Francisco, Seattle, or New York?',
    'Are you open to being in-office 5 days a week in Sunnyvale?', 'Are you able to come into the office four days per week?'])
    assert.equal(commonAnswer(label, profile).yesno, 'yes', label);
});

test('past work at this company is No, past work with a technology is not a rule', () => {
  for (const [company, label] of [['Crusoe', 'Do you currently work for Crusoe, or have you worked for Crusoe in the past?'], ['Aurora', 'Are you a former employee of Aurora?'],
    ['Lyft', 'Have you previously worked at Lyft?'], ['Aurora', 'Are you currently performing work on behalf of Aurora as a contractor, agency worker, or third-party vendor?']])
    assert.equal(commonAnswer(label, {...profile, company_name: company}).yesno, 'no', label);
  for (const label of ['Have you ever worked with Kubernetes?', 'Have you ever worked at Meta?', 'Are you currently employed?'])
    assert.equal(commonAnswer(label, profile), null, label);
});

test('optional interview recording is declined however long the label is', () => {
  const answer = commonAnswer('We use Metaview, an AI notetaking tool, to streamline interviews by recording and summarizing discussions, enabling interviewers to be fully present without manual notetaking.', profile);
  assert.equal(answer.yesno, 'no');
});

function buttonsHarness(question, {required = true, index = 0, answer = null} = {}) {
  const src = fs.readFileSync(path.join(__dirname, '../extension/content.js'), 'utf8');
  const helpers = src.slice(src.indexOf('  // A required Ashby choice no rule can answer'), src.indexOf('  async function aiPickCombo('));
  const fn = src.indexOf('  async function fillAshbyButtons(profile, log, role) {');
  const sent = [], clicked = [];
  const label = {className: required ? '_required_x' : ''};
  const entry = {className: '_fieldEntry_x', innerText: question + ' Yes No', querySelector: () => label, querySelectorAll: () => []};
  const button = text => ({offsetParent: {}, textContent: text, className: '', parentElement: entry, getAttribute: () => null,
    closest: () => entry, click() { clicked.push(text); }});
  const ctx = {document: {querySelectorAll: () => [button('Yes'), button('No')]}, Map, commonAnswer: () => answer, SENSITIVE: /salary/i, EXPORT_CONTROL: /export control/i, IS_ASHBY: true,
    short: s => s, sleep: async () => {}, send: async m => { sent.push(m); return {index}; }};
  return {run: role => vm.runInNewContext(helpers + src.slice(fn, src.indexOf('\n  }\n', fn) + 5) + '\nfillAshbyButtons', ctx)({}, () => {}, role), sent, clicked};
}

test('a required Yes/No no rule answers goes to the model, and -1 leaves it blank', async () => {
  const role = {job_id: 'j', ai_enabled: true};
  let h = buttonsHarness('We work from our offices on Anchor Days.');
  await h.run(role);
  assert.deepEqual([...h.clicked], ['Yes']);
  assert.equal(h.sent[0].type, 'pickOption');
  assert.deepEqual([...h.sent[0].options], ['Yes', 'No']);
  h = buttonsHarness('Do you speak Spanish?', {index: -1});
  await h.run(role);
  assert.equal(h.clicked.length, 0);
  h = buttonsHarness('Optional question?', {required: false});
  await h.run(role);
  assert.equal(h.sent.length, 0);
  h = buttonsHarness('Anything?');
  await h.run({job_id: 'j', ai_enabled: false});
  assert.equal(h.sent.length, 0);
  h = buttonsHarness('A sentence answer?', {answer: {text: "I can't."}});
  await h.run(role);
  assert.equal(h.sent.length + h.clicked.length, 0);
});

const unattended = {...profile, unattended: true};
// Same answer whether or not she reads the form first.
const inBothModes = (label, check, extra = {}) => {
  check(commonAnswer(label, {...profile, ...extra}), label);
  check(commonAnswer(label, {...unattended, ...extra}), label);
};
const blank = (answer, label) => assert.equal(answer, null, label);
const yes = (answer, label) => assert.equal(answer?.yesno, 'yes', label);
const no = (answer, label) => assert.equal(answer?.yesno, 'no', label);

test('a statement to sign is a Yes only when she reads the form first', () => {
  for (const label of ['I certify that all information in this application is true.', 'Do you agree to our Code of Conduct?',
    'Do you accept the Applicant Privacy Notice?', 'I confirm that I have not used AI tools to complete this application.',
    'Please confirm before submitting.', 'I acknowledge the terms above.', 'Do you consent to our data policy?']) {
    assert.equal(commonAnswer(label, profile)?.yesno, 'yes', label);
    assert.equal(commonAnswer(label, unattended), null, label);
  }
});

test('a statement of her status is never a formality', () => {
  for (const label of ['I confirm that I am a U.S. citizen or permanent resident.', 'Please confirm your current visa status.',
    'I certify that I hold a green card.', 'I confirm that I hold a U.S. passport.'])
    inBothModes(label, blank);
});

test('extra agreements are never signed for her', () => {
  for (const label of ['Are you willing to sign a confidentiality agreement?', 'Are you willing to sign a non compete?', 'Are you willing to sign an NDA?',
    'Do you agree to a non-compete?', 'I accept the terms of the mutual arbitration agreement.', 'Do you agree to our non-disclosure terms?'])
    inBothModes(label, blank);
});

test('a place the lists do not hold is not a Yes', () => {
  for (const label of ['Are you able to work from our Boulder office?', 'Are you able to work from our Reston, VA office?',
    'Can you work from our Nashville office 3 days a week?', 'Are you willing to work onsite in Texas?', 'are you willing to work onsite in texas?',
    'Are you willing to work in-person in Cambridge, MA?', 'Can you work from our Dublin office?', 'Are you able to work onsite in NY?',
    'Are you able to commute to Sacramento?', 'Are you able to work in Washington state?', 'Can you work from our HQ in Reston, VA?',
    'Are you willing to work from our Boston-based office?', 'Are you based in Austin?', 'This role is in Denver. Are you open to that?'])
    inBothModes(label, blank);
});

test('a Bay Area word beside the real place does not excuse it', () => {
  for (const label of ['If not located in SF, are you willing to relocate to New York?', 'Our offices are in SF and NYC. Are you willing to relocate to NYC?',
    'Are you willing to work in our SF office, or relocate to London?', 'If you are not in the Bay Area, can you work from our Austin office?',
    'Are you able to work from our Austin office if not located in San Francisco?', 'Our offices are in SF and NYC. Can you work from our NYC office?'])
    inBothModes(label, blank);
  // A choice that includes the Bay Area is still a true Yes.
  for (const label of ['Are you open to working in-person in either San Francisco, Seattle, or New York?', 'Are you able to work from our Santa Monica office or, if not, from San Jose?',
    'If applying for Bay Area or NYC Metro role, are you available to work onsite at the corporate office?'])
    inBothModes(label, yes);
});

test('relocating is a Yes only to a Bay Area place named', () => {
  for (const label of ['Open to relocation if necessary:', 'Are you open to relocation?', 'Are you willing to relocate for this role?',
    'Would you be willing to relocate to our headquarters?', 'Are you willing to relocate to New Jersey?', 'Are you willing to relocate within the US?',
    'Are you able to work in the location listed for this position, or are you willing to relocate if you are not?'])
    inBothModes(label, blank);
  for (const label of ['Are you willing to relocate to San Francisco?', 'Are you located in or willing to relocate to the SF Bay Area?',
    'Are you currently based in or open to relocating to the San Francisco Bay Area?', 'Are you located in the San Francisco-area and/or willing to relocate?',
    'If not currently in the Bay Area, are you willing to relocate near our Foster City, CA Office?', 'Are you able and willing to work from our San Francisco HQ 5 days a week (or relocate if needed)?',
    'If not located in SF, are you willing to relocate?'])
    inBothModes(label, yes);
  // She already lives here, unless the move is to somewhere else.
  for (const label of ['Will you require relocation?', 'Do you require relocation support?', 'Will you need to relocate?'])
    inBothModes(label, no);
  for (const label of ['Will you require relocation to New York?', 'Do you need relocation support to work in our Austin office?'])
    inBothModes(label, blank);
});

test('days in office and on-site with no place named stay a Yes in every mode', () => {
  for (const label of ['Are you able to come into the office four days per week?', 'Are you willing and able to work in the office 3 to 4 days per week?',
    'Are you able to work from our office Monday, Tuesday, and Thursday, in alignment with our hybrid work model? *', 'Are you available to work on-site at the office location listed in the job posting?',
    'We love working together in person and ask our employees to be onsite 5 days a week. Are you comfortable with that?', 'Are you excited to work in-office five days a week?',
    'Are you able to work onsite in the US?', 'Are you able to commute to our office?', 'Are you able to work onsite in California?'])
    inBothModes(label, yes);
  for (const label of ['Are you able to work in San Francisco?', 'Are you able to work from GridCARE\'s offices in Redwood City three days per week?',
    'I am comfortable coming into the Mountain View, CA office at minimum 3 days a week.', 'Are you able to commute to San Francisco M-F?',
    'Are you willing to work on-site in our San Francisco office Monday-Friday?', 'Confirming that you\'re willing and able to work from our Presidio-based office 4-5 days per week?'])
    inBothModes(label, yes);
});

test('the company is not mistaken for a place, once it is known', () => {
  const label = 'Are you able to work from a Sierra office listed for this role?';
  inBothModes(label, yes, {company_name: 'Sierra'});
  inBothModes(label, blank);
  inBothModes('Are you able to work at Scribe 3 days a week in the office?', yes, {company_name: 'Scribe'});
});

test('living in the US is Yes only when nothing else is claimed', () => {
  for (const label of ['Are you living in the U.S. on a visa?', 'Do you live in the US and hold a green card?', 'Are you based in the US on an H-1B?',
    'Are you located in the US outside of California?', 'Are you located in the US on the East Coast?', 'Are you based in the United States Eastern time zone?',
    'Do you reside in the United States as a citizen?', 'Are you located in the US, Canada, or Mexico?'])
    inBothModes(label, blank);
  for (const label of ['Do you currently reside in the US?', 'Are you located in the United States of America?', 'Are you located in the United States or Canada?',
    'Are you located in the U.S.?', 'Are you located in the USA?', 'Are you currently residing in the U.S.? *', 'Do you live in the United States'])
    inBothModes(label, yes);
});

test('citizenship and birth are not where she lives', () => {
  for (const label of ['Country of citizenship', 'What is your country of citizenship?', 'Which country\'s passport do you hold?', 'Country of birth',
    'Country of origin', 'Country of nationality', 'In which country were you born?', 'If no, is your home country listed below?'])
    inBothModes(label, blank);
  for (const label of ['Country of residence', 'Country', 'Which country do you reside in?'])
    inBothModes(label, (answer, l) => assert.equal(answer?.text, 'United States', l));
});

test('work authorization anywhere but the US is not answered from US facts', () => {
  for (const label of ['Are you authorized to work in Korea?', 'Are you authorized to work in Taiwan?', 'Are you authorized to work in China?',
    'Are you authorized to work in EU?', 'Are you legally eligible to work in Europe?', 'Are you eligible to work in Argentina?',
    'Do you have work authorization in Switzerland?', 'Will you need sponsorship to work in Sweden?', 'Are you able to work in the EU without sponsorship?',
    'Are you authorized to work in the Philippines?', 'Will you require sponsorship to work in Vietnam?', 'Do you hold an EU work permit?'])
    inBothModes(label, blank);
  for (const label of ['Are you authorized to work in the country in which this role is based?', 'Do you have the right to work in the country you are applying from?',
    'Are you authorized to work in this country?', 'Are you legally authorized to work in the U.S.?'])
    inBothModes(label, yes);
  for (const label of ['Do you require sponsorship by an employer for your right to work in your current location?',
    'Will you now or in the future require sponsorship in order to employ you?'])
    inBothModes(label, no);
});

test('past employment is No only about this company', () => {
  for (const label of ['Have you ever been employed in a backend role?', 'Have you ever worked at a FAANG company?', 'Have you previously worked for a big tech company?',
    'Prior to this, have you been employed in a similar role?', 'Have you previously worked at a Series A startup?', 'Have you ever worked for US companies?',
    'Prior to applying here, have you been employed in a similar role?']) {
    inBothModes(label, blank);
    inBothModes(label, blank, {company_name: 'Aurora'});
  }
  // A company the label names is not the one applied to.
  inBothModes('Have you worked at Google before?', blank, {company_name: 'Aurora'});
  inBothModes('Are you a former employee of Aurora?', blank, {company_name: 'Crusoe'});
  inBothModes('Are you a former employee of Aurora?', blank);
  inBothModes('Have you worked at Google before?', no, {company_name: 'Google'});
  // Forms drop the suffix, and a company named like a word does not swallow the word.
  inBothModes('Are you a former employee of Genius?', no, {company_name: 'Genius AI'});
  inBothModes('Have you ever worked for Scribd?', no, {company_name: 'Scribd, Inc.'});
  inBothModes('Have you ever worked for Recall?', no, {company_name: 'Recall.ai'});
  inBothModes('Have you previously worked at a factory?', blank, {company_name: 'Factory'});
  // Us, here and our company need no name.
  for (const label of ['Have you ever worked with us?', 'Have you previously been employed by us in any capacity?', 'Have you ever worked here?', 'Have you worked for us before?',
    'Have you ever worked for this company?', 'Have you previously worked at our company?', 'Are you a former employee?', 'Are you a current or former employee?'])
    inBothModes(label, no);
  inBothModes('Have you ever worked at Meta?', blank, {company_name: 'Meta'});
});

test('facts not on file get no answer', () => {
  for (const label of ['Are you able to obtain a security clearance?', 'Are you available to start immediately?', 'Can you start within two weeks?', 'Are you able to start on Monday?',
    'Can you start by January?', 'Ideal start date in office', 'Are you willing to undergo a drug test?', 'Are you willing to undergo a background check?',
    'Do you consent to a background check?', 'Are you able to provide proof of citizenship?', 'Are you over 18 years of age and eligible for a clearance?',
    'Do you have a friend who works here?', 'Do you have a friend or relative employed here?'])
    inBothModes(label, blank);
  inBothModes('Are you related to a current employee?', no);
  inBothModes('Do you have a family member who works here?', no);
});

test('travel abroad or to a named place is not a Yes', () => {
  for (const label of ['Are you willing to travel to Europe quarterly?', 'Are you willing to travel to Asia for team offsites?', 'Are you able to travel to our London office?',
    'Are you willing to travel to New York monthly?', 'Are you willing to travel internationally?', 'Are you able to travel overseas?', 'Are you able to travel abroad for work?'])
    inBothModes(label, blank);
});

test('the owner\'s other standing answers hold in every mode', () => {
  for (const label of ['Are you able to travel for work?', 'Are you willing to travel?', 'Are you able to travel 50% of the time?',
    'Are you willing to be on call?', 'Do you have experience using our product?', 'Are you open to working remotely?'])
    inBothModes(label, yes);
});

test('generic willingness is a Yes only when she reads the form first', () => {
  for (const label of ['Are you ready to take a coding assessment?', 'Are you open to a contract role?',
    'Are you able to perform the essential functions of this job?', 'Are you comfortable working in Eastern time hours?']) {
    assert.equal(commonAnswer(label, profile)?.yesno, 'yes', label);
    assert.equal(commonAnswer(label, unattended), null, label);
  }
});

// Every real sponsorship and authorization label from the cached forms. These must never move.
const REAL_SPONSORSHIP = [
  "Do you now or in the future require Visa Sponsorship?",
  "Will you now or in the future require sponsorship for employment visa status (e.g., H-1B, TN, etc.)?",
  "Will you now or in the future require sponsorship for employment visa status (e.g., H-1B, O-1, etc.)?",
  "Will you require immigration sponsorship now or in the future?",
  "Will you require work sponsorship now and/or in the future?",
  "Will you now or in the future require sponsorship for employment visa status? Please note: candidates working on F-1 OPT or STEM OPT should answer \"No\" only if they will never require employer sponsorship in the future.",
  "Will you now or in the future require sponsorship for employment in the United States?",
  "Do you require or will you require visa sponsorship to work in the United States?",
  "Do you now or in the future require immigration support or visa sponsorship to continue working in the United States?",
  "Will you now or in the future require sponsorship to maintain employment at Scribe?",
  "Do you now, or will you at any point in the future, require sponsorship?",
  "Will you, now or in the future, require employer sponsorship to work in the United States?",
  "Will you now or in the future require immigration sponsorship to work for Crusoe?",
  "Will you now or in the future require any immigration related support or sponsorship from Hinge Health? This could include support (such as J-1, F-1 CPT letter or STEM OPT Training Plan), visa sponsorship for employment (H-1B, H-1B1, E-3, O-1, or TN), and/or any EAD holders that will need longer term employment authorization (temporary work visa or permanent residency). By selecting this option, you certify that this information provided regarding your sponsorship needs is true and accurate. Providing false or misleading information may result in the withdrawal of a job application or termination of employment.",
  "Will you now or in the future require visa sponsorship to work in the country where this position is located?",
  "Will you now or in the future require employer sponsorship for employment visa status?",
  "Lambda may agree to sponsor individuals to obtain work authorization (such as H-1B, TN or O-1 visa status). Will you now or in the future require sponsorship from Lambda to maintain your work authorization in the U.S.?",
  "Will you now or in the future require visa sponsorship to work in the country where this role is located?",
  "Will you now, or in the future, require Aurora to sponsor you for an employment-based visa in order to employ you (e.g. H-1B visa)?",
  "Will you now or in the future require visa sponsorship to work in the United States?",
  "Now or in the future, will you require sponsorship or employer support (for example, filing or supporting a visa petition, application, extension, or transfer) in order to begin or continue working for Genius AI?",
  "Will you now, or in the future, require sponsorship for employment visa status (e.g. H-1B visa status)?",
  "Will you now or in the future require employer sponsorship for employment visa status (e.g., H-1B)?",
  "Will you now or in the future require sponsorship to work in the United States (e.g., H-1B visa)?",
  "Will you now or in the future require sponsorship for employment visa status in the United States?",
  "Will you now or will you in the future require employment visa sponsorship?",
  "Do you require Visa sponsorship to work in your selected location? If so, which one? And when does your Visa expire?",
  "Will you now or in the future require visa sponsorship for employment in the United States?",
  "Do you require sponsorship by an employer for your right to work in your current location?",
  "Will you now or in the future require visa sponsorship to work in the country this role is located?",
  "Will you now or in the future require immigration sponsorship?",
  "Will you now or in the future require sponsorship to work in the US?",
  "Will you now or in the future require employer sponsorship to work in the country where this position is located?",
  "Will you require visa sponsorship to work in the United States now or in the future?",
  "Will you now, or in the future, require sponsorship (i.e. H-1B visa, etc.) to legally work in the U.S.?",
  "Will you now, or sometime in the future, require visa sponsorship?",
  "Will you require work visa sponsorship now or anytime in the future?",
  "Will you now or in the future require employer sponsorship to obtain or maintain authorization to work in the United States (e.g., H-1B, TN, O-1, etc.)?",
  "Will you now or in the future require Illumio to commence (‘sponsor’) an immigration case in order to employ you?",
  "If you answered ‘Yes’ to requiring sponsorship now or in the future, please feel free to provide additional details (Optional)",
  "Will you require sponsorship or transfer of sponsorship to obtain or extend your right to work in the United States?",
];
const REAL_AUTHORIZATION = [
  "Are you legally authorized to work in the United States?",
  "Are you authorized to work lawfully in the United States for Short Story?",
  "Are you legally authorized to work in the U.S. for any employer without requiring sponsorship now or in the future?",
  "Are you authorized to work in the United States?",
  "Are you currently legally authorized to work in the US?",
  "Are you authorized to work for any employer in the United States of America?",
  "Are you currently authorized to work in the United States?",
  "Are you legally authorized to work in the country where this role is located?",
  "Are you currently authorized to work lawfully in the United States?",
  "Are you legally authorized to work in the United States for Genius AI?",
  "Work Authorization",
  "Are you legally authorized to work in the US for any employer?",
  "Are you legally authorized to work in the country where this role is located, for any employer?",
  "Are you authorized to work in the U.S. for Tessera Labs?",
  "U.S. Work Authorization Status",
  "Are you legally authorized to work in the United States without the need for employer visa sponsorship, now or in the future?",
  "Are you authorized to work lawfully in the country for the position in which you are applying within Illumio?",
  "Are you legally authorized to work in the United States for any employer?",
];

test('every real sponsorship label answers No and every real authorization label Yes', () => {
  assert.equal(REAL_SPONSORSHIP.length, 41);
  assert.equal(REAL_AUTHORIZATION.length, 18);
  for (const label of REAL_SPONSORSHIP)
    inBothModes(label, (answer, l) => { assert.equal(answer?.yesno, 'no', l); assert.equal(answer?.legal, 'spon', l); });
  for (const label of REAL_AUTHORIZATION)
    inBothModes(label, (answer, l) => { assert.equal(answer?.yesno, 'yes', l); assert.equal(answer?.legal, 'auth', l); });
});

// ---- found by the independent check of the first round ----
test('sponsorship wording: "without sponsorship" is the authorization question, and a statement asks nothing', () => {
  for (const label of ['Can you work in the US without sponsorship?', 'Are you able to work in the United States without sponsorship?',
    'Can you work for any employer in the US without visa sponsorship?',
    'Are you legally authorized to work in the United States without the need for current or future employer visa sponsorship?',
    'Are you authorized to work in the US and do not require sponsorship?'])
    inBothModes(label, (answer, l) => { assert.equal(answer?.yesno, 'yes', l); assert.equal(answer?.legal, 'auth', l); });
  for (const label of ['I do not require sponsorship.', 'Do you confirm you do not require sponsorship?', 'We do not offer visa sponsorship. Is that OK?',
    'This role does not offer sponsorship. Are you still interested?', 'Sponsorship is not available. Do you still wish to apply?', 'We cannot sponsor visas.',
    'I will never require sponsorship.'])
    inBothModes(label, blank);
  // A real question after the statement still has its answer.
  inBothModes('We do not offer visa sponsorship. Will you now or in the future require sponsorship?', no);
  for (const label of ['Sponsorship', 'Visa sponsorship', 'Do you need sponsorship?', 'Sponsorship required?']) inBothModes(label, no);
  inBothModes('Do you have sponsorship?', blank);
});

test('"authorized to work" answers that one fact, not a status or anything else said with it', () => {
  for (const label of ['Are you a lawful permanent resident authorized to work in the United States?', 'Are you a U.S. citizen with the right to work in the US?',
    'Are you authorized to work in the US as a citizen or permanent resident?', 'Are you authorized to work in the US and are you a US citizen?',
    'Are you legally authorized to work in the US and do you hold an active security clearance?', 'Are you authorized to work in the US through your own green card?',
    'Are you authorized to work in the US on a permanent basis?', 'Are you eligible to start immediately?',
    'Are you authorized to work in the US and willing to undergo a background check?', 'Are you not authorized to work in the United States?',
    'Are there any restrictions on your right to work in the US?'])
    inBothModes(label, blank);
  // Real authorization wording keeps its answer, including this form of "begin".
  for (const label of ['Are you legally eligible to begin employment in the United States?', 'Are you authorized to work in the US? If not, please explain.'])
    inBothModes(label, yes);
});

const pickLegal = (() => {
  const src = fs.readFileSync(path.join(__dirname, '../extension/content.js'), 'utf8');
  const exclude = src.match(/^ {2}const EXCLUDE = .*$/m)[0];
  const start = src.indexOf('  const clean = (s) =>'), fn = src.indexOf('  function commonAnswer(');
  return vm.runInNewContext(`const US_STATES = {}; ${exclude}\n${src.slice(start, src.indexOf('\n  }\n', fn) + 5)}\npickLegalOption`);
})();
const OWNER_TERMS = {okTerms: ['EAD', 'employment authorization document', 'adjustment of status', 'pending'],
  wrongTerms: ['citizen', 'permanent resident', 'green card holder', 'LPR', 'H-1B', 'TN', 'OPT', 'CPT', 'L-1', 'O-1', 'non-immigrant visa', 'student visa', 'F-1']};

test('a legal pick never lands on an option claiming a status she does not hold', () => {
  const auth = {...commonAnswer('Are you authorized to work in the US?', profile), ...OWNER_TERMS};
  for (const options of [['Yes - I have a green card', 'No'], ['Yes - GC', 'No'], ['Yes - H1B', 'No'], ['Yes - H1-B', 'No'], ['Yes - U.S. national', 'No'],
    ['Yes - naturalized', 'No'], ['Yes (TS/SCI cleared)', 'No'], ['Yes, I hold a passport from this country', 'No'], ['Yes, permanently', 'Yes, temporarily', 'No'],
    ['Yes - Green Card', 'No'], ['Yes - DACA', 'No'], ['Yes - asylee', 'No'], ['Yes - refugee', 'No'], ['Yes - E-3', 'No'], ['Yes - J-1', 'No']])
    assert.equal(pickLegal(options, auth), -1, options[0]);
  const spon = {...commonAnswer('Will you now or in the future require sponsorship?', profile), ...OWNER_TERMS};
  assert.equal(pickLegal(['Yes', 'No - I have a green card'], spon), -1);
  // What she can truly say still lands.
  assert.equal(pickLegal(['Yes', 'No'], auth), 0);
  assert.equal(pickLegal(['Yes, now', 'Yes, in the future', 'No'], spon), 2);
  assert.equal(pickLegal(['Yes, I will require sponsorship', 'No, I do not require sponsorship to work in the United States'], spon), 1);
  assert.equal(pickLegal(['I require sponsorship', 'I am authorized to work for any employer in the country in which this position is based'], auth), 1);
  assert.equal(pickLegal(['Yes - I hold an EAD', 'No'], auth), 0);
  assert.equal(pickLegal(['YES: If you currently hold a valid work permit (such as OPT, CPT, or an existing H-1B transfer).', 'NO: If you do not have any active, legal authorization.'], auth), 0);
});

test('work rights anywhere but the US, however it is said', () => {
  for (const label of ['Do you have a Swiss work permit?', 'Do you hold a valid British work permit?', 'Do you have German work authorization?', 'Do you have Italian work eligibility?',
    'Are you authorized to work in the US and Portugal?', 'Are you authorized to work within Portugal?', 'Are you eligible to work at our Lisbon office?',
    'Do you have the right to work across APAC?', 'Work authorization - EMEA', 'Work eligibility: Philippines',
    'Will you need sponsorship to work out of our Zurich office?', 'Would you need visa sponsorship to join our Berlin team?', 'Will you require visa support to relocate to Dublin?',
    'Do you need sponsorship to work at our Lisbon location?', 'Sponsorship (Philippines)'])
    inBothModes(label, blank);
  // Who she would work for is not where.
  inBothModes('Will you now or in the future require sponsorship to maintain employment at Scribe?', no);
  inBothModes('Do you need sponsorship to work at Scribe?', no, {company_name: 'Scribe'});
  inBothModes('Are you authorized to work within the United States?', yes);
  inBothModes('Are you authorized to work in the US or Canada?', yes);
});

test('export control with a hyphen is still export control', () => {
  for (const label of ['Will you require an export license to access export-controlled technology?', 'Are you subject to export-control restrictions?',
    'Are you permitted to work with export-controlled technology?'])
    inBothModes(label, blank);
  inBothModes('Are you a citizen or resident of an export-controlled country?', no);
  inBothModes('Are you a citizen or resident of a sanctioned country such as Iran?', no);
});

test('a status or a licence beside a soft Yes stops it', () => {
  for (const label of ['Are you at least 18 years old and a U.S. citizen?', "Are you over 18 and do you have a valid driver's license?",
    'Are you able to work on-site at a facility restricted to U.S. citizens?', 'Are you willing to work on-site if you hold a green card?',
    'Are you able to travel with a valid passport?', 'Do you have a valid passport and are you willing to travel?', 'Are you at least 18 years old and able to lift 50 pounds?'])
    inBothModes(label, blank);
  inBothModes('Are you at least 18 years of age?', yes);
});

test('any place word stops a working-arrangement Yes, in any case and past the cut', () => {
  for (const label of ['are you able to work from our boulder office?', 'ARE YOU ABLE TO WORK FROM OUR BOULDER OFFICE?', 'Are you able to work from our office in downtown Boulder?',
    'Are you able to work onsite out of Boulder 3 days a week?', 'Are you able to work on-site within Boulder?', 'Can you commute into Tampa?', 'Can you reliably commute to downtown Tampa?',
    'Do you live within commuting distance of Reston, VA?', "Are you able to work from Nashville's office?", 'Hybrid - Tampa, FL. Are you comfortable with this?', 'Onsite (Boulder, CO): acceptable?',
    'Are you comfortable with a hybrid schedule? (Tampa, FL)', 'Are you comfortable working on-site outside the Bay Area?', 'Can you work on-site in a different state?',
    'Are you willing to work on-site in another city?', 'Can you commute out of state?', 'Are you able to work on-site on the East Coast?', 'Are you able to work on-site in Århus?',
    'Are you able to work in boulder?', 'ARE YOU ABLE TO WORK IN TAMPA?', 'Are You Able To Work From Our Boulder Office?'])
    inBothModes(label, blank);
  inBothModes(`Are you able to work on-site in the office every day, ${'and we mean every single day of the week, '.repeat(8)}in Boulder?`, blank);
  // Her own places and an unnamed office are untouched, in any case.
  for (const label of ['ARE YOU ABLE TO WORK FROM OUR SAN FRANCISCO OFFICE?', 'are you able to work from our office?', 'Are You Able To Work From Our Office?',
    'are you able to work in the office 3 days a week?', 'Are you able to work from our San José office?'])
    inBothModes(label, yes);
});

test('a Bay Area word only counts when it is where the work is', () => {
  for (const label of ['Are you able to work from Boston rather than San Francisco?', 'Are you able to work from our Austin office instead of San Francisco?',
    'Can you work from our Austin HQ (not San Francisco)?', "Can you work from our New York office if you're based in San Francisco?", 'Are you willing to work from Austin while living in San Jose?',
    'Can you commute weekly from the Bay Area to our Seattle office?', 'If you are outside San Francisco, can you work from Boston?', 'Are you able to work from our San Jose, Costa Rica office?',
    'Are you able to work on-site in Fremont, Ohio?', 'Are you able to work on-site in the Tampa Bay Area?', 'Can you work from our office in the South Bay of Los Angeles?'])
    inBothModes(label, blank);
  for (const label of ['Are you able to work from Boston or San Francisco?', 'Can you work from San Francisco or New York?', 'Are you able to work on-site in Fremont, CA?',
    'Are you able to work on-site in the South Bay?', 'Are you able to work on-site in the San Francisco Bay Area?'])
    inBothModes(label, yes);
});

test('relocating away, anywhere or to a namesake is not a Yes', () => {
  for (const label of ['Would you relocate away from the Bay Area?', 'Are you willing to relocate out of the Bay Area?', 'Are you open to relocating outside San Francisco?',
    'Are you willing to relocate from San Jose for this role?', 'Are you open to relocating anywhere in the US, including San Francisco?', 'Are you willing to relocate to Fremont, Nebraska?',
    'Are you willing to relocate to San Jose, Costa Rica?', 'Are you willing to relocate to the Tampa Bay Area?'])
    inBothModes(label, blank);
  inBothModes('Are you willing to relocate within the Bay Area?', yes);
});

test('travel abroad, however it is said', () => {
  for (const label of ['Are you willing to travel to a foreign country?', 'Are you able to travel out of the country for work?', 'Are you able to travel to team offsites outside the country?',
    'Are you willing to travel outside of North America?', 'Are you able to travel across APAC?', 'Are you willing to travel by plane to customer sites across the world?',
    'Are you willing to travel across borders for this role?', 'Are you able to travel to any of our global offices?', 'Are you willing to travel to offsites in other countries?'])
    inBothModes(label, blank);
  for (const label of ['Are you willing to travel across the US?', 'Are you able to travel to customer sites in the US?']) inBothModes(label, yes);
});

test('remote: open to it is a Yes, a residency rule or a need for it is not', () => {
  for (const label of ['This remote role requires you to live in Texas. Do you?', 'This remote role requires residency in New York State. Do you meet this?',
    'We can only hire remote employees in the following states: TX, FL, NY. Do you live in one of these states?', 'This is a remote role for candidates in Colombia. Do you qualify?',
    'Remote - LATAM only. Do you qualify?', 'Are you remote in Portugal?', 'Do you require a fully remote role?', 'Do you only want remote work?', 'Will you need to work remotely?'])
    inBothModes(label, blank);
  for (const label of ['Are you open to working remotely?', 'Are you comfortable with remote work?', 'Are you willing to work remotely?',
    'I am comfortable with the fact that this position requires being in one of our offices 3-4 days per week and is not a remote position.'])
    inBothModes(label, yes);
});

test('words of refusal around an arrangement are not a Yes', () => {
  for (const label of ['Do you object to working on-site?', 'Is there any reason you cannot work on-site?', 'Is there anything preventing you from working on-site?',
    'Do you have any restrictions on commuting?', 'Do you have any limitations that would prevent you from commuting to the office?', 'Would a hybrid schedule be a problem for you?',
    'Are you unwilling to work on-site?', 'I am unable to come into the office.', "I'm not able to work on-site.", 'Do you require an accommodation to work on-site?',
    'Do you need disability accommodations for in-office work?'])
    inBothModes(label, blank);
});

test('a keyword in an experience question is not an arrangement', () => {
  for (const label of ['Do you have experience with hybrid cloud deployments?', 'Have you operated hybrid cloud or on-prem Kubernetes clusters?', 'Have you built hybrid search systems?',
    'Have you built remote procedure call (RPC) frameworks?', 'Have you implemented remote attestation?', 'Have you worked on commutative data types (CRDTs)?',
    'Have you conducted on-site customer deployments?', 'Have you worked on the platform team at Google?', 'Are you certified in Office 365 administration?',
    'Have you led an on-call rotation of 10+ engineers?', 'Preferred days in office', 'Commute distance (miles)', 'Commute time in minutes', 'Years of on-call experience',
    'Remote work preference', 'Hybrid schedule preference', 'On-site or remote?', 'Preferred work arrangement (remote, hybrid, on-site)'])
    inBothModes(label, blank);
  for (const label of ['Are you commutable to San Francisco 3x per week?', 'Would you be open to being in office every day?', 'Can you work hybrid?'])
    inBothModes(label, yes);
});

test('a statement to sign stops every soft Yes in a batch', () => {
  for (const label of ['By submitting, you agree to our hybrid work policy and our code of conduct.', 'You acknowledge that this remote position is at-will employment.',
    'Applicants must accept the on-call policy and the Employee Handbook.', 'Please confirm you agree to our code of conduct and will work on-site.',
    'Please confirm you are authorized to work in the US and that you agree to the code of conduct.', 'Please confirm you are authorized to work in the US and that you have never been convicted of a felony.'])
    assert.equal(commonAnswer(label, unattended), null, label);
  inBothModes('Please confirm you are authorized to work in the United States.', yes);
  inBothModes('Please confirm you can work from our San Francisco office 3 days a week.', yes);
});

test('only a country she lives in now is the United States', () => {
  for (const label of ['Native country', 'Country of naturalization', "Country where you earned your bachelor's degree", 'Country of previous residence', 'Country of last employment outside the US',
    'Country of visa issuance', 'Issuing country of your ID', 'Number of years you have lived in this country', 'Have you lived in another country?', 'Do you have family in another country?'])
    inBothModes(label, blank);
  for (const label of ['Country', 'Country *', 'Country of residence', 'Current country of residence', 'Which country do you reside in?', 'Country you are located in', 'Country/Region'])
    inBothModes(label, (answer, l) => assert.equal(answer?.text, 'United States', l));
});

test('past or someone else\'s sponsorship, and family elsewhere, are on no file', () => {
  for (const label of ['Have you ever been sponsored for a work visa by an employer?', 'Has a previous employer sponsored your H-1B?', 'Is your current employer sponsoring your green card?',
    'Are you currently being sponsored by your employer?', 'Do you hold a visa that was sponsored by an employer?', 'Do you have a family member who works in government?',
    'Do you have a spouse who works for a competitor?', 'Are you related to anyone in politics?'])
    inBothModes(label, blank);
  inBothModes('Are you related to anyone at Acme?', no, {company_name: 'Acme'});
  inBothModes('Do you have a relative employed by our company?', no);
  inBothModes('Are you related to anyone who works here?', no);
});

test('No to past employment needs this company as who she worked for', () => {
  for (const label of ['Have you ever been employed by a competitor of this company?', "Have you ever worked for our company's competitors?", "Have you previously worked for the company's customers or partners?"])
    inBothModes(label, blank);
  for (const [company, label] of [['Lambda', 'Have you ever worked at a company that deployed on AWS Lambda?'], ['LangChain', 'Have you ever worked at a company that uses LangChain in production?'],
    ['Google', 'Have you ever been employed at a Google competitor?'], ['Acme', 'Have you ever worked at a Fortune 500 company such as Acme?']])
    inBothModes(label, blank, {company_name: company});
  inBothModes('Have you ever worked at Lambda?', no, {company_name: 'Lambda'});
  inBothModes('Have you ever been employed by the company?', no);
});

test('only a real recording consent gets the opt-out, and opt-in means the word', () => {
  inBothModes('Do you have a track record of conducting technical interviews?', blank);
  inBothModes('Have you ever recorded an interview?', blank);
  inBothModes('Are you adopting AI tools at work?', blank);
  for (const label of ['May we record your interview?', 'Interview Recording Consent', 'Do you consent to this interview being recorded?', 'Do you consent to recording?']) inBothModes(label, no);
  inBothModes('Marketing opt-in', no);
  inBothModes('Do you opt in to receive our newsletter?', no);
});

test('the state rule reads only a question about her state', () => {
  for (const label of ['State your reason for applying', 'State your earliest availability', 'State of licensure', "State where you hold a driver's license"])
    inBothModes(label, blank);
  for (const label of ['State', 'State *', 'State/Province', 'State of residence', 'Which state, province, or region do you currently live in?'])
    inBothModes(label, (answer, l) => assert.equal(answer?.text, 'CA', l), {state: 'CA'});
});

test('questions the first round refused without need have their answer back', () => {
  for (const label of ['Will you now or in the foreseeable future require sponsorship?', 'Will you require visa sponsorship now or in the next three years?',
    'Will you require sponsorship in connection with this job?', 'Do you need visa sponsorship to work in any US state?'])
    inBothModes(label, no);
  for (const label of ['Are you authorized to work in a full-time capacity in the US?', 'Are you authorized to work in California?', 'Are you authorized to work in the Bay Area?',
    'Do you currently reside in the US? (Required for this role)', 'Are you currently located in the United States? We only hire in the US.', 'Are you able to work from our San José office?'])
    inBothModes(label, yes);
  // A second question after it is not a note.
  inBothModes('Do you live in the US? Do you hold a green card?', blank);
});

test('the owner\'s interview-language answer stays on its own topic', { skip: !fs.existsSync(path.join(__dirname, '../data/agent_lab/confirmed_answers.casey.json')) }, () => {
  const data = JSON.parse(fs.readFileSync(path.join(__dirname, '../data/agent_lab/confirmed_answers.casey.json'), 'utf8'));
  const owner = {...profile, confirmed_answers: data.answers.filter(item => item.id === 'q-3fd65a4ef969')};
  for (const label of ['Programming language you prefer to avoid', 'For our interview, list all programming languages you have 5+ years with.',
    'Before your interview, which programming languages have you used professionally?', 'Which programming language do you prefer for production backend work?',
    'Coding language you would not like to use for the interview', 'Interviewers will ask about the programming languages you used in production',
    'What are your strongest programming languages and technical areas? Please list', 'What programming languages do you have 5+ years of professional experience in?'])
    assert.notEqual(commonAnswer(label, owner)?.text, 'Python', label);
  for (const label of ['What is your preferred programming language for interviews?', 'What is your preferred coding language?', 'Preferred programming language',
    'Which programming language do you prefer for your coding interview?', 'What language would you prefer to use in the coding interview?', 'Which coding language do you want to interview in?'])
    assert.equal(commonAnswer(label, owner)?.text, 'Python', label);
});

// What a confirmed answer has to cover: all the form says at that control.
const noted = (question, description = '') => {
  const words = `${question} ${description}`.toLowerCase().match(/\w+/g);
  return {id: 'q', answer: 'Yes', yesno: 'yes', topic: `^\\W*${words.join('\\W*')}\\W*(?:required\\W*)?$`};
};
const STATEMENT = 'I authorize this company to run a credit check.';
test('an answer to a bare title does not reach the statement under that title on another form', () => {
  const owner = {...profile, confirmed_answers: [noted('GDPR Acknowledgement')]};
  assert.equal(commonAnswer('GDPR Acknowledgement', owner, `GDPR Acknowledgement ${STATEMENT}`), null);
  assert.equal(commonAnswer('GDPR Acknowledgement', owner)?.yesno, 'yes', 'the same bare title still gets it');
  const signed = {...profile, confirmed_answers: [noted('GDPR Acknowledgement', STATEMENT)]};
  assert.equal(commonAnswer('GDPR Acknowledgement', signed, `GDPR Acknowledgement ${STATEMENT}`)?.yesno, 'yes');
  assert.equal(commonAnswer('GDPR Acknowledgement', signed), null, 'and it says nothing about the title alone');
});
test('an answer is held against the whole text: yes and no inside it count, and so does what comes after 300 characters', () => {
  const owner = {...profile, confirmed_answers: [noted('Family notice', 'I have relatives working here.')]};
  assert.equal(commonAnswer('Family notice I have no relatives working here.', owner), null);
  assert.equal(commonAnswer('Family notice I have relatives working here.', owner)?.yesno, 'yes');
  const opening = 'Records and clause '.repeat(20);
  const signed = {...profile, confirmed_answers: [noted('Notice', `${opening} Records stay in California.`)]};
  assert.equal(commonAnswer(`Notice ${opening} Records go to our partners in any country.`, signed), null);
  assert.equal(commonAnswer(`Notice ${opening} Records stay in California.`, signed)?.yesno, 'yes');
});

test('a question stating the interview language is a Yes in both modes: she agreed to Go or Java if a company insists', () => {
  for (const label of ['Our coding interview is in Java. Are you comfortable with this?', 'Our technical interviews are conducted in Go. Are you comfortable interviewing in Go?',
    'This role requires Java for the coding interview. Are you OK with that?', 'Are you willing to complete the coding interview in Java?', 'Are you willing to do the interview in Go?',
    'Do you agree to complete the coding assessment in Golang?', 'Our coding assessment is in Python. Are you okay with this?', 'Are you able to interview in Java?',
    'We interview in java. Is that okay?', 'Our Go coding interview runs 45 minutes. Are you comfortable with that?', 'Our coding round uses Java. Are you okay with this?']) {
    inBothModes(label, yes);
    inBothModes(label, yes, {company_name: 'Acme'});
  }
});

test('that rule stops at the question: her record, other languages and anything more stay with her', () => {
  for (const label of ['Do you have experience with Java?', 'How comfortable are you with Java?', 'Are you comfortable with Java?', 'Are you comfortable writing Java?', 'Are you comfortable coding in Java?',
    'How comfortable are you coding in Java?', 'Do you have experience with coding interviews in Java?', 'Have you used Java in an interview?',
    'Are you comfortable with 5 years of production coding in Java?', 'Rate your comfort with Java for the coding interview', 'Which language will you use for the interview?',
    'Are you comfortable with a coding interview in JavaScript?', 'Are you comfortable interviewing in Rust?', 'Our coding interview is not in Java. Are you comfortable with this?',
    'Our coding interview isn\'t in Python. Are you comfortable with this?', 'Are you comfortable interviewing in Java in our Boston office?',
    'Are you willing to relocate for a coding interview in Java?', 'Our coding interview is in Java.', 'Do you know Java?', 'Java'])
    inBothModes(label, blank);
  // "go" as a plain word is no language: the rule below it still treats this as an ordinary soft Yes.
  assert.equal(commonAnswer('Are you willing to go through a coding interview?', unattended), null);
});

// Each of these got a Yes in a batch while the rule was a list of words to refuse. Whatever else the sentence holds is a second thing to agree to.
test('the interview-language Yes takes nothing but the language: anything more in the sentence stays with her', () => {
  for (const label of ["Do you object to interviewing in Python? It's okay if you do.", 'Do you need an accommodation for the Python assessment? We are able to help.',
    'Are you able to lead a Java team? This will be assessed in the interview.', 'Are you a Java certified engineer able to interview this week?',
    'Were you able to finish the Python assessment we sent last week?', "Are you someone who failed our Python interview before? It's okay.",
    'Are you willing to waive your right to a jury trial and interview in Java?', 'Are you OK with a Java assessment and $90k base?', 'Are you willing to pay a $50 fee for the Java assessment?',
    'Are you comfortable interviewing in Python given our 8+ yrs requirement?', 'Are you able to attend an in-person coding round in Python at our HQ?', 'Are you okay to interview with Go-Jek?',
    'Do you require an interview in Java? We are okay with this.'])
    inBothModes(label, blank);
});

test('the same hard rules, worded another way', () => {
  for (const label of ['Sponsorship is not something we offer. OK?', 'We are unable to sponsor.', 'Are you okay with no sponsorship?', 'Do you understand we do not provide visa sponsorship?',
    'Does this company sponsor visas?', 'Have you required sponsorship before?', 'Are you authorized to work in the US for the next 3 years?', 'Do you have work authorization that does not expire?',
    'Is your work authorization tied to your employer?', 'Are you authorized to work in the US on a visa?', 'Are you authorized to work for our Prague office?', 'Are you authorized to work in New Zealand?',
    'Are you able to work on-site in Santa Clara, Cuba?', 'Are you able to work on-site in Oakland, NJ?', 'Are you able to work on-site in Fremont (NE)?', 'Are you able to work from San Francisco and Boston?',
    'Are you willing to commute to San Francisco from Sacramento?', 'Are you able to work on-site somewhere other than San Francisco?', 'Are you willing to work on-site beyond the Bay Area?',
    'Are you willing to work on-site elsewhere?', 'Are you able to work from any of our offices?', 'Are you willing to move?', 'Are you willing to move to Boston?',
    'Are you willing to travel to Canada?', 'Are you willing to travel to all of our offices?', 'Are you able to travel to our other offices?', 'Do you have any travel restrictions?',
    'Are you able to travel without restrictions?', 'Are you open to remote work only?', 'Are you able to work remotely from Texas?', 'Are you currently working remotely?',
    'Will you be working remotely or on-site?', 'Are you comfortable with remote attestation?', 'Are you comfortable with hybrid cloud?', 'Are you comfortable with Office 365?',
    'Would you have difficulty commuting?', 'Country where the office is located', 'Is anyone in your family employed by a government?', 'Are you related to a current employee of Google?',
    'Are you a former employee of a company that competes with Acme?'])
    inBothModes(label, blank, {company_name: 'Acme'});
  inBothModes('Do you have work authorization for Portugal?', blank, {company_name: 'Acme'});
  inBothModes('We don\'t sponsor visas. Do you need a visa?', no);
  inBothModes('Are you willing to move to San Jose?', yes);
  assert.equal(commonAnswer('Applicant agrees to work on-site.', unattended), null);
});

test('a bad pattern is ignored', () => {
  assert.equal(commonAnswer('Are you authorized to work in the US?', {...profile, confirmed_answers: [{topic: '(', answer: 'x'}]}).yesno, 'yes');
});
