// Independently score the same inputs via the public trained embeddings API.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {setTimeout as sleep} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
const owner = new OpenAI({baseURL: p.base, apiKey: p.key, maxRetries: 0, timeout: 60000});
const peer = new OpenAI({baseURL: p.base, apiKey: p.other, maxRetries: 0});
const model = 'qwen3-embedding-0.6b';
const caps = await fetch(p.base + '/capabilities', {headers: {Authorization: 'Bearer ' + p.key}});
assert.equal(caps.status, 200);
const advertised = await caps.json();
assert.ok(advertised.evals.text_similarity.includes('cosine'));
assert.deepEqual(advertised.eval_embedding_grader, {metric: 'cosine', model,
  dimensions: 1024, range: [0, 1], configured: true});
const scope = JSON.parse(readFileSync(new URL('../../docs/evidence/serve-scope-decision-20261120/decision.json', import.meta.url)));
const answer = (await owner.chat.completions.create({model: p.model, temperature: 0,
  max_tokens: 48, reasoning_effort: 'none', messages: [{role: 'user',
    content: 'Read ' + JSON.stringify(scope.counts) + '. Return only the integer in required_inference_or_application_service.'}]})).choices[0].message.content.trim();
assert.equal(answer, String(scope.counts.required_inference_or_application_service));
const sentence = 'The project scope includes ' + answer + ' inference and application service paths.';
const references = [sentence, 'Audio diarization identifies which speaker is talking during overlapping speech.'];
const expected = [], tokens = [];
for (const reference of references) {
  const reply = await owner.embeddings.create({model, input: [sentence, reference], encoding_format: 'float'});
  assert.equal(reply.model, model);
  const [a, b] = reply.data.map(v => v.embedding);
  assert.equal(a.length, 1024);
  assert.equal(b.length, 1024);
  const dot = a.reduce((sum, x, i) => sum + x*b[i], 0);
  expected.push(Math.max(0, Math.min(1, dot/(Math.hypot(...a)*Math.hypot(...b)))));
  tokens.push(reply.usage.prompt_tokens);
}
const threshold = 0.95;
assert.ok(expected[0] > threshold && expected[1] < threshold);
const ev = await owner.evals.create({data_source_config: {type: 'custom', include_sample_schema: true,
  item_schema: {type: 'object', properties: {reference: {type: 'string'}}, required: ['reference']}},
  testing_criteria: [{name: 'Local semantic similarity', type: 'text_similarity',
    evaluation_metric: 'cosine', pass_threshold: threshold,
    input: '{{sample.output_text}}', reference: '{{item.reference}}'}]});
const run = await owner.evals.runs.create(ev.id, {data_source: {type: 'jsonl', source: {
  type: 'file_content', content: references.map(reference => ({item: {reference}, sample: {output_text: sentence}}))}}});
const end = Date.now()+60000;
let state;
do {
  state = await owner.evals.runs.retrieve(run.id, {eval_id: ev.id});
  if (!['queued','in_progress'].includes(state.status)) break;
  await sleep(150);
} while (Date.now()<end);
assert.equal(state.status, 'completed', JSON.stringify(state.error));
assert.deepEqual(state.result_counts, {total: 2, errored: 0, passed: 1, failed: 1});
const items = (await owner.evals.runs.outputItems.list(run.id,{eval_id: ev.id})).data;
assert.equal(items.length, 2);
items.sort((a, b) => a.datasource_item_id - b.datasource_item_id);
assert.deepEqual(items.map(row => row.datasource_item_id), [0, 1]);
const observed = items.map(row => {
  assert.equal(row.sample.output[0].content, sentence);
  const actual = row.results[0].score;
  assert.ok(Math.abs(actual - expected[row.datasource_item_id]) < 1e-9);
  return actual;
});
const usage = state.per_model_usage;
assert.equal(usage.length, 1);
assert.equal(usage[0].model_name, model);
assert.equal(usage[0].invocation_count, 2);
assert.equal(usage[0].prompt_tokens, tokens[0]+tokens[1]);
assert.equal(usage[0].total_tokens, usage[0].prompt_tokens);
assert.equal(usage[0].completion_tokens, 0);
await assert.rejects(peer.evals.runs.retrieve(run.id, {eval_id: ev.id}), e => e.status === 404);
await owner.evals.delete(ev.id);
const chat = await peer.chat.completions.create({model: p.model, temperature: 0, max_tokens: 32,
  reasoning_effort: 'none', messages: [{role:'user', content:'Return only 6 times 7.'}]});
assert.equal(chat.choices[0].message.content.trim(), '42');
process.stdout.write(JSON.stringify({sdk: version, model, answer, expected, observed, threshold,
  usage: usage[0], foreign_status: 404, independent_chat: '42', run: run.id}));
