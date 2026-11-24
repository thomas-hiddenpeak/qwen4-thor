// Validate model-produced Evals samples through the official SDK.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {setTimeout as sleep} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const client = key => new OpenAI({baseURL: plan.base, apiKey: key, maxRetries: 0});
const owner = client(plan.key), peer = client(plan.other);
const decision = JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-scope-decision-20261120/decision.json', import.meta.url)));
const expected = decision.counts.required_inference_or_application_service;
const prompt = 'Read counts.required_inference_or_application_service in this JSON. ' +
  'Reply with only its integer value. ' + JSON.stringify(decision);
const {id: eval_id} = await owner.evals.create({
  name: 'Generated project scope predictions',
  data_source_config: {type: 'custom', item_schema: {type: 'object', properties: {
    prompt: {type: 'string'}, reference: {type: 'integer'}, trajectory: {type: 'array',
      items: {type: 'object', properties: {role: {type: 'string'}, content: {type: 'string'}},
        required: ['role', 'content']}}}, required: ['prompt', 'reference', 'trajectory']}},
  testing_criteria: [{name: 'Exact count', type: 'string_check',
    input: '{{sample.output_text}}', operation: 'eq', reference: '{{item.reference}}'}]});
const wire = [];
const reports = [];
async function observe(path, method, status, call) {
  const raw = await call.asResponse();
  assert.equal(raw.status, status);
  const value = await raw.clone().json();
  const parsed = await call;
  if (typeof parsed.getNextPage === 'function') assert.deepEqual(parsed.data, value.data);
  else assert.deepEqual(parsed, value);
  wire.push({path, method, status, value});
  return parsed;
}
const collection = '/evals/{eval_id}/runs', detail = collection + '/{run_id}';
for (const [index, type] of ['completions', 'responses'].entries()) {
  const sample = {max_completion_tokens: 64, temperature: 0, reasoning_effort: 'none'};
  if (!index) sample.seed = 7102;
  const item = {prompt, reference: expected + index, trajectory: [{role: 'user', content: prompt}]};
  const source = {type, model: plan.model, source: {type: 'file_content', content: [{item}]},
    sampling_params: sample, input_messages: index ?
      {type: 'item_reference', item_reference: 'item.trajectory'} :
      {type: 'template', template: [{role: 'user', content: '{{item.prompt}}'}]}};
  const created = await observe(collection, 'post', 201, owner.evals.runs.create(eval_id, {data_source: source}));
  await assert.rejects(peer.evals.runs.retrieve(created.id, {eval_id}), err => err.status === 404);
  let run;
  const until = Date.now() + 45000;
  do {
    run = await owner.evals.runs.retrieve(created.id, {eval_id});
    if (!['in_progress', 'queued'].includes(run.status)) break;
    await sleep(120);
  } while (Date.now() < until);
  assert.equal(run.status, 'completed', JSON.stringify(run.error));
  run = await observe(detail, 'get', 200, owner.evals.runs.retrieve(run.id, {eval_id}));
  assert.deepEqual(run.result_counts, {total: 1, passed: 1-index, failed: index, errored: 0});
  const page = await observe(detail+'/output_items','get',200,owner.evals.runs.outputItems.list(run.id,{eval_id}));
  assert.equal(page.data.length, 1);
  const output = page.data[0];
  assert.equal(output.results[0].score, 1-index);
  assert.equal(output.sample.output[0].content, String(expected));
  assert.equal(output.sample.input[0].content, prompt);
  assert.equal(output.sample.max_completion_tokens, 64);
  assert.equal(output.sample.temperature, 0);
  assert.ok(Number.isSafeInteger(output.sample.seed));
  if (!index) assert.equal(output.sample.seed, 7102);
  assert.equal(output.sample.error, null);
  const usage = run.per_model_usage[0];
  assert.equal(usage.model_name, plan.model);
  assert.equal(usage.invocation_count, 1);
  assert.ok(usage.completion_tokens > 0 && usage.prompt_tokens > 0);
  assert.equal(usage.total_tokens, output.sample.usage.total_tokens);
  reports.push({type, prediction: output.sample.output[0].content, score: output.results[0].score,
    seed: output.sample.seed, usage, original_id: run.id});
}
const {id: waiting} = await owner.evals.runs.create(eval_id, {data_source: {type:'responses',
  model:plan.model, input_messages:{type:'template',template:[{role:'user',content:'{{item.prompt}}'}]},
  source:{type:'file_content',content:Array.from({length:12},()=>({item:{prompt, reference:expected, trajectory:[]}}))}}});
const canceled = await observe(detail+'/cancel','post',200,owner.evals.runs.cancel(waiting,{eval_id}));
assert.equal(canceled.status,'canceled');
await owner.evals.delete(eval_id);
await assert.rejects(owner.evals.runs.retrieve(waiting,{eval_id}), err => err.status === 404);
const check = await peer.chat.completions.create({model:plan.model,temperature:0,max_tokens:32,
  reasoning_effort:'none',messages:[{role:'user',content:'Return only the answer to 6 times 7.'}]});
const independent = check.choices[0].message.content.trim();
assert.equal(independent, '42');
process.stdout.write(JSON.stringify({sdk:version,reports,independent_chat:independent,
  canceled:canceled.status,foreign_project:404,deleted:404,wire}));
