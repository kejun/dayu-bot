import {createInterface} from 'node:readline';
import {pathToFileURL} from 'node:url';
import {Runtime} from './runtime.mjs';
import {qwenModels} from './qwen.mjs';

export async function serve(makeModels=qwenModels) {
  let runtime,config,sequence=0,disconnected=false;
  const pending=new Map(),jobs=new Set();
  const clean=text=>String(text).replaceAll(config?.apiKey||'\0','[redacted]').slice(0,600);
  const emit=message=>process.stdout.write(JSON.stringify(message)+'\n');
  const host=(op,params)=>new Promise((resolve,reject)=>{
    if(disconnected)return reject(new Error('Bridge 已断开'));
    const call_id=++sequence;
    const timer=setTimeout(()=>{pending.delete(call_id);reject(new Error('Bridge 调用超时，结果未确认'));},
      ['normalize','replan'].includes(op)?config.planTimeoutMs+3000:config.phoneTimeoutMs+(config.recoveryTimeoutMs??45000)+40000);
    pending.set(call_id,{resolve,reject,timer});emit({call_id,op,params});
  });
  async function handle(message) {
    try {
      let result;
      if(message.op==='init') {
        if(runtime)throw new Error('Pi 已初始化');
        config=message.params;
        const {models,model}=makeModels(config,data=>emit({event:data}));
        runtime=new Runtime(config,host,models,model,data=>emit({event:data}));
        result=await runtime.open();
      } else if(!runtime)throw new Error('Pi 尚未初始化');
      else if(message.op==='checkpoint')result=await runtime.checkpoint(message.params);
      else if(message.op==='ask')result=await runtime.ask(message.params.task,message.params.prompt);
      else if(message.op==='cancel')result=await runtime.cancel(message.params.rid);
      else if(message.op==='recover')result=await runtime.recover();
      else if(message.op==='shutdown'){await runtime.close();runtime=null;result={closed:true};}
      else throw new Error('未知 Pi 操作');
      emit({id:message.id,result});
    } catch(error) {emit({id:message.id,error:clean(error.message),fatal:!!error.fatal});}
  }
  try {
    for await(const line of createInterface({input:process.stdin})) {
      const message=JSON.parse(line);
      if(message.reply_to) {
        const waiter=pending.get(message.reply_to);if(!waiter)continue;
        pending.delete(message.reply_to);clearTimeout(waiter.timer);
        if(message.error)waiter.reject(Object.assign(new Error(message.error),{
          fatal:message.fatal,uncertain:message.uncertain,validation:message.validation,reason:message.reason,
          mutation_generation:message.mutation_generation,recoverable:message.recoverable,user_message:message.user_message,
          diagnosable:message.diagnosable,operation:message.operation}));
        else waiter.resolve(message.result);
        continue;
      }
      const job=handle(message);jobs.add(job);job.finally(()=>jobs.delete(job));
    }
  } finally {
    disconnected=true;
    for(const waiter of pending.values()){clearTimeout(waiter.timer);waiter.reject(new Error('Bridge 已断开'));}
    await runtime?.close();await Promise.allSettled(jobs);
  }
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href)
  serve().catch(error=>{process.stderr.write('Pi runtime stopped: '+String(error.message).slice(0,300)+'\n');process.exitCode=1;});
