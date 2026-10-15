(() => {
  const $ = id => document.getElementById(id);
  const token = document.querySelector('meta[name="review-token"]').content;
  const names = {system:'Gatekeeper / 系统',filler:'Filler',reviewer:'Reviewer'};
  const phases = {idle:'等待符合条件的简历',paused:'尚未启动或已暂停',pausing:'当前申请完成后暂停',running:'正在执行',completed:'本轮已完成',needs_user:'需要处理后再继续'};
  // Why a whole batch stopped by itself. Each of these wants her to look before the next one goes out.
  const pauses = {spam_flag:'这一轮停了：雇主的回复提到疑似垃圾申请或申请太多。先看一眼下面的原因，再决定要不要开下一轮。',
    too_many_stops:'这一轮停了：连续 3 份都停下了，多半有东西变了。先看一眼下面的原因，再开下一轮。'};
  let signature = '', polling = false, jobsKey = null, ready = false, formsReading = false;
  const drafts = new Map();
  async function api(path, body, method) {
    const result = await fetch('/api/agent-lab/'+path,{method:method||(body===undefined?'GET':'POST'),cache:'no-store',
      headers:{'Content-Type':'application/json','X-JobScan-Review':token},...(body===undefined?{}:{body:JSON.stringify(body)})});
    const data=await result.json();
    // A service restart ends this page's session; reload once to get a new one instead of showing an error.
    if(result.status===403&&!sessionStorage.getItem('boardReloaded')){sessionStorage.setItem('boardReloaded','1');location.reload();}
    if(!result.ok)throw Error(typeof data.detail==='string'?data.detail:'请求未完成');
    sessionStorage.removeItem('boardReloaded');return data;
  }
  const make = (tag, className, text) => {const node=document.createElement(tag);if(className)node.className=className;if(text!=null)node.textContent=text;return node;};
  const button = (className, text, onclick) => {const node=make('button',className,text);node.type='button';node.onclick=onclick;return node;};
  const note = text => {$('answers-note').textContent=text||'';};
  function showAnswers(data){renderBatch(data.batch);renderPaused(data.paused);renderAnswers(data.answers);note('');}
  async function changeAnswers(path, body, method){try{showAnswers(await api(path,body,method));loadPreflight();}catch(e){note(e.message);}}
  // Whether a stopped job can run depends on the queue and the forms too, so these cards follow them.
  const reloadAnswers = () => api('answers').then(showAnswers).catch(e=>note(e.message));
  function syncButtons(){
    for(const node of document.querySelectorAll('.retry')){node.disabled=!ready;node.title=ready?'':'Chrome 执行台在线、没有申请在跑时才能重跑。';}
    for(const id of ['start','start-5','start-custom'])$(id).disabled=!ready;
  }
  function question(job, q){
    const box=make('div','question');box.append(make('p','q-label',q.label));
    if(q.description)box.append(make('p','hint',q.description));
    if(q.answer){box.append(make('p','q-done','✓ 已记住：'+q.answer));return box;}
    box.append(make('p','q-reason',q.reason));
    // The words she would be signing are on the employer's page only, so there is nothing here to answer yet.
    if(q.on_page){
      box.append(make('p','hint','这句声明只写在申请页上，这里看不到，所以不能在这里答。这份要你自己打开申请页，读完再填、再提交。'));
      if(q.url){const link=make('a','','打开申请页');link.href=q.url;link.target='_blank';link.rel='noopener noreferrer';box.append(link);}
      return box;
    }
    const key=job.job_id+'\n'+q.label+(q.description?'\n'+q.description:''),row=make('div','q-input');
    // The statement goes along: the answer covers this question with these words, and no other under the same title.
    const save=answer=>{drafts.delete(key);return changeAnswers('answers',{question:q.label,description:q.description||'',answer,job_id:job.job_id,company:job.company||''});};
    if(q.options&&q.options.length&&q.options.length<=12){for(const option of q.options)row.append(button('secondary',option,()=>save(option)));}
    else{
      // Shown, not prefilled: that text is the model's or the page's, and one click would make it her answer for every company.
      if(q.value)box.append(make('p','hint','页面上现在是：'+[...String(q.value)].slice(0,200).join('')));
      const input=make('textarea');input.rows=2;input.value=drafts.get(key)??'';input.setAttribute('aria-label',q.label);
      input.oninput=()=>drafts.set(key,input.value);
      row.append(input,button('primary','记住',()=>{if(input.value.trim())save(input.value);}));
    }
    box.append(row);return box;
  }
  // What each outcome means, and what the owner can do about it.
  const outcomes = {
    submitted:['已投出','good','雇主已确认收到，不用处理。'],
    expired:['已下架','good','岗位关了，不用处理。'],
    needs_user:['停下了','bad','到下面「需要你处理」看一眼，那里写着现在该做什么。'],
    blocked:['停下了','bad','没有投出去。看上面的原因，没问题就在下面点重跑。'],
    unknown:['结果不明','bad','先查申请页和 candidate 邮箱，确认有没有投出去。这份不能重跑。'],
    error:['程序出错','bad','把这一行告诉我，修好后可以在下面点重跑。'],
    claimed:['正在跑','good','让申请页留在前台。'],
  };
  function renderBatch(list){
    $('batch-card').hidden=!list.length;const box=$('batch-rows');box.replaceChildren();
    const count=status=>list.filter(row=>row.status===status).length,running=count('claimed');
    const refused=list.filter(row=>row.evidence.startsWith('雇主拒收')).length,sent=count('submitted')+list.filter(row=>row.went_out).length;
    const stopped=list.length-sent-count('expired')-running-refused;
    $('batch-summary').textContent=`这一轮到目前 ${list.length} 份：独立投出 ${sent} 份，雇主拒收 ${refused} 份，已下架 ${count('expired')} 份，停下 ${stopped} 份`+(running?'，还有 1 份在跑。':'。');
    for(const row of list){
      const [word,tone,standard]=row.went_out?['已投出','good','读到了雇主的回执，已经记为已投。这一页可以不管了，不用再提交。']:row.evidence.startsWith('雇主拒收')?['雇主拒收','bad',/possible spam|too many applications/i.test(row.evidence)?'雇主说疑似垃圾申请或申请太多，这一轮已经停了。先看一眼再开下一轮。':'雇主没有收这份，重投也一样，不用处理。']:
        row.clicked&&row.status!=='submitted'&&row.status!=='claimed'?(row.status==='unknown'?outcomes.unknown:row.needs_code?['等验证码','bad','已经点过提交，雇主还要一个邮箱验证码才算投完。回到那一页，点右上角面板的「📧 Get email code」把验证码填进去，再点一次提交。这份不能重跑。']:['停下了','bad','已经点过提交，不能重跑。打开申请页核对一下。']):
        outcomes[row.status]||[row.status,'bad',''],help=row.note||standard,node=make('div','batch-row');
      const title=make('p','job');title.append(make('span','result '+tone,word),`${row.company} · ${row.title}`);
      const why=row.went_out?'':row.unanswered?`卡在：有 ${row.unanswered} 道题没有确认过的答案。`:row.evidence&&row.status!=='claimed'?(row.status==='submitted'?'雇主回执：':'卡在：')+row.evidence.replace(/^雇主拒收：/,'').slice(0,400):'';
      node.append(title);if(why)node.append(make('p','why',why));if(help)node.append(make('p','help',help));
      if(row.clicked){const sent=make('details'),list=make('div');sent.append(make('summary','','看这份填了什么'),list);
        sent.ontoggle=async()=>{if(!sent.open||list.childElementCount)return;
          try{const data=await api('sent/'+encodeURIComponent(row.job_id));
            for(const item of data.sent){const line=make('div','ledger-row'),text=make('div');text.append(make('p','q-label',item.label),make('p','',item.value||'（空）'));line.append(text);list.append(line);}
            if(!data.sent.length)list.append(make('p','hint','这份是加留底之前投的，没有副本。'));
          }catch(e){list.append(make('p','hint',e.message));}};
        node.append(sent);}
      box.append(node);
    }
  }
  function renderPaused(list){
    const box=$('paused');box.replaceChildren();$('paused-card').hidden=!list.length;
    for(const job of list){
      const card=make('div','paused-job');card.append(make('h3','',`${job.company} · ${job.title}`));
      if(job.note)card.append(make('p','q-reason',job.note));
      else if(!job.questions.length&&job.evidence)card.append(make('p','q-reason',job.evidence.slice(0,600)));
      for(const q of job.questions)card.append(question(job,q));
      for(const text of job.page_issues)card.append(make('p','q-reason','这一项答案解决不了：'+text));
      if(job.can_retry)card.append(button('primary retry','重跑这一份',async()=>{try{await api('runner/retry',{job_id:job.job_id});await refresh();loadPreflight();}catch(e){note(e.message);}}));
      box.append(card);
    }
    syncButtons();
  }
  function renderAnswers(list){
    const box=$('answers');box.replaceChildren();$('answer-count').textContent=list.length;
    if(!list.length)box.append(make('div','empty','还没有记住的答案。'));
    for(const item of list){
      const row=make('div','answer-row'),text=make('div','answer-text'),actions=make('div','row-actions');
      const show=()=>{
        text.replaceChildren(make('p','q-label',item.question));
        if(item.description)text.append(make('p','hint',item.description));
        text.append(make('p','',item.answer));
        if(item.company)text.append(make('p','scope','答案里写了公司名，所以只用于 '+item.company));
        actions.replaceChildren(button('secondary','修改',edit),button('secondary','删除',()=>{
          if(confirm('删除后，这道题下次会停下来问你。确定吗？'))changeAnswers('answers/'+encodeURIComponent(item.id),undefined,'DELETE');
        }));
      };
      const edit=()=>{
        const input=make('textarea');input.rows=2;input.value=item.answer;input.setAttribute('aria-label',item.question);
        text.replaceChildren(make('p','q-label',item.question));
        if(item.description)text.append(make('p','hint',item.description));
        text.append(input);
        actions.replaceChildren(button('primary','保存',()=>{if(input.value.trim())changeAnswers('answers/'+encodeURIComponent(item.id),{answer:input.value},'PUT');}),button('secondary','取消',show));
      };
      show();row.append(text,actions);box.append(row);
    }
  }
  let preflightTimer=null;
  function renderPreflight(data){
    const count=data.counts,tally=data.companies,box=$('preflight-questions');
    $('preflight-count').textContent=data.questions.length+data.more_questions;
    const essay=data.allow_unread_essays?`带短答题的 ${count.essay} 份也会投`:`带短答题的 ${count.essay} 份等你打开下面的开关`;
    $('preflight-summary').textContent=data.error||`现在可以直接投 ${count.ready} 份（${tally.ready} 家）；${essay}；等你答题 ${count.ask} 份（${tally.ask} 家）；投不了 ${count.never} 份。`+
      (count.unread?` 还有 ${count.unread} 份表单没读到，读到之前不会投。`:'')+(data.reading?' 正在读表单…':'');
    $('allow-essays').checked=data.allow_unread_essays;
    $('preflight-note').textContent=data.model_warning||'';
    if(!box.querySelector('textarea:focus')){
      box.replaceChildren();
      if(!data.questions.length)box.append(make('div','empty','现在没有要你答的题。'));
      for(const q of data.questions)box.append(question({job_id:q.job_id,company:q.company},{label:q.question,description:q.description,options:q.options,value:'',on_page:q.on_page,url:q.url,
        reason:`有 ${q.forms} 份带这道题（${q.companies} 家）`}));
    }
    $('preflight-never').hidden=!data.never.length;$('preflight-never-title').textContent=`投不了的 ${count.never} 份，看原因`;
    const never=$('preflight-never-list');never.replaceChildren();
    for(const job of data.never){const row=make('div','ledger-row'),text=make('div');
      text.append(make('p','q-label',`${job.company} · ${job.title}`),make('p','q-reason',job.why.join('；')));row.append(text);never.append(row);}
    clearTimeout(preflightTimer);if(data.reading)preflightTimer=setTimeout(loadPreflight,5000);
    const finished=formsReading&&!data.reading;formsReading=!!data.reading;if(finished)reloadAnswers();
  }
  async function loadPreflight(){try{renderPreflight(await api('preflight'));}catch(e){$('preflight-summary').textContent=e.message;}}
  $('preflight-refresh').onclick=async()=>{try{renderPreflight(await api('preflight/refresh',{}));}catch(e){$('preflight-note').textContent=e.message;}};
  $('allow-essays').onchange=async event=>{try{renderPreflight(await api('preflight/essays',{allow_unread_essays:event.target.checked}));reloadAnswers();}catch(e){$('preflight-note').textContent=e.message;}};
  function requeueNote(places){
    const going=places.runnable.map(job=>`${job.company} 排第 ${job.place} 个`+(job.place>20?'（一轮最多 20 份，下一轮轮不到）':job.place>1?`（一轮至少投 ${job.place} 份才轮得到）`:''));
    const cannot=places.cannot?`另外 ${places.cannot} 份现在跑不了，原因写在「需要你处理」那张卡上。`:'';
    if(!going.length)return `没有能跑的可以放回。${cannot}`;
    const sooner=places.runnable.some(job=>job.place>1)?'想现在就跑，到「需要你处理」点「重跑这一份」。':'';
    return `放回了 ${going.length} 份能跑的：${going.join('，')}。${sooner}${cannot}下一轮开跑时会再检查一遍。`;
  }
  $('requeue').onclick=async()=>{try{await api('runner/requeue',{});$('preflight-note').textContent=requeueNote(await api('requeue-places'));}catch(e){$('preflight-note').textContent=e.message;}};
  function renderInterviews(data){
    const cal=data.calendar;$('interviews-count').textContent=data.blocked.length;
    const age=cal.age_days==null?'日历还没读过':cal.age_days<1?'日历今天读过':`日历是 ${Math.round(cal.age_days)} 天前读的`;
    const rule='最近 90 天有技术面、或者日历上还排着技术面的公司，不会自动投。只打过招聘电话的照常投。';
    if(cal.copy_note){const link=make('a','','到 /settings 重新授权');link.href='/settings';$('interviews-summary').replaceChildren(rule+cal.copy_note,link);}
    else $('interviews-summary').textContent=`${rule}${age}${cal.stale?'，太旧了，先点「刷新日历」。':'。'}`;
    const unknown=$('interviews-unknown');unknown.replaceChildren();
    for(const event of data.unknown){const box=make('div','question'),row=make('div','q-input'),input=make('input');
      input.setAttribute('aria-label','这场面试是哪家公司');input.placeholder='这是哪家公司？';input.style.flex='1 1 220px';
      box.append(make('p','q-label',`${event.date} · ${event.title}`),make('p','q-reason','日历上像技术面，但我认不出公司。'));
      row.append(input,button('primary','记住',()=>{if(input.value.trim())changeInterviews('interviews/name-event',{event_id:event.event_id,company:input.value});}));box.append(row);unknown.append(box);}
    const list=$('interviews-list');list.replaceChildren();
    for(const item of data.blocked){const row=make('div','ledger-row'),text=make('div');
      text.append(make('p','q-label',item.company),make('p','hint',`${item.date||''} · ${item.source} · ${item.title}`));
      row.append(text,button('secondary','放行',()=>changeInterviews('interviews/override',{company:item.company,action:'allow'})));list.append(row);}
    for(const item of data.allowed){const row=make('div','ledger-row');row.append(make('p','',`${item.company}：你放行了`),
      button('secondary','撤销',()=>changeInterviews('interviews/override',{company:item.company,action:'clear'})));list.append(row);}
    const recruiter=$('interviews-recruiter');recruiter.replaceChildren();
    for(const item of data.recruiter_only){const row=make('div','ledger-row');const text=make('div');
      text.append(make('p','q-label',item.company),make('p','hint',`${item.date} · ${item.source} · ${item.title}`));
      row.append(text,button('secondary','算技术面',()=>changeInterviews('interviews/override',{company:item.company,action:'block'})));recruiter.append(row);}
    $('interviews-note').textContent=data.refresh_error||'';
  }
  async function changeInterviews(path,body){try{renderInterviews(await api(path,body));loadPreflight();reloadAnswers();}catch(e){$('interviews-note').textContent=e.message;}}
  $('interviews-refresh').onclick=()=>changeInterviews('interviews/refresh',{});
  $('interviews-add-btn').onclick=()=>{const input=$('interviews-add');if(input.value.trim()){changeInterviews('interviews/override',{company:input.value,action:'block'});input.value='';}};
  function renderLogins(data){
    const box=$('logins-list');box.replaceChildren();$('logins-count').textContent=data.logins.length;
    for(const item of data.logins){const row=make('div','ledger-row'),text=make('div');
      text.append(make('p','q-label',item.host),make('p','hint',item.username));
      row.append(text,button('secondary','删除',async()=>{
        if(!confirm(`删除 ${item.host} 保存的账号？`))return;
        try{renderLogins(await api('logins/'+encodeURIComponent(item.host),undefined,'DELETE'));}catch(e){$('logins-note').textContent=e.message;}}));
      box.append(row);}
  }
  $('login-form').onsubmit=async event=>{
    event.preventDefault();
    const pass=$('login-pass'),site=$('login-site'),user=$('login-user');
    if(!site.value.trim()||!user.value.trim()||!pass.value)return;
    try{renderLogins(await api('logins',{site:site.value,username:user.value,password:pass.value}));$('logins-note').textContent='存好了。';site.value=user.value='';}
    catch(e){$('logins-note').textContent=e.message;}
    finally{pass.value='';}
  };
  async function refresh() {
    if(polling)return;polling=true;
    try {
      const state=await api('runner/status');
      // These rows can be last night's, and their reasons with them, so she has to see when this batch began.
      $('batch-started').textContent=state.started_at?`这一轮 ${new Date(state.started_at*1000).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})} 开始。`:'';
      $('state').textContent=pauses[state.pause_reason]||phases[state.phase]||state.phase;
      // A claim waits while the forms are read, so "waiting for a resume" would send her looking in the wrong place.
      if(state.phase==='idle'&&formsReading)$('state').textContent='正在读表单，读完就开始';
      $('status').textContent=state.active?'正在执行':'已同步';
      const worker=state.worker||{},stale=worker.version&&worker.expected_version&&worker.version!==worker.expected_version;
      const otherCard=worker.identity&&worker.identity!=='casey';
      $('open-runner').hidden=!!worker.online||!worker.runner_url;$('open-runner').dataset.url=worker.runner_url||'';
      $('worker').textContent=!worker.online?'Chrome 执行台：未在线。点下面的按钮打开，打开后不要关。':
        stale?`Chrome 执行台：在线，但扩展还是 ${worker.version}，磁盘上是 ${worker.expected_version}。请在 Chrome 扩展管理页重新加载。`:
        otherCard?`Chrome 执行台：在线，但扩展选的名片是 ${worker.identity}。请在扩展弹窗切回 Casey · 8787。`:
        `Chrome 执行台：在线 · 扩展 ${worker.version}`;
      ready=!(!worker.online||stale||otherCard||state.active||state.enabled);
      syncButtons();
      const job=state.active?.job || state.jobs?.at(-1)?.job;
      $('job').textContent=job?`${job.company} · ${job.title}`:'尚未领取岗位。';
      // Reload the answer cards only when a job changed, so polling never wipes what is being typed.
      const key=JSON.stringify((state.jobs||[]).map(row=>[row.job?.job_id,row.status,row.lease_id]));
      if(key!==jobsKey){jobsKey=key;reloadAnswers();}
      const events=state.events||[], next=JSON.stringify(events);if(next===signature)return;signature=next;
      const feed=$('feed'),nearEnd=feed.scrollHeight-feed.scrollTop-feed.clientHeight<70;
      feed.replaceChildren();$('count').textContent=events.length;
      if(!events.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='尚无执行消息。';feed.append(empty);}
      for(const event of events){const row=document.createElement('div');row.className='message '+event.role;
        const avatar=document.createElement('div');avatar.className='avatar';avatar.textContent=event.role==='filler'?'F':event.role==='reviewer'?'R':'G';
        const content=document.createElement('div'),line=document.createElement('div');line.className='byline';
        const name=document.createElement('b');name.textContent=(names[event.role]||'系统')+(names[event.to]?` → ${names[event.to]}`:'');
        const time=document.createElement('small');time.textContent=new Date(event.at*1000).toLocaleTimeString();
        const body=document.createElement('div');body.className='body';body.textContent=event.text;line.append(name,time);content.append(line,body);row.append(avatar,content);feed.append(row);}
      if(nearEnd)feed.scrollTop=feed.scrollHeight;
    }catch(error){$('status').textContent='连接中断';$('state').textContent=error.message;}finally{polling=false;}
  }
  $('pair').onclick=async()=>{try{$('pair-code').textContent=(await api('pair-code',{})).code;$('connection').textContent='在 Chrome 独立执行台的连接区域粘贴此码；10 分钟内有效。';}catch(e){$('connection').textContent=e.message;}};
  async function startBatch(count){
    if(!Number.isInteger(count)||count<1||count>20){$('connection').textContent='份数要是 1 到 20 之间的整数。';return;}
    ready=false;syncButtons();
    try{await api('runner/start',{apply_profile:'casey',max_jobs:count});$('connection').textContent=`已开始这一轮，最多 ${count} 份。Chrome 会自己打开申请页，请让它留在前台。`;await refresh();loadPreflight();}catch(e){$('connection').textContent=e.message;}
  }
  $('open-runner').onclick=()=>window.open($('open-runner').dataset.url,'_blank');
  $('start').onclick=()=>startBatch(1);
  $('start-5').onclick=()=>startBatch(5);
  $('start-custom').onclick=()=>startBatch(Number($('start-count').value));
  $('pause').onclick=async()=>{try{await api('runner/pause',{});await refresh();}catch(e){$('connection').textContent=e.message;}};
  refresh();setInterval(refresh,2500);loadPreflight();api('interviews').then(renderInterviews).catch(e=>{$('interviews-summary').textContent=e.message;});api('logins').then(renderLogins).catch(e=>{$('logins-note').textContent=e.message;});
})();
