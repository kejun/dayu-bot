import {createModels} from '@earendil-works/pi-ai/models';
import {fauxProvider,fauxAssistantMessage,fauxToolCall} from '@earendil-works/pi-ai/providers/faux';
import {serve} from '../main.mjs';

await serve(()=>{
  const models=createModels(),faux=fauxProvider();models.setProvider(faux.provider);
  const response=(context,options)=>{
    const tools=new Map();
    for(const m of context.messages.filter(m=>m.role==='system')) {
      for(const t of m.toolsAdded??[])tools.set(t.name,t);
      for(const t of m.toolsRemoved??[])tools.delete(t.name);
    }
    const users=context.messages.filter(m=>m.role==='user').map(m=>typeof m.content==='string'?m.content:'').join('\n');
    const last=context.messages.at(-1);
    if(users.includes('测试迟到回执')) {
      const next=last?.role!=='toolResult'&&!users.includes('恢复核实')?['open_app',{app:'微信'}]:
        ['finish_task',{kind:'success',text:'原动作已核实，任务完成。',evidence_ids:['e1']}];
      return fauxAssistantMessage(fauxToolCall(...next),{stopReason:'toolUse'});
    }
    if(users.includes('测试无障碍恢复')) {
      const next=last?.role!=='toolResult'?['open_app',{app:'微信'}]:
        last.toolName==='open_app'?['find_element',{text:'公众号'}]:
        last.toolName==='find_element'?['read_screen',{mode:'ocr'}]:
        last.toolName==='read_screen'?['tap',{x:60,y:40}]:
        ['finish_task',{kind:'success',text:'已恢复浏览。',evidence_ids:['e3']}];
      return fauxAssistantMessage(fauxToolCall(...next),{stopReason:'toolUse'});
    }
    if(tools.has('remember')&&!tools.has('read_screen'))
      return fauxAssistantMessage(fauxToolCall('remember',{items:[{key:'coffee.drink',value:'美式',type:'preference'}]}),{stopReason:'toolUse'});
    if(users.includes('测试问题')&&!users.includes('用户补充'))
      return fauxAssistantMessage(fauxToolCall('ask_user',{question:'热的还是冷的？'}),{stopReason:'toolUse'});
    if(last?.role==='toolResult')return fauxAssistantMessage(fauxToolCall('finish_task',{
      kind:'success',text:'已读取当前页面。',evidence_ids:['e1']}),{stopReason:'toolUse'});
    return fauxAssistantMessage(fauxToolCall('read_screen',{}),{stopReason:'toolUse'});
  };
  faux.setResponses(Array.from({length:100},()=>response));
  return {models,model:{provider:faux.provider.id,modelId:faux.getModel().id}};
});
