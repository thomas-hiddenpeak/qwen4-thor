// Verify durable Eval runs with a real project record and a deliberately wrong prediction.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {setTimeout as sleep} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const owner = new OpenAI({baseURL: plan.base, apiKey: plan.key, maxRetries: 0});
const other = new OpenAI({baseURL: plan.base, apiKey: plan.other, maxRetries: 0});
const record = JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-scope-decision-20261120/decision.json', import.meta.url)));
const correct = record.counts.required_inference_or_application_service;
const excluded = record.counts.excluded_platform_management_by_user;
const wire = [];
async function observe(path, method, status, call) {
  const raw = await call.asResponse();
  assert.equal(raw.status, status);
  const value = await raw.clone().json();
  const sdkResult = await call;
  if (typeof sdkResult.getNextPage === 'function') assert.deepEqual(sdkResult.data, value.data);
  else assert.deepEqual(sdkResult, value);
  wire.push({path, method, status, value});
  return value;
}
let recovery;
if (plan.resume) {
  const found = (await owner.evals.list()).data.filter(row => row.name === 'Scope approval check');
  assert.equal(found.length, 1);
  const original = found[0].id;
  const candidates = (await owner.evals.runs.list(original)).data.filter(row => row.name === 'Qualified project scope');
  assert.equal(candidates.length, 1);
  const completed = candidates[0];
  assert.equal(completed.status, 'completed');
  const outputs = await owner.evals.runs.outputItems.list(completed.id, {eval_id: original});
  assert.deepEqual(outputs.data.map(item => item.results[0].score), [1, 0]);
  await assert.rejects(owner.files.retrieve(completed.data_source.source.id), err => err.status === 404);
  recovery = {evaluation: original, run: completed.id, outputs: outputs.data.map(item => item.id),
              prediction: outputs.data[0].sample.output[0].content};
  assert.equal(recovery.prediction, String(correct));
  await owner.evals.delete(original);
}
const evaluation = await owner.evals.create({
  name: 'Scope approval check', data_source_config: {type: 'custom', item_schema: {
    type: 'object', properties: {expected: {type: 'integer'}}, required: ['expected']}},
  testing_criteria: [{name: 'Exact count', type: 'string_check',
    input: '{{sample.output_text}}', operation: 'eq', reference: '{{item.expected}}'}]
});
const eval_id = evaluation.id;
const runs = owner.evals.runs;
const collection = '/evals/{eval_id}/runs', detail = collection+'/{run_id}';
const outputPath = detail+'/output_items';
let prediction = recovery?.prediction;
if (!recovery) {
  const read = await owner.chat.completions.create({model: plan.model, temperature: 0,
    max_tokens: 64, reasoning_effort: 'none', messages: [{role: 'user', content:
      'Read counts.required_inference_or_application_service in this JSON. Reply with only its integer value. '+JSON.stringify(record)}]});
  prediction = read.choices[0].message.content.trim();
}
assert.equal(prediction, String(correct));
const rows = [{item:{expected:correct}, sample:{output_text:prediction}},
              {item:{expected:excluded}, sample:{output_text:String(excluded+1)}}];
const file = await owner.files.create({purpose:'evals',
  file:new File([rows.map(row => JSON.stringify(row)).join('\r\n')], 'scope.jsonl',
                {type:'application/jsonl'})});
const created = await observe(collection,'post',201,runs.create(eval_id,
  {name:'Qualified project scope', data_source:{type:'jsonl', source:{type:'file_id',id:file.id}}}));
assert.equal(created.status,'queued');
assert.equal(created.error,null);
await owner.files.delete(file.id);
await assert.rejects(owner.files.retrieve(file.id), err => err.status===404);
await assert.rejects(other.evals.runs.retrieve(created.id,{eval_id}),err=>err.status===404);
const deadline = Date.now()+45000;
let terminal;
do {
  terminal = await runs.retrieve(created.id,{eval_id});
  if (!['queued','in_progress'].includes(terminal.status)) break;
  await sleep(125);
} while (Date.now()<deadline);
assert.equal(terminal.status,'completed',JSON.stringify(terminal.error));
assert.deepEqual(terminal.result_counts,{total:2,errored:0,failed:1,passed:1});
const received = await observe(detail,'get',200,runs.retrieve(created.id,{eval_id}));
assert.deepEqual(received,terminal);
const page = await observe(collection,'get',200,runs.list(eval_id,{status:'completed',limit:5}));
assert.ok(page.data.some(row=>row.id===created.id));
const first = await observe(outputPath,'get',200,runs.outputItems.list(created.id,{eval_id,limit:1}));
assert.equal(first.data.length,1);
assert.equal(first.has_more,true);
const firstItem = await observe(outputPath+'/{output_item_id}','get',200,
  runs.outputItems.retrieve(first.last_id,{eval_id,run_id:created.id}));
assert.equal(firstItem.status,'pass');
assert.equal(firstItem.sample.output[0].content,prediction);
const failed = await runs.outputItems.list(created.id,{eval_id,after:first.last_id,status:'fail'});
assert.equal(failed.data.length,1);
assert.equal(failed.data[0].datasource_item.expected,excluded);
assert.equal(failed.data[0].results[0].score,0);
const waiting = await runs.create(eval_id,{data_source:{type:'jsonl',
  source:{type:'file_content',content:Array.from({length:128},()=>rows[0])}}});
const canceled = await observe(detail+'/cancel','post',200,runs.cancel(waiting.id,{eval_id}));
assert.equal(canceled.status,'canceled');
await assert.rejects(runs.outputItems.retrieve(first.last_id,{eval_id,run_id:waiting.id}),e=>e.status===404);
const removed = await observe(detail,'delete',200,runs.delete(created.id,{eval_id}));
assert.equal(removed.run_id,created.id);
await assert.rejects(runs.retrieve(created.id,{eval_id}),e=>e.status===404);
await owner.evals.delete(eval_id);
await assert.rejects(runs.retrieve(waiting.id,{eval_id}),e=>e.status===404);
const check = await other.chat.completions.create({model:plan.model,temperature:0,
  max_tokens:32,reasoning_effort:'none',messages:[{role:'user',content:'Return only the answer to 6 times 7.'}]});
const independent=check.choices[0].message.content.trim();
assert.equal(independent,'42');
process.stdout.write(JSON.stringify({sdk:version,model_answer:prediction,independent_chat:independent,recovery,
  scores:[firstItem.results[0].score,failed.data[0].results[0].score],counts:terminal.result_counts,
  foreign_project:404,foreign_run:404,deleted_file:404,deleted_run:404,canceled:canceled.status,wire}));
