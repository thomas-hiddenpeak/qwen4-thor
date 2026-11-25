// Grade a real project result and recover the same scored row by its public IDs.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {setTimeout as sleep} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let raw = '';
for await (const piece of process.stdin) raw += piece;
const options = JSON.parse(raw);
const {OpenAI, version} = loadSdk(options.sdk);
const client = apiKey => new OpenAI({baseURL: options.base, apiKey, timeout: 45000, maxRetries: 0});
const owner = client(options.key), peer = client(options.other);
const scope = JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-scope-decision-20261120/decision.json', import.meta.url)));
const answer = String(scope.counts.required_inference_or_application_service);
const capabilities = await owner.get('/capabilities');
assert.ok(capabilities.evals.text_similarity.includes('meteor'));
const wire = [];
async function observe(path, method, request) {
  const raw = await request.asResponse(), value = await raw.clone().json();
  wire.push({path, method, status: raw.status, value});
  return await request;
}
const collection = '/evals/{eval_id}/runs', detail = collection+'/{run_id}';
let eval_id = options.previous?.eval_id, run_id = options.previous?.run_id;
if (!options.previous) {
  const criterion = (name, prefix, reference, pass_threshold) => ({
    name, type: 'text_similarity', evaluation_metric: 'meteor', pass_threshold,
    input: prefix+' {{sample.output_text}}', reference});
  const definition = await observe('/evals','post',owner.evals.create({
    name: 'Project scope lexical report',
    data_source_config: {type:'custom',item_schema:{type:'object',required:['prompt','answer'],
      properties:{prompt:{type:'string'},answer:{type:'string'}}}},
    testing_criteria: [
      criterion('Equivalent scope description','project plan','project design {{item.answer}}',0.98),
      criterion('Plural project paths','project paths','project path {{item.answer}}',0.98),
      criterion('Different result','project plan','unrelated orange zero',0.5),
      criterion('Word order','project paths','{{item.answer}} project paths',0.9)]}));
  eval_id = definition.id;
  const prompt = 'Return only counts.required_inference_or_application_service from this JSON. '+JSON.stringify(scope);
  const submitted = await observe(collection,'post',owner.evals.runs.create(eval_id,{data_source:{
    type:'responses',model:options.model,source:{type:'file_content',content:[{item:{prompt,answer}}]},
    input_messages:{type:'item_reference',item_reference:'item.prompt'},
    sampling_params:{temperature:0,reasoning_effort:'none',max_completion_tokens:48}}}));
  run_id = submitted.id;
}
await assert.rejects(peer.evals.runs.retrieve(run_id,{eval_id}),err=>err.status===404);
let run;
const deadline = Date.now()+45000;
do {
  run = await owner.evals.runs.retrieve(run_id,{eval_id});
  if (!['queued','in_progress'].includes(run.status)) break;
  await sleep(120);
} while (Date.now()<deadline);
assert.equal(run.status,'completed',JSON.stringify(run.error));
assert.deepEqual(run.result_counts,{total:1,passed:0,failed:1,errored:0});
await observe(detail,'get',owner.evals.runs.retrieve(run_id,{eval_id}));
const page = await observe(detail+'/output_items','get',owner.evals.runs.outputItems.list(run_id,{eval_id}));
assert.equal(page.data.length,1);
const row = page.data[0];
assert.equal(row.sample.output[0].content.trim(),answer);
const scores = row.results.map(item=>item.score);
const oracle = [0.9814814814814815,0.9814814814814815,0,0.8518518518518519];
assert.ok(scores.every((value,i)=>Math.abs(value-oracle[i])<1e-12));
assert.deepEqual(row.results.map(item=>item.passed),[true,true,false,false]);
assert.equal(run.per_model_usage[0].invocation_count,1);
const result = {sdk:version,eval_id,run_id,output:row.id,model_answer:answer,scores,foreign_status:404};
if (options.previous) {
  for (const [key,value] of Object.entries(result)) assert.deepEqual(value,options.previous[key]);
}
if (options.phase!=='primary') {
  const independent = await peer.chat.completions.create({model:options.model,temperature:0,
    reasoning_effort:'none',max_tokens:32,messages:[{role:'user',content:'Return only six times seven.'}]});
  result.independent_chat = independent.choices[0].message.content.trim();
  assert.equal(result.independent_chat,'42');
}
process.stdout.write(JSON.stringify({...result,wire}));
