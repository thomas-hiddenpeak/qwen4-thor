// Compare partial credit, Python values, and a faulty grader on one model answer.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {setTimeout as sleep} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input='';
for await (const chunk of process.stdin) input+=chunk;
const p=JSON.parse(input);
const {OpenAI,version}=loadSdk(p.sdk);
const options={baseURL:p.base,maxRetries:0,timeout:30000};
const owner=new OpenAI({...options,apiKey:p.key}), peer=new OpenAI({...options,apiKey:p.other});
const decision=JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-scope-decision-20261120/decision.json', import.meta.url)));
const answer=String(decision.counts.required_inference_or_application_service);
const prompt='Return only counts.required_inference_or_application_service from this JSON. '+JSON.stringify(decision);
const wire=[];
async function observe(path,method,status,call) {
  const raw=await call.asResponse();
  const value=await raw.clone().json(), parsed=await call;
  assert.equal(raw.status,status);
  if(typeof parsed.getNextPage==='function')assert.deepEqual(parsed.data,value.data);
  else assert.deepEqual(parsed,value);
  wire.push({path,method,status,value});
  return parsed;
}
const def=await observe('/evals','post',201,owner.evals.create({
  name:'Scope report with partial credit', data_source_config:{type:'custom',item_schema:{
    type:'object',properties:{prompt:{type:'string'},answer:{type:'string'}},required:['prompt','answer']}},
  testing_criteria:[
    {name:'One extra character',type:'text_similarity',evaluation_metric:'fuzzy_match',
     input:'{{sample.output_text}}',reference:'{{item.answer}}0',pass_threshold:0.9},
    {name:'Scope report overlap',type:'text_similarity',evaluation_metric:'rouge_1',
     input:'Scope {{sample.output_text}}',reference:'scope {{item.answer}}',pass_threshold:1},
    {name:'Business partial credit',type:'python',pass_threshold:0.7,
     source:"def grade(sample,item):\n print('diagnostic output')\n return 0.75 if sample['output_text']==item['answer'] else 0.0"},
    {name:'Invalid grade',type:'python',pass_threshold:0,source:"def grade(sample,item):\n return float('nan')"}]}));
const eval_id=def.id, collection='/evals/{eval_id}/runs', detail=collection+'/{run_id}';
const {id}=await observe(collection,'post',201,owner.evals.runs.create(eval_id,{data_source:{
  type:'responses',model:p.model,source:{type:'file_content',content:[{item:{prompt,answer}}]},
  input_messages:{type:'item_reference',item_reference:'item.prompt'},
  sampling_params:{temperature:0,reasoning_effort:'none',max_completion_tokens:48}}}));
await assert.rejects(peer.evals.runs.retrieve(id,{eval_id}),err=>err.status===404);
let run;
const until=Date.now()+45000;
do {
  run=await owner.evals.runs.retrieve(id,{eval_id});
  if (!['queued','in_progress'].includes(run.status)) break;
  await sleep(120);
} while(Date.now()<until);
assert.equal(run.status,'completed',JSON.stringify(run.error));
assert.deepEqual(run.result_counts,{total:1,passed:0,failed:1,errored:0});
await observe(detail,'get',200,owner.evals.runs.retrieve(id,{eval_id}));
const page=await observe(detail+'/output_items','get',200,owner.evals.runs.outputItems.list(id,{eval_id}));
assert.equal(page.data.length,1);
const row=page.data[0];assert.equal(row.sample.output[0].content,answer);
assert.equal(row.status,'fail');
const results=row.results;
assert.deepEqual(results.map(r=>r.passed),[false,true,true,false]);
assert.ok(Math.abs(results[0].score-6/7)<1e-12);
assert.deepEqual(results.slice(1).map(r=>r.score),[1,0.75,0]);
assert.equal(results[3].error.code,'invalid_python_grade');
assert.equal(run.per_model_usage[0].invocation_count,1);
assert.deepEqual(run.per_testing_criteria_results.map(r=>r.passed),[0,1,1,0]);
await owner.evals.delete(eval_id);
const check=await peer.chat.completions.create({model:p.model,temperature:0,reasoning_effort:'none',
  max_tokens:32,messages:[{role:'user',content:'Return only 6 times 7.'}]});
const independent=check.choices[0].message.content.trim();assert.equal(independent,'42');
process.stdout.write(JSON.stringify({sdk:version,model_answer:answer,independent_chat:independent,
  scores:results.map(r=>r.score),invalid:results[3].error.code,usage:run.per_model_usage,wire}));
