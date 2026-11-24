// Exercise both grading models on correct and incorrect project decisions.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {setTimeout as sleep} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const p = JSON.parse(input);
const {OpenAI,version}=loadSdk(p.sdk);
const options={baseURL:p.base,maxRetries:0,timeout:45000};
const owner=new OpenAI({...options,apiKey:p.key}),peer=new OpenAI({...options,apiKey:p.other});
const decision=JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-scope-decision-20261120/decision.json',import.meta.url)));
const expected=String(decision.counts.required_inference_or_application_service);
const shared='The signed project scope count is {{item.count}}. The reported count is {{sample.output_text}}. ';
const wire=[];
async function observe(path,method,status,call) {
  const raw=await call.asResponse(),value=await raw.clone().json(),parsed=await call;
  assert.equal(raw.status,status);
  if(typeof parsed.getNextPage==='function')assert.deepEqual(parsed.data,value.data);
  else assert.deepEqual(parsed,value);
  wire.push({path,method,status,value});return parsed;
}
const {id:eval_id}=await observe('/evals','post',201,owner.evals.create({
  name:'Verify two submitted scope counts',data_source_config:{type:'custom',
    item_schema:{type:'object',properties:{count:{type:'string'}},required:['count']}},
  testing_criteria:[
    {type:'label_model',name:'Exact approval',model:p.model,labels:['correct','incorrect'],
     passing_labels:['correct'],input:[{role:'user',content:shared+'Choose correct if the two count values are exactly equal. Otherwise choose incorrect.'}]},
    {type:'score_model',name:'Business score',model:p.model,range:[0,1],pass_threshold:0.5,
     sampling_params:{temperature:0,seed:17,reasoning_effort:'none',max_completions_tokens:160},
     input:[{role:'user',content:shared+'Use result 0.75 when the two counts are equal, 0.25 when they differ. Use an empty steps list.'}]}]}));
const collection='/evals/{eval_id}/runs',detail=collection+'/{run_id}';
const predictions=[expected,String(Number(expected)+1)];
const {id}=await observe(collection,'post',201,owner.evals.runs.create(eval_id,
  {data_source:{type:'jsonl',source:{type:'file_content',content:predictions.map(value=>({
    item:{count:expected},sample:{output_text:value}}))}}}));
await assert.rejects(peer.evals.runs.retrieve(id,{eval_id}),e=>e.status===404);
const until=Date.now()+110000;let run;
do{
  run=await owner.evals.runs.retrieve(id,{eval_id});
  if(!['queued','in_progress'].includes(run.status))break;
  await sleep(175);
}while(Date.now()<until);
assert.equal(run.status,'completed',JSON.stringify(run.error));
await observe(detail,'get',200,owner.evals.runs.retrieve(id,{eval_id}));
const page=await observe(detail+'/output_items','get',200,owner.evals.runs.outputItems.list(id,{eval_id}));
const report=page.data.map(row=>({prediction:row.sample.output[0].content,
  status:row.status,grades:row.results.map(r=>({score:r.score,passed:r.passed,sample:r.sample,error:r.error}))}));
process.stderr.write(JSON.stringify({counts:run.result_counts,usage:run.per_model_usage,report})+'\n');
assert.deepEqual(run.result_counts,{total:2,passed:1,failed:1,errored:0});
assert.deepEqual(report.map(r=>r.prediction),predictions);
assert.deepEqual(report.map(r=>r.grades[0].sample.label),['correct','incorrect']);
assert.deepEqual(report.map(r=>r.grades[1].score),[0.75,0.25]);
assert.deepEqual(report.map(r=>r.grades.map(g=>g.passed)),[[true,true],[false,false]]);
const usage=run.per_model_usage[0];
assert.equal(usage.invocation_count,4);assert.ok(usage.prompt_tokens>0 && usage.completion_tokens>0);
await owner.evals.delete(eval_id);
const check=await peer.chat.completions.create({model:p.model,temperature:0,reasoning_effort:'none',
   max_tokens:32,messages:[{role:'user',content:'Return only 6 times 7.'}]});
const independent=check.choices[0].message.content.trim();assert.equal(independent,'42');
process.stdout.write(JSON.stringify({sdk:version,report,independent_chat:independent,usage,wire}));
