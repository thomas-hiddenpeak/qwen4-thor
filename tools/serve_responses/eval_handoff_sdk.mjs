// Continue the same model-sampled evaluation after replacing the serving process.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {setTimeout as sleep} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let stdin = '';
for await (const chunk of process.stdin) stdin += chunk;
const options = JSON.parse(stdin);
const {OpenAI, version} = loadSdk(options.sdk);
const client = key => new OpenAI({baseURL: options.base, apiKey: key, maxRetries: 0,
                                  timeout: 30000});
const owner = client(options.key), peer = client(options.other);
const raw = readFileSync(new URL(
  '../../docs/evidence/serve-scope-decision-20261120/decision.json', import.meta.url));
const digest = createHash('sha256').update(raw).digest('hex');
const decision = JSON.parse(raw);
const count = decision.counts.required_inference_or_application_service;
const prompt = 'Read counts.required_inference_or_application_service from the project record. ' +
  'Return only its integer value. ' + JSON.stringify(decision);
const rows = 6, model = 'qwen3.8-flash-next';
const runs = owner.evals.runs;
const embeddingModel = 'qwen3-embedding-0.6b';
const references = [String(count), 'Overlapping speech requires a diarization model to identify who spoke.'];
let cosine;
if (options.cosine) {
  const baseline = [], tokens = [];
  for (const reference of references) {
    const result = await owner.embeddings.create({model: embeddingModel,
      encoding_format: 'float', input: [String(count), reference]});
    const [a,b] = result.data.map(row => row.embedding);
    assert.equal(result.model, embeddingModel);
    assert.ok(a.length === 1024 && b.length === 1024);
    const dot = a.reduce((sum,x,i) => sum + x*b[i], 0);
    const score = a.every((x,i) => x === b[i]) ? 1 : dot/(Math.hypot(...a)*Math.hypot(...b));
    baseline.push(Math.min(1, Math.max(0, score)));
    tokens.push(result.usage.prompt_tokens);
  }
  assert.ok(baseline[0] === 1 && baseline[1] < 0.95);
  cosine = {model: embeddingModel, baseline, tokens};
}
let {evaluation, run, file} = options.previous ?? {};
if (options.phase === 'primary') {
  const definition = await owner.evals.create({
    name: 'Recover project scope evaluation',
    data_source_config: {type: 'custom', item_schema: {type: 'object',
      properties: {index: {type: 'integer'}, prompt: {type: 'string'}, expected: {type: 'integer'},
        ...(cosine ? {reference: {type: 'string'}} : {})},
      required: ['index','prompt','expected']}},
    testing_criteria: [{name: 'Exact count', type: 'string_check', input: '{{sample.output_text}}',
                        reference: '{{item.expected}}', operation: 'eq'},
      ...(cosine ? [{name: 'Semantic score', type: 'text_similarity', evaluation_metric: 'cosine',
        input: '{{sample.output_text}}', reference: '{{item.reference}}', pass_threshold: 0.95}] : [])]});
  evaluation = definition.id;
  const data = Array.from({length: rows}, (_, index) =>
    JSON.stringify({item: {index, prompt, expected: count + index % 2,
      ...(cosine ? {reference: references[index % 2]} : {})}})).join('\n');
  file = (await owner.files.create({purpose: 'evals',
    file: new File([data], 'scope.jsonl', {type: 'application/jsonl'})})).id;
  const created = await runs.create(evaluation, {name: 'Preserve scope rows', data_source: {
    type: 'completions', model, source: {type: 'file_id', id: file},
    input_messages: {type: 'template', template: [{role: 'user', content: '{{item.prompt}}'}]},
    sampling_params: {reasoning_effort: 'none', temperature: 0, max_completion_tokens: 32}}});
  run = created.id;
  await owner.files.delete(file);
} else {
  assert.equal(options.phase, 'light');
  assert.equal(digest, options.previous.source_sha256);
}
await assert.rejects(owner.files.retrieve(file), err => err.status === 404);
await assert.rejects(peer.evals.runs.retrieve(run, {eval_id: evaluation}), err => err.status === 404);
const expires = Date.now() + 75000;
let state;
do {
  state = await runs.retrieve(run, {eval_id: evaluation});
  if (!['queued','in_progress'].includes(state.status)) break;
  if (options.phase === 'primary' && state.result_counts.total > 0) break;
  await sleep(140);
} while (Date.now() < expires);
const page = await runs.outputItems.list(run, {eval_id: evaluation, limit: rows + 1});
const items = page.data.map(item => ({
  id: item.id, index: item.datasource_item.index, answer: item.sample.output[0].content,
  ...(cosine ? {cosine: item.results[1].score} : {}),
  score: item.results[0].score, seed: item.sample.seed, created_at: item.created_at
})).sort((a,b) => a.index - b.index);
assert.ok(items.length > 0 && items.every((r,i) => r.index === i &&
  r.answer === String(count) && r.score === 1 - (i % 2) && Number.isSafeInteger(r.seed)));
assert.equal(new Set(items.map(row => row.id)).size, items.length);
if (cosine) {
  for (const item of items)
    assert.ok(Math.abs(item.cosine - cosine.baseline[item.index % 2]) < 1e-9);
  const report = new URL(state.report_url);
  assert.equal(report.origin, new URL(options.base).origin);
  const readReport = key => fetch(report, {headers: {Authorization: 'Bearer ' + key}, redirect: 'error'});
  const own = await readReport(options.key);
  assert.equal(own.status, 200);
  assert.ok((await own.text()).includes('Semantic score'));
  const foreign = await readReport(options.other);
  assert.equal(foreign.status, 404);
  await foreign.arrayBuffer();
  Object.assign(cosine, {owner_report: own.status, peer_report: foreign.status});
}
let chat;
if (options.phase === 'primary') {
  assert.equal(state.status, 'in_progress', JSON.stringify(state.error));
  assert.ok(items.length < rows);
} else {
  assert.equal(state.status, 'completed', JSON.stringify(state.error));
  assert.equal(items.length, rows);
  assert.deepEqual(items.slice(0, options.previous.items.length), options.previous.items);
  assert.deepEqual(state.result_counts, {total: rows, passed: 3, failed: 3, errored: 0});
  assert.equal(state.per_model_usage[0].invocation_count, rows);
  chat = (await peer.chat.completions.create({model, temperature: 0, reasoning_effort: 'none',
    max_tokens: 32, messages: [{role: 'user', content: 'Return only the answer to 6 times 7.'}]}))
    .choices[0].message.content.trim();
  assert.equal(chat, '42');
}
process.stdout.write(JSON.stringify({sdk: version, phase: options.phase, evaluation, run, file,
  source_sha256: digest, source_deleted: 404, peer: 404, status: state.status, items,
  counts: state.result_counts, usage: state.per_model_usage, chat, cosine}));
