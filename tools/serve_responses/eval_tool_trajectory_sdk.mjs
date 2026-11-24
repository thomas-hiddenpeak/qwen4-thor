// Replay an actual Chat tool result and grade a newly sampled function call.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {setTimeout as sleep} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
const client = key => new OpenAI({baseURL: p.base, apiKey: key, maxRetries: 0, timeout: 45000});
const owner = client(p.key), peer = client(p.other);
const decision = JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-scope-decision-20261120/decision.json', import.meta.url)));
const expected = String(decision.counts.required_inference_or_application_service);
const wire = [], collection = '/evals/{eval_id}/runs', detail = collection + '/{run_id}';
async function observe(path, method, status, call) {
  const raw = await call.asResponse();
  assert.equal(raw.status, status);
  const value = await raw.clone().json(), parsed = await call;
  assert.deepEqual(parsed.data ?? parsed, value.data ?? value);
  wire.push({path, method, status, value});
  return parsed;
}
async function output(evaluation, body, remove) {
  const {id} = await observe(collection, 'post', 201, owner.evals.runs.create(evaluation, body));
  if (remove) await remove();
  await assert.rejects(peer.evals.runs.retrieve(id, {eval_id: evaluation}), err => err.status === 404);
  const expires = Date.now() + 70000;
  let run;
  do {
    run = await owner.evals.runs.retrieve(id, {eval_id: evaluation});
    if (!['queued','in_progress'].includes(run.status)) break;
    await sleep(150);
  } while (Date.now() < expires);
  assert.equal(run.status, 'completed', JSON.stringify(run.error));
  await observe(detail, 'get', 200, owner.evals.runs.retrieve(id, {eval_id: evaluation}));
  const list = await observe(detail + '/output_items', 'get', 200,
    owner.evals.runs.outputItems.list(id, {eval_id: evaluation}));
  assert.equal(list.data.length, 1);
  const row = list.data[0];
  assert.equal(row.status, 'pass', JSON.stringify(row.results));
  assert.equal(run.per_model_usage[0].invocation_count, 1);
  return row;
}
const fn = {name: 'read_scope_count', description: 'Read the approved project scope count.',
  strict: true, parameters: {type: 'object', properties: {}, required: [], additionalProperties: false}};
const tools = [{type: 'function', function: fn}];
const user = {role: 'user', content: 'Call read_scope_count. Report only the integer returned by the tool.'};
const opts = {model: p.model, temperature: 0, reasoning_effort: 'none', max_tokens: 96};
const proposed = await owner.chat.completions.create({...opts, tools, messages: [user],
  tool_choice: {type: 'function', function: {name: fn.name}}});
const call = proposed.choices[0].message.tool_calls[0];
assert.equal(call.function.name, fn.name);
assert.deepEqual(JSON.parse(call.function.arguments), {});
const metadata = {fixture: 'eval_real_tool_history'};
const stored = await owner.chat.completions.create({...opts, store: true, metadata, tools,
  tool_choice: 'none', messages: [user, {role:'assistant', tool_calls:[call], content:null},
                                  {role:'tool', tool_call_id:call.id, content:expected}]});
assert.equal(stored.choices[0].message.content.trim(), expected);
const equal = (name, input, reference) => ({type:'string_check',name,input,operation:'eq',reference});
const first = await owner.evals.create({data_source_config:{type:'logs',metadata},
  testing_criteria:[equal('Replay scope count','{{sample.output_text}}',expected)]});
const replay = await output(first.id, {data_source:{type:'completions',model:p.model,
  source:{type:'stored_completions',limit:1,metadata}, sampling_params:{temperature:0,
  reasoning_effort:'none',max_completion_tokens:64}}}, () => owner.chat.completions.delete(stored.id));
assert.equal(replay.sample.output[0].content.trim(), expected);
assert.ok(replay.datasource_item.input_trajectory.some(m => m.role === 'tool' &&
  m.tool_call_id === call.id && m.content === expected));
await owner.evals.delete(first.id);
const second = await owner.evals.create({data_source_config:{type:'custom',include_sample_schema:true,
  item_schema:{type:'object',properties:{prompt:{type:'string'}},required:['prompt']}},
  testing_criteria:[
    equal('Sampled tool name','{{sample.output_tools[0].function.name}}',fn.name),
    equal('Choice identifies the same call','{{sample.choices[0].message.tool_calls[0].id}}','{{sample.output_tools[0].id}}'),
    {name:'Empty JSON arguments',type:'python',pass_threshold:1,
     source:'import json\ndef grade(sample, item):\n    call = sample["output_tools"][0]\n    return int(call["type"] == "function" and json.loads(call["function"]["arguments"]) == {})'}
  ]});
const sampled = await output(second.id,{data_source:{type:'completions',model:p.model,
  input_messages:{type:'template',template:[{role:'user',content:'{{item.prompt}}'}]},
  source:{type:'file_content',content:[{item:{prompt:'Use the read_scope_count function now. The count is unavailable until you call it.'}}]},
  sampling_params:{tools,temperature:0,reasoning_effort:'none',max_completion_tokens:96}}});
assert.equal(sampled.sample.finish_reason,'tool_calls');
assert.deepEqual(sampled.results.map(r=>r.score),[1,1,1]);
await owner.evals.delete(second.id);
const check = await peer.chat.completions.create({...opts,messages:[{role:'user',content:'Return only 6 times 7.'}]});
const independent = check.choices[0].message.content.trim();assert.equal(independent,'42');
process.stdout.write(JSON.stringify({sdk:version,independent_chat:independent,history_answer:expected,
  history_call:call.id,sampled_finish:sampled.sample.finish_reason,grades:sampled.results.map(r=>r.score),wire}));
