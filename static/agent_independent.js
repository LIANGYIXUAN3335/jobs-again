// A browser-owned worker. No Codex tool calls or chat process participates in this loop.
(() => {
  const $ = id => document.getElementById(id);
  const token = document.querySelector('meta[name=review-token]').content;
  const worker = crypto.randomUUID();
  let busy = false, executing = false, feedSignature = '';
  async function request(path, body, method = 'POST') {
    const response = await fetch(path, {method, headers:{'Content-Type':'application/json','X-JobScan-Review':token},
      ...(method === 'GET' ? {} : {body:JSON.stringify(body || {})})});
    const data = await response.json();
    if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '服务未能完成请求。');
    return data;
  }
  const queue = (path, body, method) => request('/demo/api/agent-lab/runner/' + path, body, method);
  const reviewer = (path, body) => request('/api/agent-lab/' + path, body);
  function render(state) {
    const labels = {running:'执行中',pausing:'本份结束后停止',needs_user:'已暂停，等待处理',completed:'独立练习已完成',idle:'等待合格任务',paused:'尚未启动 / 已停止'};
    $('state').textContent = labels[state.phase] || state.phase;
    $('start').disabled = executing || !!state.active || state.enabled;
    const events = state.events || [];
    const signature = JSON.stringify(events);
    if (signature !== feedSignature) {
      feedSignature = signature; $('feed').replaceChildren();
      for (const event of events) {
        const row = document.createElement('div'); row.className = 'message ' + event.role;
        const avatar = document.createElement('div'); avatar.className = 'avatar';
        avatar.textContent = {filler:'F',reviewer:'R',system:'↳'}[event.role] || '↳';
        const content = document.createElement('div'), title = document.createElement('b'), text = document.createElement('div');
        title.textContent = {filler:'Filler',reviewer:'Reviewer',system:'调度器'}[event.role] || '调度器';
        text.className = 'body'; text.textContent = event.text;
        content.append(title,text); row.append(avatar,content); $('feed').append(row);
      }
      $('count').textContent = events.length; $('feed').scrollTop = $('feed').scrollHeight;
    }
  }
  async function execute(claim) {
    const lease = {worker_id:worker,lease_id:claim.lease_id,token:claim.token};
    let eventChain = Promise.resolve(), current = null;
    const event = e => { eventChain = eventChain.then(() => queue('event',{...lease,role:e.role,text:String(e.text).slice(0,2000)}));
      eventChain.catch(() => {}); if(e.result)$('model').textContent=e.result.model || '审查未完成'; };
    let outcome;
    try {
      let frame = $('fixture').contentWindow;
      const deadline = Date.now() + 20000;
      while (!frame.JobScanRunFill && Date.now() < deadline) { await new Promise(r=>setTimeout(r,200)); frame=$('fixture').contentWindow; }
      if (!frame.JobScanRunFill) throw Error('练习表单未能装载。');
      frame.JobScanDemo.configure('validation');
      const reviewRequest = async (route,snapshot,run) => {
        current = await reviewer(route,{job_id:'demo-001',demo:true,apply_profile:'',snapshot,
          run_id:run?.id || null,submit_authorized:true}); return current;
      };
      outcome = await JobScanWorkflow.run({
        fill:()=>frame.JobScanDemo.fill(),prepare:()=>frame.JobScanFormAgent.prepareAttachments?.(),
        capture:()=>frame.JobScanFormAgent.capture(),
        review:(s,r)=>reviewRequest('review',s,r),confirm:(s,r)=>reviewRequest('confirm',s,r),permit:(s,r)=>reviewRequest('submit-permit',s,r),
        planRepair:(snapshot,run)=>reviewer('repair-plan',{job_id:'demo-001',demo:true,snapshot,run_id:run.id}),
        repair:async(s,fixes)=>{const changes=frame.JobScanFormAgent.patch(s,fixes);await new Promise(r=>setTimeout(r,300));return changes;},
        preflight:s=>frame.JobScanFormAgent.preflight(s),submit:s=>frame.JobScanFormAgent.submit(s),
        report:async(result,run)=>{current=await reviewer('submission-result',{...result,run_id:run.id,attempt_id:run.attempt_id});return current;},event
      },{autoSubmit:true});
    } catch(error) { outcome = {status:'unknown',reason:'执行中断，停止重试：'+error.message}; }
    await eventChain;
    const terminal = ['submitted','needs_user','blocked','error','unknown'].includes(outcome.status) ? outcome.status : 'needs_user';
    const final = await queue('finish',{...lease,outcome:terminal,evidence:outcome.reason || terminal});
    $('result').hidden=false;
    $('result').textContent=terminal==='submitted' ? '独立闭环完成：执行器自行领取任务、填表、复核并处理错误，取得本机练习成功回执。没有真实投递。' : outcome.reason;
    render(final);
  }
  async function tick() {
    if (busy) return;
    busy = true;
    try {
      const state = await queue('status',null,'GET'); render(state);
      if (state.enabled && !state.active) {
        const claim = await queue('claim',{worker_id:worker});
        if (claim.status === 'claimed') {executing=true;try{await execute(claim);}finally{executing=false;}}
      }
    } catch(error) { $('state').textContent='已暂停：'+error.message; }
    finally { busy=false; }
  }
  $('start').onclick=async()=>{if(executing)return;$('start').disabled=true;try{render(await queue('start',{apply_profile:'casey',max_jobs:1}));await tick();}catch(e){$('state').textContent=e.message;$('start').disabled=false;}};
  $('pause').onclick=async()=>{try{render(await queue('pause',{}));}catch(e){$('state').textContent=e.message;}};
  // Status refresh is independent of the long-running worker, so real messages remain visible.
  setInterval(()=>queue('status',null,'GET').then(render).catch(()=>{}),2000);
  setInterval(tick,3000); tick();
})();
