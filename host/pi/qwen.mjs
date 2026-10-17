import {createModels, createProvider} from '@earendil-works/pi-ai/models';
import {openAICompletionsApi} from '@earendil-works/pi-ai/api/openai-completions.lazy';

export function qwenModels(config, event=()=>{}) {
  const models=createModels(), api=openAICompletionsApi();
  const wrap=name=>(model,context,options={})=>{
    // Generations use the short execution budget; the harness explicitly asks
    // for a larger budget when summarizing a long conversation.
    const maxTokens=options.maxTokens??config.maxTokens;
    return api[name](model,context,{
      ...options,maxTokens,timeoutMs:config.requestTimeoutMs,maxRetries:0,
      onPayload(payload) {
        payload.enable_thinking=false;payload.parallel_tool_calls=false;
        payload.max_tokens=maxTokens;delete payload.max_completion_tokens;
        event({event:'model_request',model:payload.model,thinking:false,
          input_chars:JSON.stringify(payload.messages).length,max_tokens:maxTokens});
        return payload;
      },
    });
  };
  models.setProvider(createProvider({id:'dayu-qwen',name:'Qwen',baseUrl:config.baseUrl,
    auth:{apiKey:{name:'Bridge config',resolve:async()=>({auth:{apiKey:config.apiKey}})}},
    models:[{id:config.model,name:config.model,api:'openai-completions',provider:'dayu-qwen',
      baseUrl:config.baseUrl,reasoning:true,input:['text'],contextWindow:32000,maxTokens:Math.max(config.maxTokens,3276),
      cost:{input:0,output:0,cacheRead:0,cacheWrite:0},compat:{thinkingFormat:'qwen',
        supportsStore:false,supportsDeveloperRole:false,supportsReasoningEffort:false,
        supportsStrictMode:false,maxTokensField:'max_tokens'}}],
    api:{stream:wrap('stream'),streamSimple:wrap('streamSimple')}}));
  return {models,model:{provider:'dayu-qwen',modelId:config.model}};
}
