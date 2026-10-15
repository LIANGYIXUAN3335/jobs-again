const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const src=fs.readFileSync(require('node:path').join(__dirname,'../extension/runner.js'),'utf8');
const html=fs.readFileSync(require('node:path').join(__dirname,'../extension/runner.html'),'utf8');
const settle=async()=>{for(let i=0;i<15;i++)await new Promise(setImmediate)};
function harness(opts={}) {
 const nodes=new Map();const el=()=>({textContent:'',value:'',hidden:false,disabled:false,children:[],handlers:{},className:'',append(...n){this.children.push(...n)},replaceChildren(){this.children=[]},addEventListener(k,f){this.handlers[k]=f}});
 for(const m of html.matchAll(/id="([^"]+)"/g))nodes.set(m[1],el());
 const storage=new Map(Object.entries(opts.session||{}));let runtimeListener,execs=0,creates=0,reloads=0,removes=0;const calls=[],polls=[];
 let clockOffset=0;const Clock=class extends Date {static now(){return Date.now()+clockOffset}};
 let state={enabled:false,phase:'idle',events:[],jobs:[],...(opts.state||{})};
 const job={job_id:'ab:acme:123',company:'Acme',title:'SWE',url:opts.url||'https://jobs.ashbyhq.com/acme/123/application',apply_profile:'casey',resume_ready:true,status:'new'};
 const ctx={URL,AbortController,Date:Clock,console,Promise,crypto:require('node:crypto').webcrypto,
 document:{getElementById:id=>nodes.get(id),createElement:el},window:{addEventListener(){}},
 sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
 setTimeout:(fn,ms)=>ms===3000?(polls.push(fn),{poll:true}):setTimeout(fn,ms),clearTimeout:t=>{if(!t?.poll)clearTimeout(t)},
 chrome:{storage:{local:{get:async()=>({jobscanPort:8787,jobscanIdentity:opts.identity||'',jobscanReviewTokens:opts.noToken?{}:{'8787':'review-secret'}}),set:async()=>{}}},
 runtime:{id:'extension-id',getManifest:()=>({version:opts.version}),reload:()=>{reloads++},onMessage:{addListener:f=>{runtimeListener=f}}},tabs:{create:async()=>{creates++;return{id:501}},remove:async()=>{removes++;if(opts.hangRemove)await new Promise(()=>{})},sendMessage:async(id,msg)=>{
 if(msg.type==='jobscanRunnerReady'){if(opts.readyNetwork){clockOffset+=30000;throw Error('Network failed')} if(opts.gate)return{ready:false,gate:opts.gate};return{ready:true,job_id:job.job_id}}execs++;
 assert.equal(msg.lease.lease_id,'lease-1');assert.equal(msg.lease.token,'lease-secret');assert.ok(msg.lease.worker_id);assert.ok(msg.deadline>Date.now());assert.ok(msg.deadline<=Date.now()+12*60*1000);
 runtimeListener({type:'jobscanRunnerEvent',event:{role:'filler',text:'真实测试事件'}},{id:'extension-id',tab:{id:501}},()=>{});
 runtimeListener({type:'jobscanRunnerEvent',event:{role:'filler',text:'不属于执行页'}},{id:'extension-id',tab:{id:999}},()=>{});
 if(opts.eventText)runtimeListener({type:'jobscanRunnerEvent',event:{role:'filler',text:opts.eventText}},{id:'extension-id',tab:{id:501}},()=>{});
 if(opts.lost)throw Error('The message port closed');return{status:'submitted',reason:'Employer Success'};
 }}},fetch:async(url,init)=>{
 assert.equal(url.origin,'http://127.0.0.1:8787');assert.equal(init.headers['X-JobScan-Review'],'review-secret');
 // Stringify escapes a lone surrogate and leaves whole emoji as they are. The service rejects the escape.
 if(/\\ud[89a-f][0-9a-f]{2}/i.test(init.body||''))return{ok:false,status:422,json:async()=>({detail:[]})};
 const body=init.body?JSON.parse(init.body):null;calls.push({path:url.pathname,body});let result={};
 if(opts.downOnce&&url.pathname.endsWith('/finish')){opts.downOnce=false;return{ok:false,status:500,json:async()=>({detail:'boom'})}}
 if(opts.notFound&&url.pathname.endsWith(opts.notFound))return{ok:false,status:404,json:async()=>({detail:'Runner lease not found.'})};
 if(opts.leaseExpired&&url.pathname.endsWith('/finish')){state={...state,enabled:false,jobs:[{lease_id:'lease-1',status:'unknown'}]};return{ok:false,status:409,json:async()=>({detail:'lease is over'})}}
 if(url.pathname.endsWith('/status'))result=state;
 else if(url.pathname.endsWith('/start')){state={...state,enabled:true};result={ok:true}}
 else if(url.pathname.endsWith('/claim'))result=opts.notFound&&creates?{status:'busy'}:{status:'claimed',job,lease_id:'lease-1',token:'lease-secret',expires_at:Date.now()/1000+900};
 else if(url.pathname.endsWith('/event')){state.events.push(body);result={ok:true}}
 else if(url.pathname.endsWith('/finish')){opts.batch=(opts.batch||1)-1;state={...state,enabled:opts.batch>0,phase:body.outcome};result={ok:true}}
 else if(url.pathname.endsWith('/pause')){state.enabled=false;state.phase='paused'}
 return{ok:true,status:200,json:async()=>result};
 }};
 vm.runInNewContext(src,ctx);return{nodes,calls,storage,state:()=>state,execs:()=>execs,creates:()=>creates,reloads:()=>reloads,removes:()=>removes,poll:async()=>{await polls.pop()?.();await settle()},start:()=>nodes.get('start').handlers.click()};
}
(async()=>{
 let h=harness();await settle();assert.equal(h.execs(),0);assert.ok(!h.calls.some(x=>x.path.endsWith('/claim')));await h.start();await settle();
 assert.equal(h.execs(),1);assert.equal(h.creates(),1);assert.equal(h.calls.find(x=>x.path.endsWith('/finish')).body.outcome,'submitted');
 assert.ok(h.calls.filter(x=>x.path.endsWith('/event')).some(x=>x.body.text==='真实测试事件'));
 assert.ok(!h.calls.some(x=>x.body?.text==='不属于执行页'));assert.ok(h.calls.findIndex(x=>x.path.endsWith('/event'))<h.calls.findIndex(x=>x.path.endsWith('/finish')));
 h=harness({lost:true});await settle();await h.start();assert.equal(h.execs(),1);assert.equal(h.calls.find(x=>x.path.endsWith('/finish')).body.outcome,'unknown');
 h=harness({url:'https://jobs.ashbyhq.com.attacker.test/123'});await settle();await h.start();assert.equal(h.creates(),0);assert.equal(h.execs(),0);assert.equal(h.calls.find(x=>x.path.endsWith('/finish')).body.outcome,'blocked');
 h=harness({session:{jobscanRunnerPending:JSON.stringify({active:{lease_id:'old',token:'oldtoken',job:{company:'Old'},executeSent:true,tabId:7},events:[]})}});await settle();assert.equal(h.execs(),0);assert.equal(h.creates(),0);assert.equal(h.calls.find(x=>x.path.endsWith('/finish')).body.outcome,'unknown');
 h=harness({noToken:true});await settle();assert.equal(h.nodes.get('start').disabled,true);assert.equal(h.calls.length,0);
 for(const [gate,outcome] of [[{kind:'expired',evidence:'This job is no longer available'},'expired'],[{kind:'expired'},'blocked'],[{kind:'login',evidence:'Sign in required'},'blocked'],[{kind:'captcha',evidence:'CAPTCHA shown'},'needs_user'],[{kind:'verification',evidence:'Email code required'},'needs_user']]) {
 h=harness({gate:{...gate,url:'https://jobs.ashbyhq.com/acme/123/application'}});await settle();await h.start();assert.equal(h.execs(),0);assert.equal(h.calls.find(x=>x.path.endsWith('/finish')).body.outcome,outcome);
 }

 for(const url of ['https://jobs.ashbyhq.com/acme/123/application?gh_jid=other',undefined,'https://jobs.ashbyhq.com/acme/other/application','https://jobs.ashbyhq.com/acme','https://jobs.lever.co/acme/123/application','https://jobs.ashbyhq.com.attacker.test/acme/123/application','http://jobs.ashbyhq.com/acme/123/application']) {
  h=harness({gate:{kind:'expired',evidence:'Job closed',url}});await settle();await h.start();assert.equal(h.execs(),0);assert.equal(h.calls.find(x=>x.path.endsWith('/finish')).body.outcome,'blocked');
 }
 for(const url of ['https://jobs.ashbyhq.com/acme/123','https://jobs.ashbyhq.com/acme/123/application/?source=tracking','https://jobs.ashbyhq.com/acme/123/apply']) {
  h=harness({gate:{kind:'expired',evidence:'Job closed',url}});await settle();await h.start();assert.equal(h.execs(),0);assert.equal(h.calls.find(x=>x.path.endsWith('/finish')).body.outcome,'expired');
 }
 h=harness({gate:{kind:'login',evidence:'Login required',url:'https://jobs.ashbyhq.com/login'}});await settle();await h.start();assert.equal(h.execs(),0);assert.match(h.calls.find(x=>x.path.endsWith('/finish')).body.evidence,/页面地址/);

 h=harness({url:'https://boards.greenhouse.io/acme/jobs/123?gh_jid=123&source=abc',gate:{kind:'expired',evidence:'This job is closed',url:'https://boards.greenhouse.io/acme/jobs/123/apply?utm_campaign=test&gh_jid=123'}});await settle();await h.start();assert.equal(h.execs(),0);assert.equal(h.calls.find(x=>x.path.endsWith('/finish')).body.outcome,'expired');
 h=harness({url:'https://boards.greenhouse.io/acme/jobs/123?gh_jid=123',gate:{kind:'expired',evidence:'This job is closed',url:'https://boards.greenhouse.io/acme/jobs/123?gh_jid=456'}});await settle();await h.start();assert.equal(h.execs(),0);assert.equal(h.calls.find(x=>x.path.endsWith('/finish')).body.outcome,'blocked');
 for(const identity of ['alex','casey2']){h=harness({identity});await settle();assert.equal(h.nodes.get('start').disabled,true);await h.start();assert.equal(h.creates(),0);assert.ok(!h.calls.some(x=>x.path.endsWith('/start')))}
 // A batch keeps going through the page's own polling until the service turns it off.
 h=harness({batch:3});await settle();await h.start();await settle();assert.equal(h.execs(),1);
 await h.poll();assert.equal(h.execs(),2);await h.poll();assert.equal(h.execs(),3);assert.equal(h.creates(),3);
 await h.poll();await h.poll();assert.equal(h.execs(),3);assert.equal(h.calls.filter(x=>x.path.endsWith('/finish')).length,3);
 // An idle page on an older version loads the new one once; a running or matching one never does.
 const newer={worker:{expected_version:'0.9.0'}};
 h=harness({version:'0.5.1',state:newer});await settle();assert.equal(h.reloads(),1);
 h=harness({version:'0.9.0',state:newer});await settle();assert.equal(h.reloads(),0);
 h=harness({version:'0.5.1',state:{...newer,enabled:true}});await settle();assert.equal(h.reloads(),0);
 h=harness({version:'0.5.1',state:{...newer,active:{job:{company:'Acme'}}}});await settle();assert.equal(h.reloads(),0);
 h=harness({state:{enabled:true,batch_id:'board',started_at:Date.now()/1000}});await settle();assert.equal(h.execs(),1);assert.equal(h.calls.filter(x=>x.path.endsWith('/claim')).length,1);
 h=harness({state:{enabled:true,batch_id:'old',started_at:Date.now()/1000-3600}});await settle();assert.equal(h.execs(),0);assert.ok(!h.calls.some(x=>x.path.endsWith('/claim')));
 h=harness({identity:'alex',state:{enabled:true,batch_id:'board',started_at:Date.now()/1000}});await settle();assert.equal(h.execs(),0);assert.ok(!h.calls.some(x=>x.path.endsWith('/claim')));
 h=harness({readyNetwork:true});await settle();await h.start();assert.equal(h.execs(),0);assert.equal(h.calls.find(x=>x.path.endsWith('/finish')).body.outcome,'blocked');
 // A half-cut emoji must not hold back the result: whole at the cut, mended when it arrives broken.
 h=harness({eventText:'a'.repeat(1599)+'😀 rest'});await settle();await h.start();
 assert.ok(h.calls.find(x=>x.body?.text?.startsWith('aaa')).body.text.endsWith('a😀'));assert.equal(h.calls.find(x=>x.path.endsWith('/finish')).body.outcome,'submitted');
 h=harness({eventText:'half \ud83d emoji'});await settle();await h.start();
 assert.ok(h.calls.some(x=>x.body?.text==='half \ufffd emoji'));assert.equal(h.calls.find(x=>x.path.endsWith('/finish')).body.outcome,'submitted');
 // The tab closes only when the service recorded the result; a notice saying to keep it means keep it.
 h=harness();await settle();await h.start();assert.equal(h.removes(),1);
 h=harness({leaseExpired:true});await settle();await h.start();assert.ok(h.calls.some(x=>x.path.endsWith('/finish')));
 assert.equal(h.removes(),0);assert.match(h.nodes.get('notice').textContent,/保留的申请页/);
 // A page holding Chrome's Leave-or-Stay dialog must not hold the batch.
 h=harness({batch:2,hangRemove:true});await settle();await h.start();await new Promise(r=>setTimeout(r,900));await settle();
 assert.equal(h.removes(),1);assert.equal(h.nodes.get('phase').textContent.includes('正在保存'),false);
 await h.poll();assert.equal(h.execs(),2,'the next job in the batch is claimed');
 // A lease this page no longer holds is terminal: no endless retry, the page is free again and the tab stays.
 for(const notFound of ['/finish','/event']){
  h=harness({notFound});await settle();await h.start();await settle();
  assert.equal(h.removes(),0,notFound);assert.match(h.nodes.get('notice').textContent,/不归这一页/,notFound);
  assert.equal(h.storage.has('jobscanRunnerPending'),false,notFound);
  const finishes=h.calls.filter(x=>x.path.endsWith('/finish')).length;await h.poll();
  assert.equal(h.calls.filter(x=>x.path.endsWith('/finish')).length,finishes,'no retry after it: '+notFound);
  assert.equal(h.nodes.get('pair').disabled,false,notFound);
 }
 // Any other failure still retries: the result must not be dropped because the service was briefly down.
 h=harness({downOnce:true});await settle();await h.start();await settle();
 assert.equal(h.storage.has('jobscanRunnerPending'),true);assert.equal(h.removes(),0);
 await h.poll();assert.equal(h.calls.filter(x=>x.path.endsWith('/finish')).length,2);assert.equal(h.storage.has('jobscanRunnerPending'),false);assert.equal(h.removes(),1);
 console.log('PASS: fresh board batch arms once, stale or conflicting batch never claims; same-role URL gate attribution and absolute execute deadline/lease, expiry and auth gates skip Execute; conflicting card blocks start; Ready network failure is not expiry; no auto-start, bounded single execute, owned-tab events, events before finish, unknown no retry, hostname spoof rejected, interrupted recovery no execute, missing pairing fail closed');
})().catch(e=>{console.error(e);process.exitCode=1});
