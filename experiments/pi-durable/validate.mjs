import assert from 'node:assert/strict';
import { readFile, writeFile, mkdir, stat } from 'node:fs/promises';
import { performance } from 'node:perf_hooks';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {
  HostRpc,qwenModels,openRuntime,submit,phoneTools,Memory,Task,C,OUTPUT,yesterday,completedOrders,
} from './runtime.mjs';

const options=Object.fromEntries(process.argv.slice(2).map(a=>a.replace(/^--/,'').split('=')));
const samples=Number(options.samples??20),phoneSamples=Number(options['phone-samples']??3);
const runId=new Date().toISOString().replaceAll(/[:.]/g,'-');
const runDir=path.join(OUTPUT,runId);
await mkdir(runDir,{recursive:true,mode:0o700});
const host=new HostRpc(),cfg=await host.call('init');
const sourceHash=createHash('sha256');
for(const file of ['runtime.mjs','host_rpc.py','validate.mjs'])sourceHash.update(await readFile(new URL(file,import.meta.url)));
const report={runId,date:new Date().toISOString(),piVersion:'1.3.0',node:process.version,model:cfg.model,
  sourceHash:sourceHash.digest('hex'),
  parameters:{thinking:false,parallelTools:false,maxOutputTokens:cfg.maxTokens,timeoutMs:cfg.timeoutMs},
  scope:'isolated prototype; production Bridge and firmware unchanged',results:{}};
const log=(event,data={})=>console.log(JSON.stringify({event,...data}));
const save=async()=>writeFile(path.join(runDir,'report.json'),JSON.stringify(report,null,2)+'\n',{mode:0o600});
const stats=values=>{
  const sorted=[...values].sort((a,b)=>a-b);
  const q=p=>sorted[Math.max(0,Math.ceil(sorted.length*p)-1)];
  return {n:values.length,median_ms:q(.5),p95_ms:q(.95),min_ms:sorted[0],max_ms:sorted.at(-1)};
};

async function memoryValidation() {
  const requests=[],q=qwenModels(cfg,requests),db=path.join(runDir,'memory.sqlite');
  const system='你是轻量个人助手。简短中文回答。明确要求记住的长期偏好/笔记，一次 remember 提交全部。'
    +'偏好 key 固定为 coffee.drink、coffee.temperature、coffee.sugar。今天/本次使用 set_override，不改变长期值。'
    +'只根据提供的已核实订单记录回答；测试记录是测试数据。只召回，不实际操作手机。';
  let r=await openRuntime({db,...q,system,memory:true,requests});
  const result={};
  try {
    const before=requests.length;
    await submit(r,'帮我记住：以后咖啡默认美式、热的、不加糖。','remember-batch');
    const saved=await r.memories();
    assert.equal(saved.items.length,3);assert.match(JSON.stringify(saved),/美式/);
    result.batch={committedItems:saved.items.length,modelCalls:requests.length-before};
    await r.close();r=await openRuntime({db,...q,system,memory:true,requests});
    await r.root.reset(undefined,C); // Proves recall comes from documents, not replayed user text.
    const recall=await submit(r,'我之前让你记住的咖啡偏好是什么？','recall');
    assert.match(recall.answer,/美式/);assert.match(recall.answer,/热/);assert.match(recall.answer,/无糖|不加糖/);
    result.afterRestartRecall={answer:recall.answer,passed:true};
    await submit(r,'今天这次换成拿铁，长期习惯不变。','temporary');
    assert.match(JSON.stringify((await r.state()).overrides),/拿铁/);
    assert.match(JSON.stringify((await r.memories()).items.find(i=>i.key==='coffee.drink')),/美式/);
    result.temporaryDoesNotOverwrite=true;
    await submit(r,'以后咖啡默认改为拿铁，记住这个长期偏好。','change-preference');
    assert.match(JSON.stringify((await r.memories()).items.find(i=>i.key==='coffee.drink')),/拿铁/);
    assert.equal((await r.memories()).items.filter(i=>i.key==='coffee.drink').length,1);
    result.permanentUpdate=true;
    const date=yesterday();
    const fixture={type:'order',key:'fixture-order-completed',date,status:'completed',verified:true,
      evidence:'fixture:completed-receipt',store:'测试咖啡店A',product:'燕麦拿铁',size:'中杯',temperature:'热',sugar:'无糖',source:'test_fixture'};
    await r.root.commit(async tx=>{
      const m=await tx.doc(Memory,r.root.id);
      m.items.push(fixture,{...fixture,key:'fixture-unfinished',status:'selected',store:'未支付候选B',product:'焦糖冰摩卡'},
        {...fixture,key:'fixture-unverified',verified:false,store:'未核实C',product:'冰美式'});
    },C);
    await r.root.reset(undefined,C);
    const order=await submit(r,'按照昨天下单的那杯咖啡再来一杯。先告诉我应该复用哪家店、什么规格，仅核对记忆，不操作手机。','yesterday');
    assert.match(order.answer,/燕麦拿铁/);assert.match(order.answer,/咖啡店A/);
    assert.doesNotMatch(order.answer,/焦糖|冰摩卡|未核实C|未支付候选B/);
    assert.equal(completedOrders((await r.memories()).items,date).length,1);
    result.yesterdayOrder={date,answer:order.answer,fixtureOnly:true,passed:true};
    const timings=[];
    for(let i=0;i<100;i++){
      const start=performance.now();
      const m=await r.memories();completedOrders(m.items,date);
      timings.push(performance.now()-start);
    }
    result.localMemoryRead=stats(timings);result.requests=requests;
    result.passed=true;
    return result;
  } finally {await r.close();}
}

async function prepare(text) {
  const start=performance.now();
  const result=await host.call('normalize',{text,trace:path.join(runDir,'planning')});
  return {...result,elapsed_ms:performance.now()-start,
    message:'按本轮整理后的任务规范执行，页面事实仍须实际验证：\n'+JSON.stringify(result.plan)};
}
async function resetPhone(r,rid,plan,fixture) {
  await r.root.reset(undefined,C);
  await r.root.commit(async tx=>Object.assign(await tx.doc(Task,r.root.id),{
    rid,state:'running',question:null,receipts:{},blocked:false,overrides:{},pendingPhoneAction:null}),C);
  await host.call('begin',{rid,plan,fixture,trace:path.join(runDir,'phone-tools')});
}
async function piRun(r,text,plan,fixture,label) {
  const rid=label+'-'+crypto.randomUUID();
  await resetPhone(r,rid,plan.plan,fixture);
  // Same two user messages as DirectAgent: original utterance, then shared plan.
  await r.root.submit({type:'write',entry:{kind:'validation.original',model:[{role:'user',content:text,timestamp:Date.now()}]}},C);
  const offset=r.requests.length,start=performance.now();
  const result=await submit(r,plan.message,rid);
  const elapsed_ms=performance.now()-start;
  const state=result.state;
  assert.equal(state.result?.kind,'success',JSON.stringify(state));
  return {backend:'pi',elapsed_ms,modelCalls:r.requests.length-offset,
    model_ms:r.requests.slice(offset).reduce((sum,x)=>sum+(x.elapsed_ms??0),0),
    toolCalls:Object.keys(state.receipts).length,result:state.result.text,
    requestParameters:r.requests.slice(offset).map(({model,stream,enable_thinking,parallel_tool_calls,max_tokens,input_chars})=>
      ({model,stream,enable_thinking,parallel_tool_calls,max_tokens,input_chars}))};
}
async function directRun(text,plan,fixture,label) {
  const trace=path.join(runDir,'direct'),start=performance.now();
  const result=await host.call('direct',{text,plan:plan.plan,plan_message:plan.message,fixture,
    rid:label+'-'+crypto.randomUUID(),trace});
  const elapsed_ms=performance.now()-start;
  assert.match(result.text,/^DONE: success;/);
  const events=(await readFile(path.join(trace,result.evidence.trace_id,'events.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
  const calls=events.filter(e=>e.event==='model');
  return {backend:'direct',elapsed_ms,modelCalls:calls.length,model_ms:calls.reduce((sum,x)=>sum+x.elapsed_ms,0),
    toolCalls:events.filter(e=>e.event==='tool').length,result:result.text};
}
async function benchmarks({real,samples:count}) {
  const requests=[],q=qwenModels(cfg,requests);
  const r=await openRuntime({db:path.join(runDir,real?'phone.sqlite':'benchmark.sqlite'),...q,
    system:cfg.system,tools:phoneTools(cfg,host),requests});
  const scenarios=[{name:'read',text:'只读取当前手机页面，用一句话总结，不要点击。'},
    {name:'open',text:'打开淘宝并读取当前页面，用一句话总结，不要点击任何按钮。'}];
  const result={realPhone:real,samplesPerBackendPerScenario:count,scenarios:[],method:
    'Interleaved backend order; shared production plan; same narrowed tool schemas; warm persistent HTTP clients; execution timing includes model, IPC, tools and SQLite. Planning recorded separately. Pi streams SSE; Direct uses nonstream JSON.'};
  try {
    // Ensure a real Taobao screen before capture; no product selection or checkout.
    const openPlan=await prepare(scenarios[1].text);
    if(real)result.warmup=await piRun(r,scenarios[1].text,openPlan,undefined,'real-warmup');
    const fixture=real?undefined:JSON.parse(await readFile(path.join(OUTPUT,'phone-fixture.json'),'utf8'));
    for(const scenario of scenarios) {
      const plan=scenario.name==='open'?openPlan:await prepare(scenario.text);
      const rows=[],replay=real?undefined:fixture;
      // Exclude one warmup for each backend per scenario from summary.
      await directRun(scenario.text,plan,replay,'warmup');await piRun(r,scenario.text,plan,replay,'warmup');
      for(let i=0;i<count;i++) {
        const order=i%2===0?['direct','pi']:['pi','direct'];
        for(const backend of order) {
          const row=backend==='direct'?await directRun(scenario.text,plan,replay,scenario.name):
            await piRun(r,scenario.text,plan,replay,scenario.name);
          rows.push({...row,pair:i});log('benchmark_sample',{real,scenario:scenario.name,pair:i,backend,elapsed_ms:Math.round(row.elapsed_ms)});
        }
        report.inProgress={real,scenario:scenario.name,pair:i};await save();
      }
      const summary=Object.fromEntries(['direct','pi'].map(backend=>{
        const selected=rows.filter(row=>row.backend===backend);
        return [backend,{...stats(selected.map(x=>x.elapsed_ms)),modelCalls:[...new Set(selected.map(x=>x.modelCalls))],
          toolCalls:[...new Set(selected.map(x=>x.toolCalls))]}];
      }));
      const p95Ratio=summary.pi.p95_ms/summary.direct.p95_ms;
      result.scenarios.push({name:scenario.name,planning_ms:plan.elapsed_ms,plan:plan.plan,summary,p95Ratio,
        thresholdPass:p95Ratio<=1.1,noExtraModelRounds:Math.max(...summary.pi.modelCalls)<=Math.max(...summary.direct.modelCalls),rows});
      log('benchmark_summary',{real,scenario:scenario.name,summary,p95Ratio});
    }
    result.processMemory={node_rss_bytes:process.memoryUsage().rss,python:await host.call('stats')};
    result.sqlite_bytes=(await stat(path.join(runDir,real?'phone.sqlite':'benchmark.sqlite'))).size;
    if(real) {
      const capture=await host.call('capture');
      assert.match(capture.screen.bundle_id,/^com\.taobao\./,'capture must be actual Taobao foreground');
      await writeFile(path.join(OUTPUT,'phone-fixture.json'),JSON.stringify(capture),{mode:0o600});
    }
    return result;
  } finally {await r.close();}
}

try {
  log('started',{runDir,model:cfg.model});
  if(!options.only||options.only==='memory'){
    report.results.memory=await memoryValidation();await save();log('memory_passed');
  }
  if(!options.only||options.only==='phone'){
    report.results.phone=await benchmarks({real:true,samples:phoneSamples});await save();
  }
  if(!options.only||options.only==='benchmark'){
    report.results.benchmark=await benchmarks({real:false,samples});await save();
  }
  delete report.inProgress;report.completed=true;
} catch(error) {report.error={name:error.name,message:error.message};log('failed',report.error);process.exitCode=1;}
finally {
  await save();await host.close();log('report',{path:path.join(runDir,'report.json')});
}
