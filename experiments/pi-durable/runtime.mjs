import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { once } from 'node:events';
import { mkdir, chmod } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { performance } from 'node:perf_hooks';
import { BACKGROUND_CONTEXT as C } from '@earendil-works/chord/context';
import { createModels, createProvider } from '@earendil-works/pi-ai/models';
import { openAICompletionsApi } from '@earendil-works/pi-ai/api/openai-completions.lazy';
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai/utils/event-stream';
import {
  Harness, createRegistry, defineExtension, defineTool, defineDoc, section,
  hook, GenerationTask, ToolResultEntry, AssistantEntry,
} from '@earendil-works/pi-durable';
import { openNodeSqliteStorage } from '@earendil-works/pi-durable/storage/sqlite/node';

export { C, defineTool };
export const ROOT = fileURLToPath(new URL('../../', import.meta.url));
export const OUTPUT = path.join(ROOT, '.runtime/pi-durable');
export const Memory = defineDoc({kind:'dayu.memory', version:1, scope:'conversation',
  history:'latest', fork:'initial', initial:()=>({items:[]})});
export const Task = defineDoc({kind:'dayu.task', version:1, scope:'conversation',
  history:'latest', fork:'initial', initial:()=>({rid:'', state:'idle', question:null,
    questionSeq:0, audioId:null, overrides:{}, receipts:{}, blocked:false, pendingPhoneAction:null})});
const textOutput = value => [{type:'text', text:typeof value === 'string' ? value : JSON.stringify(value)}];

export class HostRpc {
  constructor() {
    this.seq=0; this.pending=new Map();
    this.child=spawn(path.join(ROOT,'host/.venv/bin/python'),
      [path.join(ROOT,'experiments/pi-durable/host_rpc.py')],
      {cwd:path.join(ROOT,'host'), stdio:['pipe','pipe','pipe']});
    this.stderr='';
    this.child.stderr.on('data',data=>{ this.stderr=(this.stderr+data).slice(-3000); });
    createInterface({input:this.child.stdout}).on('line',line=>{
      const msg=JSON.parse(line), waiter=this.pending.get(msg.id);
      if (!waiter) return;
      this.pending.delete(msg.id); clearTimeout(waiter.timer);
      if (msg.error) waiter.reject(Object.assign(new Error(msg.error),{uncertain:msg.uncertain,reason:msg.reason})); else waiter.resolve(msg.result);
    });
    this.child.on('exit',code=>{
      for (const p of this.pending.values()) {clearTimeout(p.timer);p.reject(new Error('host adapter exited '+code));}
      this.pending.clear();
    });
  }
  call(op,params={},timeout=60000) {
    const id=++this.seq;
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{this.pending.delete(id);reject(new Error('host RPC timeout: '+op));},timeout);
      this.pending.set(id,{resolve,reject,timer});
      this.child.stdin.write(JSON.stringify({id,op,params})+'\n');
    });
  }
  async close() {
    this.child.stdin.end();
    if(this.child.exitCode===null) await once(this.child,'exit');
  }
}

// No all-provider import, shell tools, code mode, image processor or child agents.
export function qwenModels(cfg, requests=[]) {
  const models=createModels(), base=openAICompletionsApi();
  const wrap=method=>(model,context,options={})=>base[method](model,context,{
    ...options, maxTokens:cfg.maxTokens, timeoutMs:cfg.timeoutMs, maxRetries:0,
    onPayload(payload) {
      payload.enable_thinking=false;
      payload.parallel_tool_calls=false;
      payload.max_tokens=cfg.maxTokens;
      delete payload.max_completion_tokens;
      requests.push({started:performance.now(), model:payload.model, stream:payload.stream,
        enable_thinking:payload.enable_thinking, parallel_tool_calls:payload.parallel_tool_calls,
        max_tokens:payload.max_tokens, messages:payload.messages.length,
        input_chars:JSON.stringify(payload.messages).length});
      return payload;
    },
    onResponse(response) {
      const r=requests.at(-1); if(r) {r.http_status=response.status;r.headers_ms=performance.now()-r.started;}
    },
  });
  models.setProvider(createProvider({id:'dayu-qwen', name:'Qwen (project config)', baseUrl:cfg.baseUrl,
    auth:{apiKey:{name:'project config',resolve:async()=>({auth:{apiKey:cfg.apiKey}})}},
    models:[{id:cfg.model,name:cfg.model,api:'openai-completions',provider:'dayu-qwen',
      baseUrl:cfg.baseUrl,reasoning:true,input:['text'],contextWindow:32000,maxTokens:cfg.maxTokens,
      cost:{input:0,output:0,cacheRead:0,cacheWrite:0},
      compat:{thinkingFormat:'qwen',supportsStore:false,supportsDeveloperRole:false,
        supportsReasoningEffort:false,supportsStrictMode:false,maxTokensField:'max_tokens'}}],
    api:{stream:wrap('stream'),streamSimple:wrap('streamSimple')}}));
  return {models,model:{provider:'dayu-qwen',modelId:cfg.model}};
}

export function dateInShanghai(date=new Date()) {
  return new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(date);
}
export function yesterday(date=new Date()) {
  const today=dateInShanghai(date);
  return new Date(Date.parse(today+'T12:00:00+08:00')-86400000).toISOString().slice(0,10);
}
export function completedOrders(items,date) {
  return items.filter(i=>i.type==='order' && i.date===date && i.status==='completed'
    && i.verified===true && typeof i.evidence==='string' && i.evidence.length>0);
}
export function activeOverrides(overrides,rid,date=dateInShanghai()) {
  return Object.fromEntries(Object.entries(overrides).filter(([,item])=>item && item.date===date
    && (item.scope==='today'||(item.scope==='task'&&item.taskId===rid&&rid)))
    .map(([key,item])=>[key,item.value]));
}

function memoryTools() {
  return [defineTool({name:'remember',description:'一次提交用户明确要求记住的多条长期偏好或笔记；今天/本次的要求使用 set_override。不能用此工具写入订单完成事实。',
    parameters:{type:'object',properties:{items:{type:'array',minItems:1,maxItems:12,items:{type:'object',
      properties:{key:{type:'string'},value:{type:'string'},type:{type:'string',enum:['preference','note']}},
      required:['key','value','type'],additionalProperties:false}}},required:['items'],additionalProperties:false},
    replay:'safe',executionMode:'sequential',
    async execute(args,api,ctx) {
      await api.commit(async tx=>{
        const doc=await tx.doc(Memory,api.conversationId);
        for(const item of args.items) {
          const value={...item,source:'explicit_user',updatedAt:new Date().toISOString()};
          const index=doc.items.findIndex(x=>x.type===item.type && x.key===item.key);
          if(index<0)doc.items.push(value);else doc.items[index]=value;
        }
      },ctx);
      return {output:textOutput('记住了。'),control:{terminate:true}};
    }}),defineTool({name:'set_override',description:'保存仅今天/当前任务使用的临时要求，不更改长期偏好。',
    parameters:{type:'object',properties:{key:{type:'string'},value:{type:'string'},
      scope:{type:'string',enum:['task','today'],description:'今天要求用today；仅本次任务用task。'}},required:['key','value'],additionalProperties:false},
    replay:'safe',executionMode:'sequential',
    async execute(args,api,ctx) {
      await api.commit(async tx=>{
        const t=await tx.doc(Task,api.conversationId);
        t.overrides[args.key]={value:args.value,scope:args.scope??(t.rid?'task':'today'),taskId:t.rid,date:dateInShanghai()};
      },ctx);
      return {output:textOutput('这次按你的要求。'),control:{terminate:true}};
    }})];
}

export function phoneTools(cfg,host) {
  return cfg.tools.map(def=>defineTool({...def,replay:'unsafe',executionMode:'sequential',
    async execute(args,api,ctx) {
      const task=await api.snapshot(Task,api.conversationId,ctx);
      if(task?.blocked || task?.state!=='running')
        throw new Error('phone task is not dispatchable');
      if(def.name==='ask_user') {
        await api.commit(async tx=>{
          const t=await tx.doc(Task,api.conversationId);
          t.state='need_user';t.question=args.question;t.questionSeq++;t.audioId=null;
        },ctx);
        return {output:textOutput({question:args.question}),control:{terminate:true}};
      }
      if(def.name==='finish_task') {
        let kind=args.kind;
        const receipts=task?.receipts??{};
        const ids=args.evidence_ids;
        const values=Object.values(receipts);
        const lastGeneration=Math.max(0,...values.map(v=>v.mutation_generation));
        if(kind==='success' && (!ids.length || ids.some(id=>!receipts[id]?.ok)
          || !ids.some(id=>receipts[id]?.mutation_generation===lastGeneration && receipts[id]?.has_screen)))
          kind='unknown';
        await api.commit(async tx=>{
          const t=await tx.doc(Task,api.conversationId);t.state='done';t.result={kind,text:args.text,evidence_ids:ids};
        },ctx);
        return {output:textOutput({kind,text:args.text}),control:{terminate:true}};
      }
      const onAbort=()=>{drain=host.call('cancel',{rid:task.rid});};
      let drain;
      ctx.abortSignal?.addEventListener('abort',onAbort,{once:true});
      try {
        if(ctx.abortSignal?.aborted)throw new Error('cancelled before dispatch');
        await api.commit(async tx=>{
          (await tx.doc(Task,api.conversationId)).pendingPhoneAction={callId:api.callId,name:def.name};
        },ctx);
        const result=await host.call('tool',{rid:task.rid,name:def.name,args});
        // Short task-local receipt IDs match the production protocol and avoid
        // asking the model to generate long provider call IDs in finish_task.
        const evidence_id='e'+(Object.keys(task.receipts).length+1);
        await api.commit(async tx=>{
          const t=await tx.doc(Task,api.conversationId);t.receipts[evidence_id]=result;t.pendingPhoneAction=null;
        },ctx);
        const {has_screen,...modelResult}=result;
        return {output:textOutput({...modelResult,evidence_id})};
      } catch(error) {
        if(!ctx.abortSignal?.aborted) {
          const uncertain=error.uncertain!==false;
          await api.commit(async tx=>{
            const t=await tx.doc(Task,api.conversationId);t.blocked=uncertain;t.state=uncertain?'unknown':'done';
            if(!uncertain)t.pendingPhoneAction=null;
            t.result={kind:uncertain?'unknown':'failed',text:error.message};
          },ctx);
          return {output:textOutput({kind:uncertain?'unknown':'failed',error:error.message}),isError:true,control:{terminate:true}};
        }
        throw error;
      } finally {
        if(drain)await drain;
        ctx.abortSignal?.removeEventListener('abort',onAbort);
      }
    }}));
}

export async function openRuntime({db,models,model,system='',tools=[],memory=false,requests=[],extraHooks=[],compaction={enabled:false}}) {
  await mkdir(path.dirname(db),{recursive:true,mode:0o700});
  const registry=createRegistry();
  const sections=[section('preamble',()=>system,{tag:false})];
  if(memory)sections.push(section('memory',async input=>{
    const m=await input.read.snapshot(Memory,input.conversationId,C);
    const t=await input.read.snapshot(Task,input.conversationId,C);
    return JSON.stringify({preferences:m?.items.filter(i=>i.type==='preference').slice(-24)??[],
      notes:m?.items.filter(i=>i.type==='note').slice(-8)??[],
      yesterday_completed_orders:completedOrders(m?.items??[],yesterday()).slice(-5),
      current_task_overrides:activeOverrides(t?.overrides??{},t?.rid??'')});
  },{tag:false}));
  let dispatchAllowed=async()=>false;
  // Hook errors are isolated by the harness. Enforcement belongs at dispatch:
  // freshly read durable policy before allowing any provider network request.
  const guardedModels=new Proxy(models,{get(target,key) {
    if(key==='stream'||key==='streamSimple')return (requested,context,options)=>{
      const stream=createAssistantMessageEventStream();
      void (async()=>{
        try {
          if(!await dispatchAllowed())throw new Error('Unknown external action: reconciliation required');
          const upstream=target[key](requested,context,options);
          for await(const event of upstream)stream.push(event);
          stream.end(await upstream.result());
        } catch(error) {
          const message={role:'assistant',content:[],api:requested.api,provider:requested.provider,
            model:requested.id,stopReason:'error',errorMessage:error.message,
            timestamp:Date.now(),usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,
              cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
          stream.push({type:'error',reason:'error',error:message});stream.end(message);
        }
      })();return stream;
    };
    const value=target[key];return typeof value==='function'?value.bind(target):value;
  }});
  const hooks=[hook(GenerationTask,{
    async onYield(_answer,api,ctx) {
      const task=await api.snapshot(Task,api.conversationId,ctx);
      if(tools.some(tool=>tool.name==='finish_task') && task?.state==='running')
        return {continue:'请使用 finish_task 或 ask_user 工具结束；不要只输出文字。'};
    },
    afterResponse(message) {
      const r=requests.at(-1);
      if(r){r.elapsed_ms=performance.now()-r.started;r.stopReason=message.stopReason;r.usage=message.usage;}
    },
    async afterTools(_assistant,ids,api,ctx) {
      await api.commit(async tx=>{
        const entries=await Promise.all(ids.map(id=>tx.entry(ToolResultEntry,id)));
        if(entries.some(e=>e?.data?.diagnostics?.some(d=>['interrupted','aborted'].includes(d.code)))) {
          const t=await tx.doc(Task,api.conversationId);t.blocked=true;t.state='unknown';
        }
      },ctx);
    },
  }),...extraHooks];
  registry.install(defineExtension({name:'dayu-validation',sections,tools:[...tools,...(memory?memoryTools():[])],hooks}));
  const storage=await openNodeSqliteStorage(db);
  const harness=await Harness.open(storage,{models:guardedModels,registry,settings:{
    toolExecution:'sequential',retry:{enabled:false,maxRetries:0},stream:{timeoutMs:30000,maxRetries:0},
    compaction,progress:{partialIntervalMs:100,outputIntervalMs:100},
  }},C);
  await chmod(db,0o600);
  const root=await harness.root(C,{agent:{model,thinkingLevel:'off'}});
  await root.commit(async tx=>{await tx.doc(Memory,root.id);await tx.doc(Task,root.id);},C);
  dispatchAllowed=async()=>{
    const task=await harness.snapshot(Task,root.id,C);
    return !!task && !task.blocked && !task.pendingPhoneAction
      && !['cancelling','cancelled'].includes(task.state);
  };
  return {harness,root,requests,
    async state(){return harness.snapshot(Task,root.id,C);},
    async memories(){return harness.snapshot(Memory,root.id,C);},
    async close(){await harness.close(C);}};
}

export async function submit(runtime,text,requestId,timeout=45000) {
  let timer;
  const work=(async()=>{
    const submission=await runtime.root.submit({type:'input',content:text,requestId},C);
    const settled=await submission.wait(C);
    if(settled.status!=='done')throw new Error('submission unanswered: '+JSON.stringify(settled));
    const entry=settled.answer?await runtime.root.commit(tx=>tx.entry(AssistantEntry,settled.answer),C):null;
    return {submissionId:submission.id,settled,
      answer:entry?.model?.[0]?.content?.filter(b=>b.type==='text').map(b=>b.text).join('')??'',
      state:await runtime.state()};
  })();
  try {return await Promise.race([work,new Promise((_,reject)=>{
    timer=setTimeout(()=>reject(new Error('submission timed out')),timeout);
  })]);} catch(error) {await runtime.root.abort(C);throw error;}
  finally {clearTimeout(timer);}
}

// Device-facing gate: no listening until a matching physical playback receipt.
// This is application policy, deliberately separate from Pi's generation queue.
export async function playbackDone(runtime,{rid,seq,audioId}) {
  return runtime.root.commit(async tx=>{
    const t=await tx.doc(Task,runtime.root.id);
    if(t.state!=='need_user'||t.rid!==rid||t.questionSeq!==seq||t.audioId!==audioId)return false;
    t.state='listening';return true;
  },C);
}
export async function acceptAnswer(runtime,{rid,seq,text}) {
  return runtime.root.commit(async tx=>{
    const t=await tx.doc(Task,runtime.root.id);
    if(t.state!=='listening'||t.rid!==rid||t.questionSeq!==seq||!text.trim())return false;
    t.state='running';t.question=null;t.audioId=null;return true;
  },C);
}
