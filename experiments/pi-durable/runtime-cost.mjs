import {createModels} from '@earendil-works/pi-ai/models';
import {fauxProvider,fauxAssistantMessage,fauxToolCall} from '@earendil-works/pi-ai/providers/faux';
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {performance} from 'node:perf_hooks';
import path from 'node:path';
import {HostRpc,openRuntime,submit,phoneTools,Task,C,OUTPUT} from './runtime.mjs';

const dir=path.join(OUTPUT,'runtime-cost-'+Date.now());await mkdir(dir,{recursive:true,mode:0o700});
const host=new HostRpc(),cfg=await host.call('init');
const fixture=JSON.parse(await readFile(path.join(OUTPUT,'phone-fixture.json'),'utf8'));
const faux=fauxProvider(),models=createModels();models.setProvider(faux.provider);
const runtime=await openRuntime({db:path.join(dir,'session.sqlite'),models,
  model:{provider:faux.provider.id,modelId:faux.getModel().id},system:cfg.system,tools:phoneTools(cfg,host)});
const stats=rows=>{const a=[...rows].sort((x,y)=>x-y);return {n:a.length,median_ms:a[Math.ceil(a.length*.5)-1],p95_ms:a[Math.ceil(a.length*.95)-1]};};
const report={mockModels:true,replayedPhoneSnapshot:true,cloud:false,realPhoneActions:false,samples:100};
try {
  const direct=await host.call('local_benchmark',{fixture,samples:100,trace:path.join(dir,'direct')});
  report.direct=stats(direct.timings_ms);
  const timings=[];
  for(let i=0;i<110;i++) {
    const rid='local-'+i;
    await runtime.root.reset(undefined,C);
    await runtime.root.commit(async tx=>Object.assign(await tx.doc(Task,runtime.root.id),{
      rid,state:'running',receipts:{},blocked:false,pendingPhoneAction:null}),C);
    await host.call('begin',{rid,fixture,plan:{read_only:true},trace:path.join(dir,'phone')});
    faux.setResponses([
      fauxAssistantMessage(fauxToolCall('read_screen',{}),{stopReason:'toolUse'}),
      fauxAssistantMessage(fauxToolCall('finish_task',{kind:'success',text:'已读取页面。',evidence_ids:['e1']}),{stopReason:'toolUse'}),
    ]);
    const started=performance.now();
    const result=await submit(runtime,'读取当前页面',rid);
    if(result.state.result?.kind!=='success')throw new Error('local fixture task failed');
    if(i>=10)timings.push(performance.now()-started);
  }
  report.pi=stats(timings);
  report.p95_added_ms=report.pi.p95_ms-report.direct.p95_ms;
  report.node_rss_bytes=process.memoryUsage().rss;
  report.python=await host.call('stats');report.passed=true;
} catch(error) {report.passed=false;report.error=error.message;process.exitCode=1;}
finally {
  await runtime.close();await host.close();
  await writeFile(path.join(dir,'report.json'),JSON.stringify(report,null,2)+'\n',{mode:0o600});
  console.log(JSON.stringify({report:path.join(dir,'report.json'),...report}));
}
