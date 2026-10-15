// Fixture-only transport adapter. The filler is the unmodified production content-script flow.
// No real profile, credentials, employer page, or business state is accessed here.
const demoProfile = {first_name:'Avery',last_name:'Chen',email:'avery@example.test',work_authorization_answer:"I'm authorized to work in the US and I don't require sponsorship."};
const demoRole = {job_id:'demo-001',company:'Northstar Labs',title:'Backend Engineer',profile:demoProfile,
  status:'new',this_status:'new',has_tailored_pdf:false,ai_enabled:false,auto_dropdowns:false,
  auto_cover_letter:false,eeo_decline:false,mailcode_enabled:false,apply_profile:'',company_applied:[]};
const demoMessages = {
  lookup: () => ({found:true,role:demoRole}),
  profile: () => ({ok:true,profile:demoProfile}),
  pins: () => ({found:true,roles:[]}),
  pinned: () => ({found:false}),
};
globalThis.JobScanDemoTransport = message => demoMessages[message.type]?.() ||
  {ok:false,error:'This action is not available in the local fixture.'};
function seedFaults() {
  document.getElementById('first_name').value = 'Avrey';
  document.getElementById('sponsor').value = 'Yes';
  document.getElementById('degree').value = 'MBA';
  document.getElementById('why').value = 'I build Python APIs and PostgreSQL-backed services. This backend role would let me apply that experience to request latency and API reliability.';
}
seedFaults();
let scenario = 'validation', submissions = 0;
const practiceForm = document.getElementById('application');
practiceForm.onsubmit = event => {
  event.preventDefault(); submissions++;
  if (scenario === 'unknown') { document.getElementById('practice-submit').disabled=true; return; }
  if (scenario === 'validation' && submissions === 1) {
    const input = document.getElementById('first_name');
    input.value = 'Avrey';
    input.setCustomValidity('Server rejected first name: expected Avery.');
    const error = document.getElementById('form-error');
    error.hidden = false; error.textContent = 'Server rejected first name: expected Avery.（预设的提交失败场景）';
    return;
  }
  practiceForm.hidden = true;
  document.querySelector('[data-application-success]').hidden = false;
};
document.getElementById('first_name').addEventListener('input', event => {
  event.target.setCustomValidity('');
  document.getElementById('form-error').hidden = true;
});
globalThis.JobScanDemo = {
  configure(value) {scenario=value;document.getElementById('practice-submit').disabled=value==='disabled';},
  async fill() {
    const start = Date.now();
    while (!globalThis.JobScanRunFill && Date.now() - start < 10000) await new Promise(r => setTimeout(r,100));
    if (!globalThis.JobScanRunFill) throw new Error('插件填表入口未装载。');
    return globalThis.JobScanRunFill();
  },
  reset(){location.reload();},
};
