// Runs the extension's own commonAnswer over a form's questions, so a preflight sees what Filler will do.
// stdin: {profile, groups: [{company, confirmed: [{topic, title_topic, description, ...}], entries: [{label, type, options, description}]}]}.
// stdout: for each group, for each entry, what Filler has to work with: {how: "confirmed" | "rule" | "",
// soft, yesno, text, choice, legal, legal_pick, picked, typed, claimed, value, place}. legal_pick comes from the
// real pickLegalOption, picked is the option the real fillRadio would click, claimed and value are what the real
// classify and valueFor give a plain box with this label, place whether the typeahead would type her city.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '../extension/content.js'), 'utf8');
const states = src.match(/^ {2}const US_STATES = \{[\s\S]*?^ {2}\};/m)[0];
// Filler's own reader of standard fields runs first and its boxes never reach the rules, so it has to run here too.
const fields = src.slice(src.indexOf('  function fieldLabels('), src.indexOf('  // The phone box always'));
const start = src.indexOf('  const clean = (s) =>');
const fn = src.indexOf('  function commonAnswer(');
const end = src.indexOf('\n  }\n', fn) + 5;
const radio = src.indexOf('  function fillRadio(radios, want) {');
const radioEnd = src.indexOf('\n  }\n', radio) + 5;
const [commonAnswer, pickLegalOption, fillRadio, classify, valueFor, WHERE_SHE_IS, YES_NO_QUESTION] = vm.runInNewContext(
  `const radioLabel = (r) => r.text; const document = {getElementById: () => null, querySelector: () => null}; const CSS = {escape: (s) => s};
   ${states}\n${fields}\n${src.slice(start, end)}\n${src.slice(radio, radioEnd)}
   [commonAnswer, pickLegalOption, fillRadio, classify, valueFor, WHERE_SHE_IS, YES_NO_QUESTION]`);
// A plain box carrying this label, as classify reads one on the page.
const box = (label) => ({name: '', id: '', placeholder: '', getAttribute: (name) => (name === 'aria-label' ? label : null), closest: () => null});

const clicked = (options, answer) => {
  let picked = null;
  fillRadio(options.map(text => ({text, click() { picked = text; }})), answer);
  return picked;
};
const {profile, groups} = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = groups.map(group => {
  // A batch has no reader behind it, and it fills this company's form.
  const own = {...profile, confirmed_answers: [], unattended: true, company_name: group.company};
  return group.entries.map(entry => {
    // A confirmed answer has to cover everything the form says, the statement under the title too.
    const whole = `${entry.label} ${entry.description || ''}`.replace(/\s+/g, ' ').trim();
    // Filler's rules read a Yes/No block's whole text without its buttons, and every other control by its label.
    const said = entry.type === 'Boolean' ? whole : entry.label;
    const fits = (pattern) => { try { return new RegExp(pattern, 'i').test(whole.toLowerCase()); } catch { return false; } };
    const exact = group.confirmed.find(known => fits(known.topic));
    // With no statement from the API, an answer she gave to this title on the page may fit. Filler holds it against the
    // words on the page and leaves the field blank if they differ, so this can only cost a stop.
    const onPage = !exact && !entry.description && group.confirmed.find(known => known.description && fits(known.title_topic));
    const item = exact || (onPage && {...onPage, topic: onPage.title_topic});
    const answer = commonAnswer(said, item ? {...own, confirmed_answers: [item]} : own, whole);
    const key = classify(box(entry.label));
    const read = {claimed: key || '', value: key ? valueFor(key, own) : '', place: WHERE_SHE_IS.test(entry.label)};
    if (!answer) return {how: '', ...read};
    return {how: item ? 'confirmed' : 'rule', soft: !!(item && item.soft), yesno: answer.yesno || '', text: !!answer.text, choice: answer.choice || '',
            legal: !!answer.legal, legal_pick: answer.legal ? pickLegalOption(entry.options, answer) : -1, picked: clicked(entry.options, answer),
            // In a text box a rule's bare Yes or No is typed only under a yes/no question.
            typed: !!answer.text || (!!answer.yesno && YES_NO_QUESTION.test(said)), ...read};
  });
});
process.stdout.write(JSON.stringify(out));
