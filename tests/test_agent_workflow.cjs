const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ctx = {}; vm.runInNewContext(fs.readFileSync('extension/agent_workflow.js','utf8'),ctx);

function fixture({wrong=false, outcomes=['submitted'], gate=true, stale=false, modelError=false, recordError=false}={}) {
  let value=wrong?'wrong':'Avery', calls=[], submits=0;
  const snapshot=()=>({fields:[{id:'name',label:'First name',value}]});
  const io={
    capture:snapshot, fill:async()=>({status:'completed'}),
    review:async()=>{calls.push('review');const result={decision:value==='Avery'?'pass':'needs_fix',model_status:modelError?'error':'completed',summary:'checked',issues:value==='Avery'?[]:[{field_id:'name',auto_fix:true,replacement:'Avery'}]};
      if(stale)value='changed while reviewing';return {id:'run',result};},
    repair:async()=>{calls.push('repair');value='Avery';return [{label:'First name',before:'wrong',after:value}];},
    confirm:async(s,r)=>{calls.push('confirm');return r;},
    preflight:()=>({ok:gate,reason:'button disabled'}),
    permit:async(s,r)=>{calls.push('permit');return r;},
    submit:async()=>{calls.push('submit');const outcome=outcomes[submits++]||'unknown';if(outcome==='validation_error')value='wrong';return {outcome,evidence:outcome};},
    report:async(o,r)=>{calls.push('report:'+o.outcome);return r;},
    record:async()=>{calls.push('record');if(recordError)throw Error('offline');},
  };
  return {io,calls,submits:()=>submits};
}
test('filler correction, independent re-review, submit rejection, correction, re-review, success',async()=>{
  const f=fixture({wrong:true,outcomes:['validation_error','submitted']});
  const r=await ctx.JobScanWorkflow.run(f.io,{autoSubmit:true});
  assert.equal(r.status,'submitted');assert.equal(f.submits(),2);
  assert.equal(f.calls.filter(c=>c==='review').length,4);
  assert.equal(f.calls.filter(c=>c==='repair').length,2);
  assert.equal(f.calls.at(-1),'record');
});
test('an employer refusal stops without retrying or recording applied, and a batch can move on',async()=>{
  const f=fixture({outcomes:['blocked']});const r=await ctx.JobScanWorkflow.run(f.io,{autoSubmit:true});
  assert.equal(r.status,'blocked');assert.equal(f.submits(),1);assert.ok(!f.calls.includes('record'));
});
test('unknown response never retries or records applied',async()=>{
  const f=fixture({outcomes:['unknown']});const r=await ctx.JobScanWorkflow.run(f.io,{autoSubmit:true});
  assert.equal(r.status,'unknown');assert.equal(f.submits(),1);assert.ok(!f.calls.includes('record'));
});
test('disabled submit does not issue permit or click',async()=>{
  const f=fixture({gate:false});const r=await ctx.JobScanWorkflow.run(f.io,{autoSubmit:true});
  assert.equal(r.status,'needs_user');assert.equal(f.submits(),0);assert.ok(!f.calls.includes('permit'));
});
test('a page that keeps changing is re-read, never clicked, and stops at the round limit',async()=>{
  const f=fixture({stale:true});const r=await ctx.JobScanWorkflow.run(f.io,{autoSubmit:true});
  assert.equal(r.status,'needs_user');assert.equal(f.submits(),0);assert.equal(f.calls.filter(c=>c==='review').length,5);
  assert.ok(!f.calls.includes('permit'));
});
test('the board is told what the form holds after filling',async()=>{
  const f=fixture();const events=[];f.io.event=e=>events.push(e);
  await ctx.JobScanWorkflow.run(f.io,{autoSubmit:false});
  const report=events.find(e=>e.role==='filler'&&e.to==='reviewer');
  assert.match(report.text,/First name：Avery/);
});
test('the fill report never cuts an emoji in half',async()=>{
  const f=fixture();const events=[];f.io.event=e=>events.push(e);
  const first={id:'name',label:'First name',value:'Avery'};
  f.io.capture=()=>({fields:[first,{id:'long',label:'l'.repeat(59)+'😀 label',value:'v'.repeat(79)+'😀 value'}]});
  await ctx.JobScanWorkflow.run(f.io,{autoSubmit:false});
  const report=events.find(e=>e.role==='filler'&&e.to==='reviewer');
  assert.ok(report.text.isWellFormed());
  assert.ok(report.text.endsWith('l'.repeat(59)+'😀：'+'v'.repeat(79)+'😀'));
});
test('a failed model cannot click even if verdict says pass',async()=>{
  const f=fixture({modelError:true});assert.equal((await ctx.JobScanWorkflow.run(f.io,{autoSubmit:true})).status,'needs_user');assert.equal(f.submits(),0);
});
test('review-only does not submit and a logging failure never resubmits',async()=>{
  const f=fixture();assert.equal((await ctx.JobScanWorkflow.run(f.io)).status,'review_passed');assert.equal(f.submits(),0);
  const g=fixture({recordError:true});assert.equal((await ctx.JobScanWorkflow.run(g.io,{autoSubmit:true})).status,'submitted');assert.equal(g.submits(),1);
});
test('repeat validation failure cannot become unlimited submissions',async()=>{
  const f=fixture({outcomes:['validation_error','validation_error','submitted']});
  assert.equal((await ctx.JobScanWorkflow.run(f.io,{autoSubmit:true})).status,'needs_user');assert.equal(f.submits(),2);
});

test('API repair planning starts only after review feedback and is re-reviewed before permit',async()=>{
  const f=fixture({wrong:true});
  f.io.review=async()=>{
    f.calls.push('review');
    const wrong=f.io.capture().fields[0].value!=='Avery';
    return {id:'run',result:{decision:wrong?'needs_fix':'pass',model_status:'completed',
      summary:wrong?'Name does not match the confirmed profile.':'Corrected form checked.',
      issues:wrong?[{field_id:'name',reason:'Wrong name',auto_fix:false}]:[]}};
  };
  f.io.planRepair=async(snapshot,run)=>{
    f.calls.push('planRepair');
    assert.equal(snapshot.fields[0].value,'wrong');
    assert.equal(run.result.issues[0].reason,'Wrong name');
    assert.deepEqual(f.calls,['review','planRepair']);
    return {status:'planned',summary:'Use confirmed profile.',
      repairs:[{field_id:'name',auto_fix:true,replacement:'Avery'}]};
  };
  assert.equal((await ctx.JobScanWorkflow.run(f.io,{autoSubmit:true})).status,'submitted');
  assert.deepEqual(f.calls,['review','planRepair','repair','review','confirm','permit','submit','report:submitted','record']);
});

test('a passing review, rule repair, and model failure never invoke speculative API repair',async()=>{
  for(const options of [{},{wrong:true},{wrong:true,modelError:true}]) {
    const f=fixture(options);
    f.io.planRepair=async()=>{throw Error('Unexpected repair planning without eligible feedback');};
    await ctx.JobScanWorkflow.run(f.io,{autoSubmit:true});
  }
});

test('a repair plan cannot authorize submission if the new review still rejects it',async()=>{
  const f=fixture({wrong:true});
  f.io.review=async()=>{
    f.calls.push('review');
    return {id:'run',result:{decision:'needs_user',model_status:'completed',summary:'Unresolved fact.',
      issues:[{field_id:'name',reason:'Still unsupported',auto_fix:false}]}};
  };
  f.io.planRepair=async()=>{f.calls.push('planRepair');return {status:'planned',summary:'Proposed correction',
    repairs:[{field_id:'name',replacement:'Avery',auto_fix:true}]};};
  const result=await ctx.JobScanWorkflow.run(f.io,{autoSubmit:true,maxRounds:2});
  assert.equal(result.status,'needs_user');
  assert.deepEqual(f.calls,['review','planRepair','repair','review']);
  assert.equal(f.submits(),0);
});

test('missing facts in an API repair response do not lead to changes or submission',async()=>{
  const f=fixture({wrong:true});
  f.io.review=async()=>{f.calls.push('review');return {id:'run',result:{decision:'needs_user',
    model_status:'completed',summary:'Needs a confirmed fact.',issues:[{field_id:'name',auto_fix:false}]}};};
  f.io.planRepair=async()=>{f.calls.push('planRepair');return {status:'needs_user',summary:'Cannot infer it.',repairs:[]};};
  assert.equal((await ctx.JobScanWorkflow.run(f.io,{autoSubmit:true})).status,'needs_user');
  assert.deepEqual(f.calls,['review','planRepair']);
  assert.equal(f.submits(),0);
});
