// Verify per-request links to the same owned run through three real frontends.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {setTimeout as sleep} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
const owner = new OpenAI({baseURL: p.native, apiKey: p.key, maxRetries: 0});
const data = JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-scope-decision-20261120/decision.json', import.meta.url)));
const count = String(data.counts.required_inference_or_application_service);
const message = 'Read the project record and return only the integer in ' +
  'required_inference_or_application_service. Record: ' + JSON.stringify(data.counts);
const answer = (await owner.chat.completions.create({model: p.model,
  messages: [{role: 'user', content: message}], temperature: 0,
  max_tokens: 48, reasoning_effort: 'none'})).choices[0].message.content.trim();
assert.equal(answer, count);
const evaluation = await owner.evals.create({data_source_config: {type: 'custom',
  item_schema: {type: 'object', properties: {reference: {type: 'string'}},
    required: ['reference']}, include_sample_schema: true}, testing_criteria: [{
    type: 'string_check', name: 'Scope & reference', operation: 'eq',
    input: '{{sample.output_text}}', reference: '{{item.reference}}'}]});
const rows = [count, String(Number(count) + 1)].map(reference => ({
  item: {reference}, sample: {output_text: answer}}));
const run = await owner.evals.runs.create(evaluation.id, {
  name: 'Project <scope> review', data_source: {type: 'jsonl',
    source: {type: 'file_content', content: rows}}});
const detail = '/evals/' + evaluation.id + '/runs/' + run.id;
assert.equal(run.report_url, p.native + detail + '/report');
const deadline = Date.now() + 30000;
let completed;
do {
  completed = await owner.evals.runs.retrieve(run.id, {eval_id: evaluation.id});
  if (completed.status === 'completed') break;
  await sleep(100);
} while (Date.now() < deadline);
assert.equal(completed.status, 'completed');
const results = [];
for (const entry of ['native', 'nginx', 'direct']) {
  const base = p[entry];
  const auth = key => entry === 'direct' ? {
    'X-Q4T-Client-Id': key === p.key ? 'alpha' : 'beta',
    'X-Q4T-Gateway-Token': p.binding,
  } : {Authorization: 'Bearer ' + key};
  const api = new OpenAI({apiKey: p.key, baseURL: base, maxRetries: 0,
    defaultHeaders: entry === 'direct' ? auth(p.key) : {}});
  const found = await api.evals.runs.retrieve(run.id, {eval_id: evaluation.id});
  const list = await api.evals.runs.list(evaluation.id, {status: 'completed'});
  assert.equal(found.report_url, base + detail + '/report');
  assert.equal(list.data.find(r => r.id === run.id).report_url, found.report_url);
  const page = await fetch(found.report_url, {headers: auth(p.key), redirect: 'error'});
  assert.equal(page.status, 200);
  assert.match(page.headers.get('content-type'), /^text\/html/i);
  assert.match(page.headers.get('cache-control'), /no-store/);
  const html = await page.text();
  assert.ok(html.includes('Project &lt;scope&gt; review') && html.includes(run.id));
  assert.ok(html.includes('Scope &amp; reference</th><td>1</td><td>1'));
  const href = html.match(/<a href="([^"]+)">Output items/)[1];
  const outputUrl = new URL(href, found.report_url);
  assert.equal(outputUrl.href, base + detail + '/output_items');
  const output = await fetch(outputUrl, {headers: auth(p.key)});
  assert.equal(output.status, 200);
  assert.deepEqual((await output.json()).data.map(v => v.status).sort(), ['fail', 'pass']);
  const foreign = await fetch(found.report_url, {headers: auth(p.other)});
  assert.equal(foreign.status, 404);
  assert.equal((await foreign.json()).error.code, 'object_not_found');
  results.push({entry, absolute: found.report_url, foreign_status: foreign.status,
    sha256: createHash('sha256').update(html).digest('hex'), bytes: Buffer.byteLength(html)});
}
assert.equal(new Set(results.map(r => r.sha256)).size, 1);
await owner.evals.runs.delete(run.id, {eval_id: evaluation.id});
assert.equal((await fetch(run.report_url, {headers: {Authorization: 'Bearer ' + p.key}})).status, 404);
await owner.evals.delete(evaluation.id);
const independent = new OpenAI({apiKey: p.other, baseURL: p.native, maxRetries: 0});
const chat = await independent.chat.completions.create({model: p.model, temperature: 0,
  max_tokens: 32, reasoning_effort: 'none', messages: [{role: 'user', content: 'Return only 6 times 7.'}]});
assert.equal(chat.choices[0].message.content.trim(), '42');
process.stdout.write(JSON.stringify({sdk: version, evaluation: evaluation.id, run: run.id,
  answer, independent_chat: '42', results, deleted_status: 404}));
