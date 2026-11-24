// Generate two real project log records, delete them after selection, and
// verify offline ownership, model replay and all three signed Evals terminals.
import assert from 'node:assert/strict';
import {once} from 'node:events';
import {readFileSync} from 'node:fs';
import https from 'node:https';
import {setTimeout as sleep} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
const options = {baseURL:p.base,maxRetries:0,timeout:30000};
const owner = new OpenAI({...options,apiKey:p.key}), peer = new OpenAI({...options,apiKey:p.other});
const decision = JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-scope-decision-20261120/decision.json', import.meta.url)));
const prompt = 'Return only counts.required_inference_or_application_service from this JSON. ' +
               JSON.stringify(decision);
const answer = String(decision.counts.required_inference_or_application_service);
let secret;
const notices = [], wire = [];
const server = https.createServer({key:readFileSync(p.tlsKey),cert:readFileSync(p.tlsCert)},async(req,res)=>{
  try {
    assert.equal(req.url,'/eval-status');
    let raw = '';
    for await (const piece of req) raw += piece;
    const event = await owner.webhooks.unwrap(raw,new Headers(req.headers),secret);
    assert.deepEqual(Object.keys(event.data),['id']);
    notices.push({event:event.id,type:event.type,run:event.data.id});
    res.writeHead(202).end();
  } catch (err) {
    process.stderr.write('Subscriber verification failed: '+err.name+'\n');
    process.exitCode=1;
    res.writeHead(400).end();
  }
});
async function until(fn) {
  const end = Date.now()+30000;
  do { const value = await fn(); if (value) return value; await sleep(150); } while (Date.now()<end);
  throw Error('Deadline waiting for '+fn.name);
}
async function observe(path,method,status,call) {
  const raw = await call.asResponse();
  assert.equal(raw.status,status);
  const value = await raw.clone().json(), parsed = await call;
  if (typeof parsed.getNextPage==='function') assert.deepEqual(parsed.data,value.data);
  else assert.deepEqual(parsed,value);
  wire.push({path,method,status,value});
  return parsed;
}
try {
  server.listen(p.subscriberPort,'127.0.0.1');
  await once(server,'listening');
  const subscription=await owner.webhooks.create({name:'Scope evaluation events',
    url:'https://127.0.0.1:'+p.subscriberPort+'/eval-status',
    event_types:['eval.run.succeeded','eval.run.failed','eval.run.canceled','response.completed']});
  secret=subscription.signing_secret;
  const project={project:'scope-decision'};
  const other=await peer.chat.completions.create({model:p.model,store:true,temperature:0,
    reasoning_effort:'none',max_tokens:32,metadata:project,messages:[{role:'user',content:'Return only 6 times 7.'}]});
  const independent=other.choices[0].message.content.trim(); assert.equal(independent,'42');
  const chat=await owner.chat.completions.create({model:p.model,store:true,temperature:0,
    reasoning_effort:'none',max_tokens:48,metadata:project,messages:[{role:'user',content:prompt}]});
  const response=await owner.responses.create({model:p.model,store:true,temperature:0,
    reasoning:{effort:'none'},max_output_tokens:48,metadata:project,instructions:'Scope test',input:prompt});
  assert.equal(chat.choices[0].message.content.trim(),answer);
  assert.equal(response.output_text.trim(),answer);
  const {id:eval_id}=await owner.evals.create({data_source_config:{type:'logs',metadata:project},
    testing_criteria:[{name:'Original prediction',type:'string_check',input:'{{sample.output_text}}',
                       operation:'eq',reference:'{{item.output_text}}'}]});
  const collection='/evals/{eval_id}/runs',detail=collection+'/{run_id}';
  const reports=[];
  for (const [type,source,record,release] of [
    ['completions',{type:'stored_completions',metadata:project,model:p.model,limit:4},chat,
      ()=>owner.chat.completions.delete(chat.id)],
    ['responses',{type:'responses',metadata:project,instructions_search:'Scope test',
       reasoning_effort:'none',temperature:0,model:p.model},response,()=>owner.responses.delete(response.id)]]) {
    const created=await observe(collection,'post',201,owner.evals.runs.create(eval_id,
      {data_source:{type,model:p.model,source,input_messages:{type:'item_reference',item_reference:'item.input_trajectory'},
                    sampling_params:{reasoning_effort:'none',temperature:0,max_completion_tokens:48,seed:98}}}));
    await release();
    const retrieve=()=>type==='responses'?owner.responses.retrieve(record.id):owner.chat.completions.retrieve(record.id);
    await assert.rejects(retrieve,e=>e.status===404);
    const run=await until(async function completed() {
      const value=await owner.evals.runs.retrieve(created.id,{eval_id});
      return !['queued','in_progress'].includes(value.status)&&value;
    });
    assert.equal(run.status,'completed',JSON.stringify(run.error));
    assert.deepEqual(run.result_counts,{total:1,passed:1,failed:0,errored:0});
    const out=await observe(detail+'/output_items','get',200,owner.evals.runs.outputItems.list(run.id,{eval_id}));
    assert.equal(out.data.length,1);
    assert.equal(out.data[0].datasource_item.id,record.id);
    assert.equal(out.data[0].datasource_item.output_text,answer);
    assert.equal(out.data[0].sample.output[0].content,answer);
    assert.equal(run.per_model_usage[0].invocation_count,1);
    reports.push({type,source:record.id,run:run.id,answer,score:out.data[0].results[0].score});
  }
  const bad=await owner.evals.runs.create(eval_id,{data_source:{type:'responses',model:p.model,
    source:{type:'file_content',content:[{item:{}}]},input_messages:{type:'item_reference',item_reference:'item.missing'}}});
  const invalid=await until(async function failure() {
    const value=await owner.evals.runs.retrieve(bad.id,{eval_id});
    return value.status==='failed' && value;
  });
  assert.ok(invalid.error.code);
  const pending=await owner.evals.runs.create(eval_id,{data_source:{type:'jsonl',source:{type:'file_content',
    content:Array.from({length:96},()=>({item:{output_text:answer},sample:{output_text:answer}}))}}});
  await observe(detail+'/cancel','post',200,owner.evals.runs.cancel(pending.id,{eval_id}));
  const expected=new Map([...reports.map(row=>[row.run,'eval.run.succeeded']),[bad.id,'eval.run.failed'],[pending.id,'eval.run.canceled']]);
  await until(function deliveries(){return notices.length>=expected.size;});
  assert.equal(notices.length,expected.size);
  assert.equal(new Set(notices.map(event=>event.run)).size,expected.size);
  for(const notice of notices)assert.equal(notice.type,expected.get(notice.run));
  await owner.evals.delete(eval_id);
  await owner.webhooks.delete(subscription.id);
  await peer.chat.completions.delete(other.id);
  process.stdout.write(JSON.stringify({sdk:version,reports,independent_chat:independent,notices,wire}));
} finally {
  await new Promise(resolve=>server.close(resolve));
}
