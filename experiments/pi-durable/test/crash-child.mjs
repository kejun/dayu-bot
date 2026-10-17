import { appendFile, readFile } from 'node:fs/promises';
import { createModels } from '@earendil-works/pi-ai/models';
import { fauxProvider, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';
import { openRuntime, defineTool, C, Task } from '../runtime.mjs';

const [mode,db,journal,replay]=process.argv.slice(2);
const faux=fauxProvider();const models=createModels();models.setProvider(faux.provider);
const effect=defineTool({name:'external_effect',description:'test external action',
  parameters:{type:'object',properties:{},additionalProperties:false},replay,
  async execute(_args,api) {
    let records=[];
    try {records=(await readFile(journal,'utf8')).trim().split('\n').filter(Boolean).map(JSON.parse);} catch{}
    // Even a replay-safe tool requires external idempotency using its durable call ID.
    if(!records.some(x=>x.callId===api.callId))
      await appendFile(journal,JSON.stringify({callId:api.callId,event:'effect'})+'\n',{mode:0o600});
    if(mode==='start') {
      console.log(JSON.stringify({event:'effect_started',callId:api.callId}));
      await new Promise(()=>{});
    }
    return {output:[{type:'text',text:'effect confirmed'}]};
  }});
if(mode==='start')faux.setResponses([fauxAssistantMessage(fauxToolCall('external_effect',{}),{stopReason:'toolUse'})]);
else faux.setResponses([fauxAssistantMessage('recovered safely')]);
const runtime=await openRuntime({db,models,model:{provider:faux.provider.id,modelId:faux.getModel().id},tools:[effect]});
try {
  const submission=await runtime.root.submit({type:'input',content:'test external action',requestId:'crash-task'},C);
  const settled=await submission.wait(C);
  console.log(JSON.stringify({event:'settled',settled,state:await runtime.state(),modelCalls:faux.state.callCount}));
} finally {await runtime.close();}
