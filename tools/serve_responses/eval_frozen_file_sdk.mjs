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
const chatHistory = p.history === 'chat_pdf';
const scope = readFileSync(new URL('../../docs/evidence/serve-scope-decision-20261120/decision.json', import.meta.url));
const answer = String(JSON.parse(scope).counts.required_inference_or_application_service);
let bytes = scope;
if (chatHistory) {
  const template = readFileSync(new URL('../../tests/fixtures/file_nested/page.pdf', import.meta.url));
  // Change only the text of the qualified one-page fixture, preserving the
  // byte offsets of its PDF objects and the stream's declared length.
  const original = 'Embedded actual PDF 42';
  const label = 'Scope total ' + answer;
  const marker = template.indexOf(original);
  assert.ok(marker >= 0 && template.indexOf(original, marker + 1) < 0);
  assert.ok(label.length <= original.length);
  bytes = Buffer.from(template);
  bytes.write(label.padEnd(original.length), marker, 'ascii');
}
const filename = chatHistory ? 'project-scope.pdf' : 'scope-decision.txt';
const mime = chatHistory ? 'application/pdf' : 'text/plain';
const file = await owner.files.create({file: new File([bytes], filename, {type: mime}), purpose: 'user_data'});
const metadata = {project: 'q4t', fixture: chatHistory ? 'frozen_chat_pdf' : 'frozen_file_evaluation'};
const question = chatHistory ? 'Read the attached project record. Return only the integer labelled Scope total.' :
  'Read the document. Return only the integer in counts.required_inference_or_application_service.';
let first;
if (chatHistory) {
  first = await owner.chat.completions.create({model: p.model, store: true, metadata, temperature: 0,
    max_completion_tokens: 48, reasoning_effort: 'none', messages: [{role: 'user', content: [
      {type: 'file', file: {file_id: file.id}}, {type: 'text', text: question}]}]});
  assert.equal(first.choices[0].message.content.trim(), answer);
} else {
  first = await owner.responses.create({model: p.model, store: true, metadata, temperature: 0,
    max_output_tokens: 48, reasoning: {effort: 'none'}, input: [{role: 'user', content: [
      {type: 'input_file', file_id: file.id}, {type: 'input_text', text: question}]}]});
  assert.equal(first.output_text.trim(), answer);
}
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
const source = {type: chatHistory ? 'stored_completions' : 'responses', model: p.model, metadata};
const run = await observe(collection, 'post', 201, owner.evals.runs.create(ev.id, {
  data_source: {type: chatHistory ? 'completions' : 'responses', model: p.model, source, sampling_params:
    {temperature: 0, reasoning_effort: 'none', max_completion_tokens: 48}}}));
const history = chatHistory ? owner.chat.completions : owner.responses;
await history.delete(first.id);
await assert.rejects(history.retrieve(first.id), e => e.status === 404);
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
  Array.isArray(m.content) && m.content.some(part => (chatHistory ? part.file?.file_id : part.file_id) === file.id)));
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
