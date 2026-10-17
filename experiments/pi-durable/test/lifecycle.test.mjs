import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { once } from 'node:events';
import { performance } from 'node:perf_hooks';
import { setTimeout as delay } from 'node:timers/promises';
import { createModels } from '@earendil-works/pi-ai/models';
import { fauxProvider, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';
import { C, openRuntime, submit, Task, Memory, phoneTools, dateInShanghai, yesterday,
  completedOrders, activeOverrides, playbackDone, acceptAnswer } from '../runtime.mjs';

async function env(tools=[]) {
  const dir=await mkdtemp(path.join(os.tmpdir(),'dayu-pi-test-'));
  const faux=fauxProvider();const models=createModels();models.setProvider(faux.provider);
  const options={db:path.join(dir,'test.sqlite'),models,
    model:{provider:faux.provider.id,modelId:faux.getModel().id},tools};
  return {dir,faux,options,runtime:await openRuntime(options)};
}

test('Shanghai yesterday handles UTC boundary and leap day',()=>{
  const now=new Date('2026-10-16T16:00:00Z');
  assert.equal(dateInShanghai(now),'2026-10-17');assert.equal(yesterday(now),'2026-10-16');
  assert.equal(yesterday(new Date('2024-02-29T16:01:00Z')),'2024-02-29');
});

test('only verified completed orders on the requested local date are recalled',()=>{
  const record={type:'order',date:'2026-10-16',status:'completed',verified:true,evidence:'fixture:receipt-1'};
  assert.deepEqual(completedOrders([record,{...record,status:'selected'},
    {...record,date:'2026-10-17'},{...record,verified:false},{...record,evidence:''}],'2026-10-16'),[record]);
});

test('temporary overrides expire by day and do not cross task ownership',()=>{
  const items={drink:{value:'拿铁',scope:'today',date:'2026-10-17',taskId:'a'},
    sugar:{value:'半糖',scope:'task',date:'2026-10-17',taskId:'a'}};
  assert.deepEqual(activeOverrides(items,'a','2026-10-17'),{drink:'拿铁',sugar:'半糖'});
  assert.deepEqual(activeOverrides(items,'b','2026-10-17'),{drink:'拿铁'});
  assert.deepEqual(activeOverrides(items,'a','2026-10-18'),{});
});

test('SQLite memory and request-id dedup survive reopen; transcript can reset independently',async()=>{
  const e=await env();let r=e.runtime;
  try {
    e.faux.setResponses([fauxAssistantMessage('done')]);
    const first=await submit(r,'hello','one');
    await r.root.commit(async tx=>{(await tx.doc(Memory,r.root.id)).items.push({type:'preference',key:'coffee',value:'美式'});},C);
    await r.close();r=await openRuntime(e.options);
    const repeated=await submit(r,'hello','one');
    assert.equal(first.submissionId,repeated.submissionId);assert.equal(e.faux.state.callCount,1);
    await r.root.reset(undefined,C);
    assert.equal((await r.memories()).items[0].value,'美式');
  } finally {await r.close();}
});

test('question gates persist and reject early, duplicate, stale and mismatched replies',async()=>{
  const e=await env();let r=e.runtime;
  try {
    await r.root.commit(async tx=>Object.assign(await tx.doc(Task,r.root.id),
      {rid:'phone-task',state:'need_user',question:'热的还是冷的？',questionSeq:2,audioId:17}),C);
    assert.equal(await acceptAnswer(r,{rid:'phone-task',seq:2,text:'热的'}),false);
    assert.equal(await playbackDone(r,{rid:'phone-task',seq:1,audioId:17}),false);
    assert.equal(await playbackDone(r,{rid:'other',seq:2,audioId:17}),false);
    assert.equal(await playbackDone(r,{rid:'phone-task',seq:2,audioId:16}),false);
    await r.close();r=await openRuntime(e.options);
    assert.equal((await r.state()).question,'热的还是冷的？');
    assert.equal(await playbackDone(r,{rid:'phone-task',seq:2,audioId:17}),true);
    assert.equal(await acceptAnswer(r,{rid:'phone-task',seq:1,text:'热的'}),false);
    assert.equal(await acceptAnswer(r,{rid:'phone-task',seq:2,text:''}),false);
    assert.equal(await acceptAnswer(r,{rid:'phone-task',seq:2,text:'热的'}),true);
    assert.equal(await acceptAnswer(r,{rid:'phone-task',seq:2,text:'热的'}),false);
    assert.equal((await r.state()).rid,'phone-task');
  } finally {await r.close();}
});

test('abort during generation withdraws queued input and stops subsequent requests',async()=>{
  const e=await env();const r=e.runtime;
  let started;const ready=new Promise(resolve=>{started=resolve;});
  e.faux.setResponses([async(_context,options)=>{
    started();await delay(10000,undefined,{signal:options.signal});return fauxAssistantMessage('late');
  },fauxAssistantMessage('must not run')]);
  try {
    const first=await r.root.submit({type:'input',content:'run',requestId:'cancel-main'},C);
    await ready;
    const queued=await r.root.submit({type:'input',content:'queued',requestId:'cancel-queue'},C);
    await r.root.abort(C);
    assert.equal((await first.wait(C)).status,'unanswered');
    assert.equal((await queued.wait(C)).status,'unanswered');
    assert.equal(e.faux.state.callCount,1);
  } finally {await r.close();}
});

test('abort drains in-flight phone adapter before idle and skips the next tool',async()=>{
  let started,done=false,cancelled=false,calls=0;
  const ready=new Promise(resolve=>{started=resolve;});let completion;
  const host={async call(op) {
    if(op==='tool') {calls++;started();await (completion=delay(180));done=true;throw new Error('cancelled');}
    if(op==='cancel') {cancelled=true;await completion;return {drained:true};}
  }};
  const tools=phoneTools({tools:[{name:'read_screen',description:'read',parameters:{type:'object',properties:{}}}]},host);
  const e=await env(tools);const r=e.runtime;
  e.faux.setResponses([fauxAssistantMessage([fauxToolCall('read_screen',{}),fauxToolCall('read_screen',{})],{stopReason:'toolUse'})]);
  try {
    await r.root.commit(async tx=>Object.assign(await tx.doc(Task,r.root.id),{rid:'drain',state:'running'}),C);
    await r.root.submit({type:'input',content:'read twice'},C);await ready;
    const start=performance.now();await r.root.abort(C);
    assert.equal(done,true);assert.equal(cancelled,true);assert.equal(calls,1);
    assert.ok(performance.now()-start>100);
  } finally {await r.close();}
});

test('uncertain phone result terminates without retry or another model round',async()=>{
  let calls=0;
  const host={async call(){calls++;throw new Error('phone result uncertain');}};
  const tools=phoneTools({tools:[{name:'open_app',description:'open',parameters:{type:'object',properties:{app:{type:'string'}},required:['app']}}]},host);
  const e=await env(tools);const r=e.runtime;
  e.faux.setResponses([fauxAssistantMessage(fauxToolCall('open_app',{app:'淘宝'}),{stopReason:'toolUse'})]);
  try {
    await r.root.commit(async tx=>Object.assign(await tx.doc(Task,r.root.id),{rid:'uncertain',state:'running'}),C);
    await submit(r,'open','uncertain');
    assert.equal((await r.state()).state,'unknown');assert.equal((await r.state()).blocked,true);
    assert.equal(calls,1);assert.equal(e.faux.state.callCount,1);
  } finally {await r.close();}
});

test('pending phone intent blocks model dispatch even before a recovery hook runs',async()=>{
  const e=await env();const r=e.runtime;
  try {
    await r.root.commit(async tx=>{(await tx.doc(Task,r.root.id)).pendingPhoneAction={callId:'external-1',name:'open_app'};},C);
    e.faux.setResponses([fauxAssistantMessage('must not run')]);
    await assert.rejects(submit(r,'continue','blocked'),/unanswered/);
    assert.equal(e.faux.state.callCount,0);
  } finally {await r.close();}
});

test('definite app launch rejection ends failed without poisoning the execution channel',async()=>{
  const host={async call(){throw Object.assign(new Error('app launch rejected'),{uncertain:false,reason:'app_launch_rejected'});}};
  const tools=phoneTools({tools:[{name:'open_app',description:'open',parameters:{type:'object',properties:{app:{type:'string'}},required:['app']}}]},host);
  const e=await env(tools);const r=e.runtime;
  try {
    await r.root.commit(async tx=>Object.assign(await tx.doc(Task,r.root.id),{rid:'rejected',state:'running'}),C);
    e.faux.setResponses([fauxAssistantMessage(fauxToolCall('open_app',{app:'missing'}),{stopReason:'toolUse'})]);
    await submit(r,'open missing','rejected');
    const state=await r.state();assert.equal(state.result.kind,'failed');assert.equal(state.blocked,false);
    assert.equal(state.pendingPhoneAction,null);
  } finally {await r.close();}
});

test('success without a current screen receipt becomes unknown',async()=>{
  const tools=phoneTools({tools:[{name:'finish_task',description:'finish',parameters:{type:'object',properties:{
    kind:{type:'string'},text:{type:'string'},evidence_ids:{type:'array',items:{type:'string'}}},required:['kind','text','evidence_ids']}}]},{});
  const e=await env(tools);const r=e.runtime;
  try {
    await r.root.commit(async tx=>Object.assign(await tx.doc(Task,r.root.id),{rid:'no-proof',state:'running'}),C);
    e.faux.setResponses([fauxAssistantMessage(fauxToolCall('finish_task',{kind:'success',text:'done',evidence_ids:[]}),{stopReason:'toolUse'})]);
    await submit(r,'finish','no-proof');assert.equal((await r.state()).result.kind,'unknown');
  } finally {await r.close();}
});

test('natural-language phone completion must pass the same structured finish guard as Direct',async()=>{
  const tools=phoneTools({tools:[{name:'finish_task',description:'finish',parameters:{type:'object',properties:{
    kind:{type:'string'},text:{type:'string'},evidence_ids:{type:'array',items:{type:'string'}}},required:['kind','text','evidence_ids']}}]},{});
  const e=await env(tools);const r=e.runtime;
  try {
    await r.root.commit(async tx=>Object.assign(await tx.doc(Task,r.root.id),{rid:'natural',state:'running'}),C);
    e.faux.setResponses([fauxAssistantMessage('already done'),
      fauxAssistantMessage(fauxToolCall('finish_task',{kind:'success',text:'done',evidence_ids:[]}),{stopReason:'toolUse'})]);
    await submit(r,'finish','natural');
    assert.equal(e.faux.state.callCount,2);assert.equal((await r.state()).result.kind,'unknown');
  } finally {await r.close();}
});

for(const replay of ['unsafe','safe'])test(`SIGKILL recovery: ${replay} external effect is not duplicated`,async()=>{
  const dir=await mkdtemp(path.join(os.tmpdir(),'dayu-pi-crash-'));
  const args=[path.join(import.meta.dirname,'crash-child.mjs')];
  const db=path.join(dir,'session.sqlite'),journal=path.join(dir,'effects.jsonl');
  const child=spawn(process.execPath,[...args,'start',db,journal,replay],{stdio:['ignore','pipe','pipe']});
  try {
    const lines=createInterface({input:child.stdout});
    const timeout=AbortSignal.timeout(10000);
    const [line]=await once(lines,'line',{signal:timeout});
    assert.equal(JSON.parse(line).event,'effect_started');
    child.kill('SIGKILL');await once(child,'exit');
    const resumed=spawn(process.execPath,[...args,'resume',db,journal,replay],{stdio:['ignore','pipe','pipe']});
    let stdout='',stderr='';resumed.stdout.on('data',x=>stdout+=x);resumed.stderr.on('data',x=>stderr+=x);
    const timer=setTimeout(()=>resumed.kill('SIGKILL'),10000);
    const [code]=await once(resumed,'exit');clearTimeout(timer);assert.equal(code,0,stderr);
    const result=JSON.parse(stdout.trim());
    const records=(await readFile(journal,'utf8')).trim().split('\n').map(JSON.parse);
    assert.equal(records.length,1);
    if(replay==='unsafe') {
      assert.equal(result.state.blocked,true);assert.equal(result.state.state,'unknown');
      assert.equal(result.modelCalls,0);assert.equal(result.settled.status,'unanswered');
    } else {assert.equal(result.settled.status,'done');assert.equal(result.modelCalls,1);}
  } finally {if(child.exitCode===null)child.kill('SIGKILL');}
});
