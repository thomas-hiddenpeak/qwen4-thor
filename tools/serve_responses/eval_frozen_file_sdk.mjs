// Evaluate the original file-backed model input after both public sources disappear.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {setTimeout as sleep} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const piece of process.stdin) input += piece;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
const makeClient = key => new OpenAI({baseURL: p.base, apiKey: key, maxRetries: 0, timeout: 45000});
const owner = makeClient(p.key), peer = makeClient(p.other);
const bytes = readFileSync(new URL('../../docs/evidence/serve-scope-decision-20261120/decision.json', import.meta.url));
const answer = String(JSON.parse(bytes).counts.required_inference_or_application_service);
const file = await owner.files.create({file: new File([bytes], 'scope-decision.txt', {type: 'text/plain'}), purpose: 'user_data'});
const metadata = {project: 'q4t', fixture: 'frozen_file_evaluation'};
const first = await owner.responses.create({model: p.model, store: true, metadata, temperature: 0,
  max_output_tokens: 48, reasoning: {effort: 'none'}, input: [{role: 'user', content: [
    {type: 'input_file', file_id: file.id},
    {type: 'input_text', text: 'Read the document. Return only the integer in counts.required_inference_or_application_service.'}
  ]}]});
assert.equal(first.output_text.trim(), answer);
await owner.files.delete(file.id);
await assert.rejects(owner.files.retrieve(file.id), e => e.status === 404);
const ev = await owner.evals.create({data_source_config: {type: 'logs', metadata},
  testing_criteria: [{name: 'Original attached scope', type: 'string_check',
    input: '{{sample.output_text}}', reference: answer, operation: 'eq'}]});
const wire = [];
async function observe(path, method, status, call) {
  const response = await call.asResponse();
  assert.equal(response.status, status);
  const value = await response.clone().json();
  const parsed = await call;
  assert.deepEqual(value.data ?? value, parsed.data ?? parsed);
  wire.push({path, method, status, value});
  return parsed;
}
const collection = '/evals/{eval_id}/runs', detail = collection + '/{run_id}';
const source = {type: 'responses', model: p.model, metadata};
const run = await observe(collection, 'post', 201, owner.evals.runs.create(ev.id, {
  data_source: {type: 'responses', model: p.model, source, sampling_params:
    {temperature: 0, reasoning_effort: 'none', max_completion_tokens: 48}}}));
await owner.responses.delete(first.id);
await assert.rejects(owner.responses.retrieve(first.id), e => e.status === 404);
await assert.rejects(peer.evals.runs.retrieve(run.id, {eval_id: ev.id}), e => e.status === 404);
const end = Date.now() + 60000;
let state;
do {
  state = await owner.evals.runs.retrieve(run.id, {eval_id: ev.id});
  if (!['queued', 'in_progress'].includes(state.status)) break;
  await sleep(125);
} while (Date.now() < end);
assert.equal(state.status, 'completed', JSON.stringify(state.error));
await observe(detail, 'get', 200, owner.evals.runs.retrieve(run.id, {eval_id: ev.id}));
const results = await observe(detail + '/output_items', 'get', 200,
  owner.evals.runs.outputItems.list(run.id, {eval_id: ev.id}));
assert.equal(results.data.length, 1);
const result = results.data[0];
assert.equal(result.sample.output[0].content.trim(), answer);
assert.equal(result.status, 'pass');
assert.equal(result.results[0].score, 1);
assert.equal(state.per_model_usage[0].invocation_count, 1);
assert.equal(result.datasource_item.id, first.id);
assert.ok(result.datasource_item.input_trajectory.some(m =>
  Array.isArray(m.content) && m.content.some(part => part.file_id === file.id)));
assert.ok(!JSON.stringify(result).includes('frozen_response_parts'));
const independent = (await peer.chat.completions.create({model: p.model, temperature: 0,
  max_tokens: 32, reasoning_effort: 'none', messages: [{role: 'user', content: 'Return only 6 times 7.'}]}))
  .choices[0].message.content.trim();
assert.equal(independent, '42');
process.stdout.write(JSON.stringify({sdk: version, independent_chat: independent, history_answer: answer,
  replay_answer: result.sample.output[0].content.trim(), score: result.results[0].score,
  sha256: createHash('sha256').update(bytes).digest('hex'), source_file: file.id,
  response: first.id, evaluation: ev.id, run: run.id, output: result.id, source_deleted_before_selection: 404, response_deleted: 404,
  foreign_project: 404, wire}));
