import assert from 'node:assert/strict';
import {createModels} from '@earendil-works/pi-ai/models';
import {fauxProvider} from '@earendil-works/pi-ai/providers/faux';
import {mkdir,stat,writeFile} from 'node:fs/promises';
import path from 'node:path';
import {performance} from 'node:perf_hooks';
import {C,Memory,OUTPUT,completedOrders,dateInShanghai,openRuntime,yesterday} from './runtime.mjs';

const dir=path.join(OUTPUT,'memory-scale-'+Date.now());await mkdir(dir,{recursive:true,mode:0o700});
const faux=fauxProvider(),models=createModels();models.setProvider(faux.provider);
const options={db:path.join(dir,'session.sqlite'),models,model:{provider:faux.provider.id,modelId:faux.getModel().id}};
let runtime=await openRuntime(options);
const report={fixtureOnly:true,cloudCalls:0};
try {
  const latest=yesterday(),base=Date.parse(latest+'T12:00:00+08:00');
  const items=Array.from({length:3000},(_,i)=>({type:'order',key:'order-'+i,date:dateInShanghai(new Date(base-i*86400000)),
    status:'completed',verified:true,evidence:'fixture:receipt-'+i,store:'测试店',product:'美式',size:'中杯',temperature:'热',sugar:'无糖'}));
  items.push(...Array.from({length:300},(_,i)=>({type:'note',key:'note-'+i,value:'测试笔记'+i})),
    ...Array.from({length:24},(_,i)=>({type:'preference',key:'preference-'+i,value:'测试偏好'+i})));
  await runtime.root.commit(async tx=>{(await tx.doc(Memory,runtime.root.id)).items.push(...items);},C);
  await runtime.close();
  const coldStart=performance.now();runtime=await openRuntime(options);
  const first=await runtime.memories();report.reopen_and_first_read_ms=performance.now()-coldStart;
  assert.equal(first.items.length,3324);
  const times=[];
  for(let i=0;i<100;i++) {
    const start=performance.now(),m=await runtime.memories();
    const orders=completedOrders(m.items,latest);
    const context={preferences:m.items.filter(x=>x.type==='preference').slice(-24),
      notes:m.items.filter(x=>x.type==='note').slice(-8),yesterday_completed_orders:orders.slice(-5)};
    const chars=JSON.stringify(context).length;
    times.push(performance.now()-start);
    assert.equal(orders.length,1);assert.equal(orders[0].key,'order-0');
    report.context_chars=chars;
  }
  times.sort((a,b)=>a-b);
  report.items=first.items.length;report.orders=3000;report.notes=300;
  report.local_snapshot_filter_serialize={n:times.length,median_ms:times[49],p95_ms:times[94]};
  assert.ok(times[94]<=50);report.passed=true;
} catch(error) {report.passed=false;report.error=error.message;process.exitCode=1;}
finally {
  await runtime.close();report.sqlite_bytes=(await stat(options.db)).size;
  await writeFile(path.join(dir,'report.json'),JSON.stringify(report,null,2)+'\n',{mode:0o600});
  console.log(JSON.stringify({report:path.join(dir,'report.json'),...report}));
}
