import assert from 'node:assert/strict';
import {writeFile,mkdir} from 'node:fs/promises';
import {DatabaseSync} from 'node:sqlite';
import path from 'node:path';
import {performance} from 'node:perf_hooks';
import {HostRpc,qwenModels,openRuntime,submit,Memory,C,OUTPUT} from './runtime.mjs';

const dir=path.join(OUTPUT,'context-'+Date.now());await mkdir(dir,{recursive:true,mode:0o700});
const db=path.join(dir,'context.sqlite'),host=new HostRpc(),cfg=await host.call('init'),requests=[];
const runtime=await openRuntime({db,...qwenModels(cfg,requests),requests,memory:true,
  system:'你是手机助手。只根据记录简短回答，不执行新动作。',
  compaction:{enabled:false,keepRecentTokens:256,reserveTokens:2048,backgroundTokens:0}});
const report={physicalPhone:false,seededHistory:true};
try {
  await runtime.root.commit(async tx=>{
    (await tx.doc(Memory,runtime.root.id)).items.push({type:'preference',key:'coffee.drink',value:'美式',source:'test_fixture'});
  },C);
  for(let i=0;i<72;i++) {
    const content=`历史页面${i}：`+'仅供测试的商品列表，旧页面不能作为当前操作证据。'.repeat(12);
    await runtime.root.submit({type:'write',entry:{kind:'validation.history',model:[{role:'user',content,timestamp:Date.now()}]}},C);
  }
  await runtime.root.submit({type:'write',entry:{kind:'validation.checkpoint',model:[{
    role:'user',content:'最新任务编号R100。任务已经暂停，正在等待用户回答“要热的还是冷的？”。尚未下单。',timestamp:Date.now()}]}},C);
  const started=performance.now();
  const taskId=await runtime.root.compact('用不超过80个汉字总结旧记录，只保留任务状态和限制，不枚举商品。',C);
  const task=await runtime.harness.waitForTask(taskId,C);
  report.compaction_ms=performance.now()-started;
  report.outcome=task.state.outcome;
  assert.equal(task.state.outcome.status,'completed');
  const result=await submit(runtime,'当前任务编号是什么，处于什么状态？我的长期咖啡品类是什么？只核对记录。','after-compaction');
  report.answer=result.answer;
  assert.match(result.answer,/R100/);assert.match(result.answer,/等待|暂停/);assert.match(result.answer,/美式/);
  report.requests=requests;report.totalModelCalls=requests.length;
  report.passed=true;
} catch(error) {report.passed=false;report.error=error.message;process.exitCode=1;}
finally {
  await runtime.close();await host.close();
  const sqlite=new DatabaseSync(db,{readOnly:true});
  report.storedHistoryEntries=sqlite.prepare("SELECT count(*) AS n FROM entries WHERE json_extract(record,'$.kind')='validation.history'").get().n;
  sqlite.close();
  await writeFile(path.join(dir,'report.json'),JSON.stringify(report,null,2)+'\n',{mode:0o600});
  console.log(JSON.stringify({report:path.join(dir,'report.json'),...report}));
}
