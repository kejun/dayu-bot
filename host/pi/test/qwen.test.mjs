import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {qwenModels} from '../qwen.mjs';

test('Qwen keeps short execution output and honors the larger compaction budget on the wire',async t=>{
  const payloads=[];
  const server=http.createServer(async(req,res)=>{
    let body='';for await(const part of req)body+=part;
    payloads.push(JSON.parse(body));
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    res.end('data: '+JSON.stringify({id:'local',object:'chat.completion.chunk',created:1,model:'local-qwen',
      choices:[{index:0,delta:{role:'assistant',content:'summary'},finish_reason:'stop'}]})+'\n\ndata: [DONE]\n\n');
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));t.after(()=>server.close());
  const {models,model}=qwenModels({baseUrl:`http://127.0.0.1:${server.address().port}/v1`,
    model:'local-qwen',apiKey:'local-test',maxTokens:384,requestTimeoutMs:1000});
  const target=models.getModel(model.provider,model.modelId);
  const context={messages:[{role:'user',content:'summarize',timestamp:1}]};
  assert.equal((await models.completeSimple(target,context)).stopReason,'stop');
  assert.equal((await models.completeSimple(target,context,{maxTokens:3276})).stopReason,'stop');
  assert.deepEqual(payloads.map(p=>p.max_tokens),[384,3276]);
  assert.ok(target.maxTokens>=3276);
  for(const p of payloads){assert.equal(p.enable_thinking,false);assert.equal(p.parallel_tool_calls,false);assert.equal(p.max_completion_tokens,undefined);}
});
