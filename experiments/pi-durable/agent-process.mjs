// Test-only agent endpoint for the isolated Python voice Bridge.
import {createInterface} from 'node:readline';
import path from 'node:path';
import {HostRpc,qwenModels,openRuntime,phoneTools,submit,Task,C,OUTPUT} from './runtime.mjs';

const host=new HostRpc(),cfg=await host.call('init'),requests=[];
const db=process.argv[2]??path.join(OUTPUT,'voice.sqlite');
const runtime=await openRuntime({db,...qwenModels(cfg,requests),requests,system:cfg.system,tools:phoneTools(cfg,host)});
let currentRid='',cancelled=false;
const trace=path.join(path.dirname(db),'phone-traces');
const emit=msg=>process.stdout.write(JSON.stringify(msg)+'\n');

async function ask(params) {
  const {rid,original,prompt,answers=[]}=params;
  cancelled=false;
  const isNew=currentRid!==rid;
  currentRid=rid;
  const cancelledResult=async()=>{
    await runtime.root.commit(async tx=>{
      const t=await tx.doc(Task,runtime.root.id);t.rid=rid;t.state='cancelled';
    },C);
    return {text:'',returncode:-1,cancelled:true};
  };
  const previous=await runtime.state();
  if(cancelled)return cancelledResult();
  if(previous.blocked||previous.pendingPhoneAction)throw new Error('Phone reconciliation required before another task');
  if(isNew)await runtime.root.reset(undefined,C);
  if(cancelled)return cancelledResult();
  await runtime.root.commit(async tx=>{
    const t=await tx.doc(Task,runtime.root.id);t.rid=rid;t.state='preparing';
  },C);
  if(cancelled)return cancelledResult();
  const prepared=await host.call('normalize',{rid,text:original,answers,trace:path.join(path.dirname(db),'planning')});
  if(cancelled)return cancelledResult();
  await runtime.root.commit(async tx=>{
    const t=await tx.doc(Task,runtime.root.id);
    Object.assign(t,{rid,state:'running',receipts:{},blocked:false});
    t.plan=prepared.plan;
  },C);
  if(cancelled)return cancelledResult();
  if(prepared.plan.clarification) {
    await runtime.root.commit(async tx=>{
      const t=await tx.doc(Task,runtime.root.id);t.state='need_user';t.question=prepared.plan.clarification;t.questionSeq++;
    },C);
    if(cancelled)return cancelledResult();
    return {text:'ASK: '+prepared.plan.clarification,returncode:0,cancelled:false};
  }
  await host.call('begin',{rid,plan:prepared.plan,trace});
  if(cancelled)return cancelledResult();
  const offset=requests.length;
  const result=await submit(runtime,prompt+'\n按本轮整理后的任务规范执行，页面事实仍须实际验证：\n'+JSON.stringify(prepared.plan),
    rid+':'+answers.length);
  if(cancelled)return {text:'',returncode:-1,cancelled:true};
  const t=result.state;
  if(t.state==='need_user')return {text:'ASK: '+t.question,returncode:0,cancelled:false};
  if(!t.result)return {text:'DONE: unknown; 未取得经过验证的任务结果',returncode:0,cancelled:false};
  return {text:'DONE: '+t.result.kind+'; '+t.result.text,returncode:0,cancelled:false,
    evidence:{backend:'pi-validation',trace,modelCalls:requests.length-offset}};
}

const jobs=new Set();
for await(const line of createInterface({input:process.stdin})) {
  const msg=JSON.parse(line);
  const job=(async()=>{
    try {
      if(msg.op==='ask')emit({id:msg.id,result:await ask(msg.params)});
      else if(msg.op==='cancel') {
        cancelled=true;
        await runtime.root.commit(async tx=>{(await tx.doc(Task,runtime.root.id)).state='cancelling';},C);
        await runtime.root.abort(C);
        try {
          await host.call('cancel',{rid:currentRid});
          await runtime.root.commit(async tx=>{
            const t=await tx.doc(Task,runtime.root.id);t.state='cancelled';t.pendingPhoneAction=null;
          },C);
        } catch(error) {
          await runtime.root.commit(async tx=>{
            const t=await tx.doc(Task,runtime.root.id);t.state='unknown';t.blocked=true;
          },C);
          throw error;
        }
        emit({id:msg.id,result:{drained:true}});
      } else if(msg.op==='state')emit({id:msg.id,result:await runtime.state()});
    } catch(error) {
      emit({id:msg.id,...(msg.op==='ask'&&cancelled?{result:{text:'',returncode:-1,cancelled:true}}:{error:error.message})});
    }
  })();
  jobs.add(job);job.finally(()=>jobs.delete(job));
}
await Promise.allSettled(jobs);await runtime.close();await host.close();
