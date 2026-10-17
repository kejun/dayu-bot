import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm} from 'node:fs/promises';
import os from 'node:os';
import {createModels} from '@earendil-works/pi-ai/models';
import {fauxProvider,fauxAssistantMessage as message,fauxToolCall as call} from '@earendil-works/pi-ai/providers/faux';
import {Runtime,State,Memory,Index,C,localDate,selectMemory} from '../runtime.mjs';

const schema=(name,properties={},required=[])=>({name,description:name,parameters:{type:'object',properties,required,additionalProperties:false}});
const tools=[schema('read_screen',{mode:{type:'string',enum:['tree','ocr']}}),schema('find_element',{text:{type:'string'}},['text']),schema('open_app',{app:{type:'string'}},['app']),
  schema('diagnose_phone'),
  schema('tap',{x:{type:'number'},y:{type:'number'}},['x','y']),
  schema('ask_user',{question:{type:'string'}},['question']),
  schema('finish_task',{kind:{type:'string',enum:['success','failed','unknown']},text:{type:'string'},
    evidence_ids:{type:'array',items:{type:'string'}}},['kind','text','evidence_ids'])];
const meta=(rid='one',text='读取当前页面',owner='ds-bot')=>({request_id:rid,text,owner,history:[],state:'processing',
  source:'voice',started:true,cancel_requested:false,question:null,question_seq:0,result_kind:null,result_text:null});
const done=(kind='success',ids=['e1'])=>message(call('finish_task',{kind,text:'已读取页面。',evidence_ids:ids}),{stopReason:'toolUse'});
const read=()=>message(call('read_screen',{}),{stopReason:'toolUse'});
async function setup(t,hostOverride,extra={}) {
  const directory=await mkdtemp(os.tmpdir()+'/dayu-pi-runtime-');
  const models=createModels(),faux=fauxProvider(extra.fauxOptions);models.setProvider(faux.provider);
  const calls=[];
  const host=async(op,p)=>{
    calls.push({op,...p});
    if(hostOverride)return hostOverride(op,p);
    if(op==='normalize')return {plan:{goal:'read',app:'',entry:'',additional_apps:[],steps:['read'],constraints:[],
      assumptions:[],questions:[],clarification:'',read_only:true}};
    if(op==='phone')return {ok:true,result:{screen:{source:'test'}},has_screen:true,mutation_generation:0};
    return {ready:true,drained:true};
  };
  const config={dataDir:directory,system:'Use the supplied tools.',tools,maxSteps:3000,
    requestTimeoutMs:1000,taskTimeoutMs:10000,...extra};
  const instances=[];
  const open=async()=>{const r=new Runtime(config,host,models,{provider:faux.provider.id,modelId:faux.getModel().id});
    await r.open();instances.push(r);return r;};
  const close=async r=>{await r.close();instances.splice(instances.indexOf(r),1);};
  t.after(async()=>{for(const r of instances)await r.close();await rm(directory,{recursive:true,force:true});});
  return {runtime:await open(),open,close,faux,calls,config,models};
}

test('normal phone turn uses two model requests and current screen evidence',async t=>{
  const e=await setup(t);e.faux.setResponses([read(),done()]);
  const result=await e.runtime.ask(meta(),'read');
  assert.match(result.text,/DONE: success/);assert.equal(e.faux.state.callCount,2);
  assert.equal(e.calls.filter(c=>c.op==='phone').length,1);
});
test('request dedup and terminal result survive runtime reopen',async t=>{
  const e=await setup(t);e.faux.setResponses([read(),done()]);
  const first=await e.runtime.ask(meta(),'read');await e.close(e.runtime);
  const reopened=await e.open();const second=await reopened.ask(meta(),'read');
  assert.equal(first.text,second.text);assert.equal(e.faux.state.callCount,2);
});
test('durable question restores without starting another model request',async t=>{
  const e=await setup(t);e.faux.setResponses([message(call('ask_user',{question:'热的还是冷的？'}),{stopReason:'toolUse'})]);
  assert.match((await e.runtime.ask(meta(),'read')).text,/ASK:/);
  await e.close(e.runtime);const r=await e.open();
  const record=await r.record(r.handles.get('one'));
  assert.equal(record.state,'need_user');assert.equal(record.question_seq,1);assert.equal(e.faux.state.callCount,1);
  e.faux.setResponses([read(),done()]);
  assert.match((await r.ask({...meta(),history:['热的']},'热的')).text,/DONE: success/);
});
test('queued admission survives reopen without executing it',async t=>{
  const e=await setup(t);await e.runtime.checkpoint({...meta(),started:false});
  await e.close(e.runtime);const r=await e.open();
  assert.equal((await r.record(r.handles.get('one'))).state,'processing');assert.equal(e.faux.state.callCount,0);
});
test('recovery halts an unfinished phone intent and does not replay it',async t=>{
  const e=await setup(t);await e.runtime.checkpoint(meta());const conv=e.runtime.handles.get('one');
  await conv.commit(async tx=>{const s=await tx.doc(State,conv.id);s.phase='running';s.pending={name:'tap',callId:'a'};},C);
  await e.close(e.runtime);const r=await e.open();
  const record=await r.record(r.handles.get('one'));
  assert.equal(record.result_kind,'unknown');assert.ok((await r.harness.snapshot(Index,C)).channel.blocked);
  assert.equal(e.calls.length,0);assert.equal(e.faux.state.callCount,0);
});
test('foreign task owner cannot reuse a request ID',async t=>{
  const e=await setup(t);await e.runtime.checkpoint(meta());
  await assert.rejects(e.runtime.ask(meta('one','读取当前页面','other'),'read'),/绑定/);
});
test('unproven success is unknown',async t=>{
  const e=await setup(t);e.faux.setResponses([done('success',[])]);
  assert.match((await e.runtime.ask(meta(),'read')).text,/DONE: unknown/);
});
test('natural text cannot bypass structured phone completion',async t=>{
  const e=await setup(t);e.faux.setResponses([message('done'),done('failed',[]),read(),done()]);
  assert.match((await e.runtime.ask(meta(),'read')).text,/DONE: success/);
  assert.equal(e.faux.state.callCount,4);
  assert.equal(e.calls.filter(c=>c.op==='replan').length,1);
  assert.equal(e.calls.filter(c=>c.op==='phone').length,1);
});
test('step limit blocks network dispatch before the next request',async t=>{
  const e=await setup(t,undefined,{maxSteps:2});e.faux.setResponses([read(),read(),done()]);
  assert.match((await e.runtime.ask(meta(),'read')).text,/步数上限/);assert.equal(e.faux.state.callCount,2);
});
test('explicit memory uses no normalizer or phone and persists across conversations',async t=>{
  const e=await setup(t);e.faux.setResponses([message(call('remember',{items:[
    {key:'coffee.drink',value:'美式',type:'preference'},{key:'coffee.temperature',value:'热',type:'preference'}]}),{stopReason:'toolUse'})]);
  assert.match((await e.runtime.ask(meta('memory','记住我喜欢热美式'),'记住我喜欢热美式')).text,/已经记住/);
  assert.equal(e.calls.length,0);assert.equal(e.faux.state.callCount,1);
  await e.close(e.runtime);const r=await e.open();
  assert.equal((await r.harness.snapshot(Memory,C)).items.length,2);
});
test('memory selection respects owner, completion evidence and current dates',()=>{
  const today=localDate(),items=[{type:'preference',owner:'a',value:'美式'},
    {type:'temporary',owner:'a',value:'拿铁',date:today},{type:'temporary',owner:'a',value:'摩卡',date:'2020-01-01'},
    {type:'preference',owner:'b',value:'咖啡'},
    {type:'order',owner:'a',date:today,status:'completed',verified:true,evidence:'test'},
    {type:'order',owner:'a',date:today,status:'selected',verified:true,evidence:'test'}];
  const result=selectMemory(items,'a','今天咖啡');
  assert.equal(result.preferences.length,1);assert.equal(result.temporary.length,1);assert.equal(result.orders.length,1);
});
test('cancel during planning drains host before acknowledging completion',async t=>{
  let begin,release;const ready=new Promise(r=>{begin=r;});let drained=false;
  const e=await setup(t,async op=>{
    if(op==='normalize'){begin();return new Promise((_,reject)=>{release=()=>reject(new Error('cancelled'));});}
    if(op==='cancel_phone'){await new Promise(r=>setTimeout(r,30));drained=true;release();return {drained:true};}
    throw new Error('unexpected phone action');
  });
  const job=e.runtime.ask(meta(),'read');await ready;
  await e.runtime.cancel('one');assert.ok(drained);
  assert.equal((await job).cancelled,true);
});
test('failed cancellation remains unknown and blocks the shared phone channel',async t=>{
  const e=await setup(t,async()=>{throw Object.assign(new Error('cleanup failed'),{fatal:true});});
  await e.runtime.checkpoint(meta());
  await assert.rejects(e.runtime.cancel('one'),/cleanup failed/);
  const record=await e.runtime.record(e.runtime.handles.get('one'));
  assert.equal(record.result_kind,'unknown');assert.ok((await e.runtime.harness.snapshot(Index,C)).channel.blocked);
});
test('runtime storage cannot have two simultaneous owners',async t=>{
  const e=await setup(t);await assert.rejects(e.open(),/占用/);
});
test('invalid phone tool parameters clear the pending intent and replan before giving up',async t=>{
  let attempts=0;
  const e=await setup(t,async op=>{
    if(op==='normalize'||op==='replan')return {plan:{}};
    if(op==='phone'&&++attempts===1)throw Object.assign(new Error('参数无效'),{validation:true});
    if(op==='phone')return {ok:true,has_screen:true,mutation_generation:0};
    return {};
  });
  e.faux.setResponses([read(),done('failed',[]),read(),done()]);
  assert.match((await e.runtime.ask(meta(),'read')).text,/DONE: success; 已读取页面/);
  const s=await e.runtime.harness.snapshot(State,e.runtime.handles.get('one').id,C);
  assert.equal(s.pending,null);assert.equal(e.faux.state.callCount,4);
});
test('a successful mutation followed by failed reading remains unknown',async t=>{
  const e=await setup(t,async op=>{
    if(op==='normalize')return {plan:{}};
    if(op==='phone')throw Object.assign(new Error('read failed after launch'),{uncertain:false,mutation_generation:1});
    return {};
  });
  e.faux.setResponses([message(call('open_app',{app:'测试'}),{stopReason:'toolUse'})]);
  assert.match((await e.runtime.ask(meta(),'read')).text,/DONE: unknown/);
});
test('failed app lookup asks for connection help and leaves the channel usable across restart',async t=>{
  const e=await setup(t,async(op,p)=>{
    if(op==='normalize')return {plan:{}};
    if(op==='phone'&&p.name==='open_app')throw Object.assign(new Error('curl timeout'),{
      uncertain:false,reason:'app_lookup_failed',mutation_generation:0,
      user_message:'查询应用失败，请检查手机连接后重试。'});
    return {ok:true,has_screen:true,mutation_generation:0};
  });
  e.faux.setResponses([message(call('open_app',{app:'微信'}),{stopReason:'toolUse'}),
    message(call('ask_user',{question:'查询应用失败，请检查手机连接。'}),{stopReason:'toolUse'})]);
  const failed=await e.runtime.ask(meta(),'open');
  assert.match(failed.text,/ASK: 查询应用失败/);
  assert.equal((await e.runtime.harness.snapshot(Index,C)).channel.blocked,null);
  await e.close(e.runtime);const r=await e.open();
  assert.equal((await r.harness.snapshot(Index,C)).channel.blocked,null);
  assert.equal((await r.record(r.handles.get('one'))).question,'查询应用失败，请检查手机连接。');
  await r.cancel('one');
  e.faux.setResponses([read(),done()]);
  assert.match((await r.ask(meta('next'),'read')).text,/DONE: success/);
  assert.equal(e.calls.filter(c=>c.op==='phone'&&c.name==='open_app').length,1);
});
test('phone evidence docs retain bounded metadata rather than page payloads',async t=>{
  let screens=0;
  const e=await setup(t,async op=>op==='phone'?{ok:true,has_screen:true,mutation_generation:0,
    result:{source:'screen '+(++screens)}}:{plan:{}});
  e.faux.setResponses([...Array.from({length:70},read),done('success',['e70'])]);
  assert.match((await e.runtime.ask(meta(),'read')).text,/DONE: success/);
  const s=await e.runtime.harness.snapshot(State,e.runtime.handles.get('one').id,C);
  assert.equal(Object.keys(s.receipts).length,64);assert.equal(s.receiptSeq,70);
  assert.equal(s.receipts.e70.result,undefined);
});
test('only a current owned snapshot alias resolves to a success receipt',async t=>{
  const e=await setup(t,async op=>op==='phone'?{
    ok:true,result:{snapshot_id:'owned-snapshot'},has_screen:true,mutation_generation:0}:{plan:{}});
  e.faux.setResponses([read(),done('success',['owned-snapshot'])]);
  const result=await e.runtime.ask(meta(),'read');
  assert.match(result.text,/DONE: success/);assert.deepEqual(result.evidence.evidence_ids,['e1']);
  e.faux.setResponses([done('success',['owned-snapshot'])]);
  assert.match((await e.runtime.ask(meta('other'),'read')).text,/DONE: unknown/);
});
test('a plain memory acknowledgement cannot claim a write that never committed',async t=>{
  const e=await setup(t);e.faux.setResponses([message('已经记住了'),message('已经记住了')]);
  assert.match((await e.runtime.ask(meta('m','记住我喜欢美式'),'记住我喜欢美式')).text,/DONE: failed; 记忆未保存/);
  assert.equal((await e.runtime.harness.snapshot(Memory,C)).items.length,0);
  assert.equal(e.faux.state.callCount,2);
});
test('an explicit lookup miss can recover without ending or blocking the task',async t=>{
  const e=await setup(t,async(op,p)=>{
    if(op==='normalize')return {plan:{}};
    if(op==='phone'&&p.name==='find_element')throw Object.assign(new Error('No element'),{
      recoverable:true,uncertain:false,reason:'element_not_found'});
    return {ok:true,has_screen:true,mutation_generation:0};
  });
  e.faux.setResponses([message(call('find_element',{text:'搜索'}),{stopReason:'toolUse'}),read(),done('success',['e1'])]);
  assert.match((await e.runtime.ask(meta(),'read')).text,/DONE: success/);
  assert.equal((await e.runtime.harness.snapshot(Index,C)).channel.blocked,null);
});
test('repeated lookup misses replan before asking for user help',async t=>{
  const e=await setup(t,async op=>{
    if(op==='normalize'||op==='replan')return {plan:{}};
    if(op==='phone')throw Object.assign(new Error('No element'),{recoverable:true,uncertain:false,reason:'element_not_found'});
    return {};
  });
  e.faux.setResponses(Array.from({length:12},()=>message(call('find_element',{text:'搜索'}),{stopReason:'toolUse'})));
  assert.match((await e.runtime.ask(meta(),'read')).text,/ASK:/);
  assert.equal(e.calls.filter(c=>c.op==='replan').length,3);
});
test('completed AX lookup failure falls back to OCR without locking or replaying actions',async t=>{
  const e=await setup(t,async(op,p)=>{
    if(op==='normalize')return {plan:{}};
    if(op==='phone'&&p.name==='find_element')throw Object.assign(new Error('AX unavailable'),{
      recoverable:true,uncertain:false,reason:'accessibility_unavailable',mutation_generation:1});
    return {ok:true,has_screen:true,mutation_generation:p.name==='tap'?2:1};
  });
  e.faux.setResponses([
    message(call('open_app',{app:'微信'}),{stopReason:'toolUse'}),
    message(call('find_element',{text:'公众号'}),{stopReason:'toolUse'}),
    message(call('read_screen',{mode:'ocr'}),{stopReason:'toolUse'}),
    message(call('tap',{x:60,y:40}),{stopReason:'toolUse'}),done('success',['e3'])]);
  assert.match((await e.runtime.ask(meta(),'read')).text,/DONE: success/);
  const s=await e.runtime.harness.snapshot(State,e.runtime.handles.get('one').id,C);
  assert.equal(s.pending,null);assert.equal(s.mutationCount,2);assert.equal(s.lookupMisses,0);
  assert.equal((await e.runtime.harness.snapshot(Index,C)).channel.blocked,null);
  assert.deepEqual(e.calls.filter(c=>c.op==='phone').map(c=>c.name),['open_app','find_element','read_screen','tap']);
  await e.close(e.runtime);const r=await e.open();
  assert.equal((await r.harness.snapshot(Index,C)).channel.blocked,null);
});
test('repeated completed AX rejections replan before asking for help without blocking the shared channel',async t=>{
  const e=await setup(t,async op=>{
    if(op==='normalize'||op==='replan')return {plan:{}};
    if(op==='phone')throw Object.assign(new Error('AX unavailable'),{
      recoverable:true,uncertain:false,reason:'accessibility_unavailable'});
    return {};
  });
  e.faux.setResponses(Array.from({length:12},()=>message(call('find_element',{text:'公众号'}),{stopReason:'toolUse'})));
  assert.match((await e.runtime.ask(meta(),'read')).text,/ASK: 手机控件树持续无响应/);
  assert.equal((await e.runtime.harness.snapshot(Index,C)).channel.blocked,null);
});
test('uncertain lookup errors never use the recovery branch or speak raw RPC JSON',async t=>{
  const e=await setup(t,async op=>{
    if(op==='normalize')return {plan:{}};
    if(op==='phone')throw Object.assign(new Error('{"code":-32000,"message":"timeout"}'),{recoverable:true,uncertain:true});
    return {};
  });
  e.faux.setResponses([message(call('find_element',{text:'搜索'}),{stopReason:'toolUse'}),read()]);
  const result=await e.runtime.ask(meta(),'read');
  assert.doesNotMatch(result.text,/code|32000|timeout/);
  assert.equal(e.calls.filter(c=>c.op==='phone').length,1); // analysis cannot dispatch the attempted read
  assert.ok((await e.runtime.harness.snapshot(Index,C)).channel.blocked);
});

test('Pi replans from errors and completed actions then continues the same task',async t=>{
  const e=await setup(t,async(op,p)=>{
    if(op==='normalize'||op==='replan')return {plan:{goal:'same goal',steps:['use OCR']}};
    if(op==='phone'&&p.name==='find_element')throw Object.assign(new Error('No match'),{
      recoverable:true,uncertain:false,reason:'element_not_found',mutation_generation:1});
    return {ok:true,has_screen:true,mutation_generation:p.name==='tap'?2:1,
      result:{source:p.name==='read_screen'?'new OCR screen':'initial screen'}};
  });
  e.faux.setResponses([message(call('open_app',{app:'微信'}),{stopReason:'toolUse'}),
    ...Array.from({length:3},()=>message(call('find_element',{text:'公众号'}),{stopReason:'toolUse'})),
    message(call('read_screen',{mode:'ocr'}),{stopReason:'toolUse'}),
    message(call('tap',{x:60,y:40}),{stopReason:'toolUse'}),done('success',['e3'])]);
  assert.match((await e.runtime.ask(meta(),'read')).text,/DONE: success/);
  const replans=e.calls.filter(c=>c.op==='replan');assert.equal(replans.length,1);
  assert.equal(replans[0].original,meta().text);assert.equal(replans[0].recovery.errors.length,3);
  assert.equal(replans[0].recovery.completed_actions[0].tool,'open_app');
  assert.equal(e.calls.filter(c=>c.op==='phone'&&c.name==='open_app').length,1);
});
test('cancel during recovery planning drains the host and never dispatches the new strategy',async t=>{
  let begin,release;const ready=new Promise(r=>{begin=r;});let drained=false;
  const e=await setup(t,async(op,p)=>{
    if(op==='normalize')return {plan:{}};
    if(op==='replan'){begin();return new Promise((_,reject)=>{release=()=>reject(new Error('cancelled'));});}
    if(op==='cancel_phone'){drained=true;release();return {drained:true};}
    if(op==='phone')throw Object.assign(new Error('No match'),{recoverable:true,uncertain:false,reason:'element_not_found'});
    return {ready:true};
  });
  e.faux.setResponses([...Array.from({length:3},()=>message(call('find_element',{text:'搜索'}),{stopReason:'toolUse'})),read()]);
  const job=e.runtime.ask(meta(),'read');await ready;
  await e.runtime.cancel('one');assert.ok(drained);
  assert.equal((await job).cancelled,true);
  assert.equal(e.calls.filter(c=>c.op==='phone').length,3);
});
test('unchanged screens trigger a new strategy and do not repeat successful actions',async t=>{
  const e=await setup(t,async(op,p)=>{
    if(op==='normalize'||op==='replan')return {plan:{steps:['use OCR']}};
    return {ok:true,has_screen:true,mutation_generation:0,result:{source:p.args?.mode==='ocr'?'changed':'unchanged'}};
  });
  e.faux.setResponses([...Array.from({length:5},read),message(call('read_screen',{mode:'ocr'}),{stopReason:'toolUse'}),done('success',['e6'])]);
  assert.match((await e.runtime.ask(meta(),'read')).text,/DONE: success/);
  assert.equal(e.calls.filter(c=>c.op==='replan').length,1);
});
test('uncertain actions are analyzed with diagnostics while every further action stays blocked',async t=>{
  const e=await setup(t,async(op,p)=>{
    if(op==='normalize')return {plan:{}};
    if(op==='phone'&&p.name==='tap')throw Object.assign(new Error('remote action timeout'),{
      fatal:true,uncertain:true,diagnosable:true});
    return {ok:true,has_screen:false,mutation_generation:0,result:{logs:['action still unconfirmed']}};
  });
  e.faux.setResponses([message(call('tap',{x:60,y:40}),{stopReason:'toolUse'}),
    message(call('tap',{x:60,y:40}),{stopReason:'toolUse'}),
    message(call('diagnose_phone',{}),{stopReason:'toolUse'}),done('success',[])]);
  const result=await e.runtime.ask(meta(),'read');
  assert.match(result.text,/ASK: 手机上次动作仍在核实/);
  assert.equal((await e.runtime.record(e.runtime.handles.get('one'))).state,'need_user');
  assert.deepEqual(e.calls.filter(c=>c.op==='phone').map(c=>c.name),['tap','diagnose_phone']);
  assert.ok((await e.runtime.harness.snapshot(Index,C)).channel.blocked);
});
test('long execution compacts with a larger summary budget and continues to completion',async t=>{
  let count=0,normal=0;const summaries=[],generations=[];
  const e=await setup(t,async op=>op==='phone'?{ok:true,has_screen:true,mutation_generation:0,
    result:{source:String(++count)+'x'.repeat(5000)}}:{plan:{}},
    {fauxOptions:{models:[{id:'small',contextWindow:10000,maxTokens:3276}]}});
  const response=(context,options)=>{
    if(context.messages.some(m=>m.role==='system'&&JSON.stringify(m.content).includes('context summarization assistant'))) {
      summaries.push(options.maxTokens);return message('Goal: finish reading. Continue remaining screens; no action replay.');
    }
    generations.push(options.maxTokens);return ++normal<=10?read():done('success',['e10']);
  };
  e.faux.setResponses(Array.from({length:40},()=>response));
  assert.match((await e.runtime.ask(meta(),'read')).text,/DONE: success/);
  assert.ok(summaries.length>0);assert.ok(summaries.every(n=>n===3276));
  assert.ok(generations.every(n=>n===384));assert.equal(count,10);
});

test('unconfirmed action waits durably then resumes the same goal without replay',async t=>{
  let originalId,ready=false;
  const e=await setup(t,async(op,p)=>{
    if(op==='normalize')return {plan:{goal:'finish original goal'}};
    if(op==='phone'&&p.name==='tap') {
      originalId=p.operation_id;
      throw Object.assign(new Error('timeout'),{uncertain:true,diagnosable:true,operation:{operation_id:originalId,method:'tap'}});
    }
    if(op==='reconcile_phone') {
      assert.equal(p.pending.operation_id,originalId);
      if(!ready)return {checked:false};
      return {checked:true,ok:true,has_screen:true,mutation_generation:1,result:{screen:{source:'after tap',bundle_id:'com.test'}}};
    }
    if(op==='phone')return {ok:true,has_screen:true,mutation_generation:1,result:{source:'remaining goal finished'}};
    return {ready:true,drained:true};
  });
  e.faux.setResponses([message(call('tap',{x:60,y:40}),{stopReason:'toolUse'}),done('unknown',[])]);
  assert.match((await e.runtime.ask(meta(),'read')).text,/ASK:/);
  assert.equal((await e.runtime.record(e.runtime.handles.get('one'))).state,'need_user');
  await e.close(e.runtime);const r=await e.open();
  ready=true;
  e.faux.setResponses([read(),done('success',['e2'])]);
  assert.match((await r.ask({...meta(),history:['继续']},'继续原任务')).text,/DONE: success/);
  assert.equal(e.calls.filter(c=>c.op==='phone'&&c.name==='tap').length,1);
  assert.equal(e.calls.filter(c=>c.op==='normalize').length,1);
  assert.equal((await r.harness.snapshot(Index,C)).channel.blocked,null);
});
test('Pi rechecks a late action before giving up and continues automatically when it finished',async t=>{
  const e=await setup(t,async(op,p)=>{
    if(op==='normalize')return {plan:{goal:'read after original action'}};
    if(op==='phone'&&p.name==='tap')throw Object.assign(new Error('timeout'),{uncertain:true,diagnosable:true});
    if(op==='reconcile_phone')return {checked:true,ok:true,has_screen:true,mutation_generation:1,result:{screen:{source:'completed page'}}};
    return {ready:true};
  });
  e.faux.setResponses([message(call('tap',{x:60,y:40}),{stopReason:'toolUse'}),done('unknown',[]),done('success',['e1'])]);
  assert.match((await e.runtime.ask(meta(),'read')).text,/DONE: success/);
  assert.equal(e.calls.filter(c=>c.op==='phone'&&c.name==='tap').length,1);
  assert.equal(e.calls.filter(c=>c.op==='reconcile_phone').length,1);
  assert.equal((await e.runtime.harness.snapshot(Index,C)).channel.blocked,null);
});
test('restart queries an unfinished durable action before any new phone dispatch',async t=>{
  const e=await setup(t,async(op,p)=>{
    if(op==='reconcile_phone')return {checked:true,ok:true,has_screen:true,mutation_generation:1,result:{screen:{source:'after original tap'}}};
    if(op==='phone')return {ok:true,has_screen:true,mutation_generation:1,result:{source:'done'}};
    return {ready:true};
  });
  await e.runtime.checkpoint(meta());const conv=e.runtime.handles.get('one');
  await conv.commit(async tx=>{const s=await tx.doc(State,conv.id);s.phase='running';s.plan={goal:'original'};
    s.pending={name:'tap',args:{x:60,y:40},operation_id:'durable-operation-123',evidence_id:'e1',mutation_generation:0};},C);
  await e.close(e.runtime);const r=await e.open();
  assert.equal((await r.record(r.handles.get('one'))).state,'processing');
  e.faux.setResponses([read(),done('success',['e2'])]);
  assert.match((await r.ask(meta(),'继续原目标')).text,/DONE: success/);
  assert.equal(e.calls.filter(c=>c.op==='reconcile_phone').length,1);
  assert.equal(e.calls.filter(c=>c.op==='phone'&&c.name==='tap').length,0);
  assert.equal(e.calls.filter(c=>c.op==='normalize').length,0);
});
test('memory remains usable while a phone action is being reconciled',async t=>{
  const e=await setup(t);await e.runtime.harness.commit(async tx=>{
    (await tx.doc(Index)).channel={blocked:'pending phone',owner:'phone-task'};
  },C);
  e.faux.setResponses([message(call('remember',{items:[{key:'coffee.drink',value:'美式',type:'preference'}]}),{stopReason:'toolUse'})]);
  assert.match((await e.runtime.ask(meta('memory','记住我喜欢美式'),'记住我喜欢美式')).text,/已经记住/);
  assert.equal(e.calls.length,0);
  assert.equal((await e.runtime.harness.snapshot(Index,C)).channel.owner,'phone-task');
});
test('queued phone tasks are retained while the preceding action is unresolved',async t=>{
  const e=await setup(t);await e.runtime.harness.commit(async tx=>{
    (await tx.doc(Index)).channel={blocked:'unconfirmed',owner:'old'};
  },C);
  assert.match((await e.runtime.ask(meta(),'read')).text,/ASK:.*任务已保留/);
  assert.equal(e.calls.length,0);assert.equal(e.faux.state.callCount,0);
  assert.equal((await e.runtime.record(e.runtime.handles.get('one'))).state,'need_user');
});
test('cancel retains the remote pending operation and the next task waits for its exact receipt',async t=>{
  let finished=false;
  const e=await setup(t,async(op,p)=>{
    if(op==='cancel_phone')return {drained:true,remote_pending:{operation_id:'cancel-operation-123',method:'tap'}};
    if(op==='operation_status')return {operation_id:p.operation_id,method:'tap',state:finished?'succeeded':'running'};
    if(op==='normalize')return {plan:{}};
    if(op==='phone')return {ok:true,has_screen:true,mutation_generation:0,result:{source:'next task'}};
    return {};
  });
  await e.runtime.checkpoint(meta());const conv=e.runtime.handles.get('one');
  await conv.commit(async tx=>{const s=await tx.doc(State,conv.id);s.phase='running';s.plan={};
    s.pending={name:'tap',operation_id:'cancel-operation-123',evidence_id:'e1',args:{x:60,y:40}};
    (await tx.doc(Index)).channel.owner='one';},C);
  await e.runtime.cancel('one');
  assert.match((await e.runtime.ask(meta('next'),'read')).text,/ASK:/);
  assert.equal(e.calls.filter(c=>c.op==='phone').length,0);
  finished=true;e.faux.setResponses([read(),done()]);
  assert.match((await e.runtime.ask({...meta('next'),history:['继续']},'继续')).text,/DONE: success/);
  assert.equal(e.calls.filter(c=>c.op==='phone').length,1);
  assert.equal((await e.runtime.record(conv)).result_kind,'cancelled');
});
