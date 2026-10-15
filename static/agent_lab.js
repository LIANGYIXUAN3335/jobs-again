const $ = s => document.querySelector(s);
const token = $('meta[name=review-token]').content;
let currentRun = null, approvedSnapshot = null, running = false, messages = 0;
const frame = () => $('#fixture').contentWindow;
const capture = () => frame().JobScanFormAgent.capture();
const same = (a,b) => JSON.stringify(a) === JSON.stringify(b);
async function api(path, body) {
  const r = await fetch('/api/agent-lab/' + path, {method:'POST',headers:{'Content-Type':'application/json','X-JobScan-Review':token},body:JSON.stringify(body || {})});
  const data = await r.json();
  if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求未完成。');
  return data;
}
function message(role, text, issues=[], snapshot=null) {
  $('#feed .empty')?.remove();
  const div = document.createElement('div'); div.className = 'message ' + role;
  const avatar = document.createElement('div'); avatar.className='avatar'; avatar.textContent={filler:'F',reviewer:'R',system:'↳'}[role];
  const content = document.createElement('div'), line = document.createElement('div'); line.className='byline';
  const name = document.createElement('b'); name.textContent={filler:'Filler · 插件执行',reviewer:'Reviewer · 独立检查',system:'调度记录'}[role];
  const time = document.createElement('small'); time.textContent=new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
  line.append(name,time); const body = document.createElement('div');body.className='body';body.textContent=text;
  content.append(line,body);
  for(const issue of issues){const box=document.createElement('div');box.className='issue';const label=document.createElement('b');label.textContent=snapshot?.fields.find(x=>x.id===issue.field_id)?.label||'页面检查';const desc=document.createElement('p');desc.textContent=issue.reason;box.append(label,desc);if(issue.replacement!=null){const code=document.createElement('code');code.textContent=`${snapshot?.fields.find(x=>x.id===issue.field_id)?.value || '空白'} → ${issue.replacement}`;box.append(code);}content.append(box);}
  div.append(avatar,content);$('#feed').append(div);$('#feed').scrollTop=$('#feed').scrollHeight;$('#count').textContent=++messages;
}
function stage(name){let before=true;for(const x of document.querySelectorAll('[data-stage]')){x.className=x.dataset.stage===name?'active':before?'done':'';if(x.dataset.stage===name)before=false;}}
function result(text,warn=false){$('#result').hidden=false;$('#result').className=warn?'warn':'';$('#result').textContent=text;}
function body(snapshot){return {job_id:'demo-001',demo:true,snapshot,run_id:currentRun?.id||null};}
async function invalidate(){if(currentRun){try{await api('invalidate/'+currentRun.id);}catch{}}approvedSnapshot=null;}
$('#run').onclick=async()=>{
  if(running)return;running=true;$('#run').disabled=true;$('#reset').disabled=true;$('#result').hidden=true;
  try{
    await invalidate();currentRun=null;
    frame().JobScanDemo.configure($('#scenario').value);
    const request=async(route,snapshot,run)=>{currentRun=await api(route,{...body(snapshot),run_id:run?.id||null,submit_authorized:true});return currentRun;};
    const outcome=await JobScanWorkflow.run({
      fill:()=>frame().JobScanDemo.fill(), capture,
      review:(s,r)=>request('review',s,r), confirm:(s,r)=>request('confirm',s,r),permit:(s,r)=>request('submit-permit',s,r),
      prepare:()=>frame().JobScanFormAgent.prepareAttachments?.(),
      planRepair:(snapshot,run)=>api('repair-plan',{...body(snapshot),run_id:run.id}),
      repair:async(s,fixes)=>{const changes=frame().JobScanFormAgent.patch(s,fixes);await new Promise(r=>setTimeout(r,300));return changes;},
      preflight:s=>frame().JobScanFormAgent.preflight(s), submit:s=>frame().JobScanFormAgent.submit(s),
      report:async(outcome,run)=>{currentRun=await api('submission-result',{...outcome,run_id:run.id,attempt_id:run.attempt_id});return currentRun;},
      event:e=>{if(e.stage)stage(e.stage);$('#status').textContent=e.stage==='submit'?'等待提交回执':'协作运行中';
        if(e.result)$('#model').textContent=e.result.model||'模型未完成';message(e.role,e.text,e.issues||[],e.snapshot);}
    },{autoSubmit:true});
    $('#status').textContent=outcome.status==='submitted'?'练习提交成功':'需要处理';
    if(outcome.status==='submitted')stage('done');
    result((outcome.status==='submitted'?'闭环完成 · 这是本机练习，没有真实投递。 ':'已暂停 · ')+(outcome.reason||''),outcome.status!=='submitted');
  }catch(e){await invalidate();$('#status').textContent='等待处理';message('system',e.message);result(e.message,true);}
  finally{running=false;$('#run').disabled=false;$('#reset').disabled=false;}
};
$('#reset').onclick=async()=>{if(running)return;running=true;try{await invalidate();$('#fixture').src='/agent-lab/fixture';currentRun=null;$('#feed').replaceChildren();messages=0;$('#count').textContent='0';$('#result').hidden=true;$('#status').textContent='等待运行';document.querySelectorAll('[data-stage]').forEach(x=>x.className='');}finally{running=false;}};
setInterval(async()=>{if(!approvedSnapshot||running)return;if(!same(capture(),approvedSnapshot)){approvedSnapshot=null;$('#status').textContent='页面已改变';result('原检查结果已失效。修改后的表单需要重新检查。',true);message('system','检测到页面字段变化，撤销旧的通过状态。');try{await api('invalidate/'+currentRun.id);}catch{}}},700);
$('#pair').onclick=async()=>{try{const r=await api('pair-code');$('#pairing').hidden=false;$('#pair-code').textContent=r.code;}catch(e){result(e.message,true);}};
