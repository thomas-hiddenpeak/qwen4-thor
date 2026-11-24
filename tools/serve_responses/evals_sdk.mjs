// Exercise the official client against a deployed service with real project data.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let wire = '';
for await (const chunk of process.stdin) wire += chunk;
const plan = JSON.parse(wire);
const {OpenAI, version} = loadSdk(plan.sdk);
const owner = new OpenAI({baseURL: plan.base, apiKey: plan.key, maxRetries: 0});
const peer = new OpenAI({baseURL: plan.base, apiKey: plan.other, maxRetries: 0});
const decision = JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-scope-decision-20261120/decision.json', import.meta.url)));
const calls = [];
async function request(operation, pending, status) {
  const response = await pending.asResponse();
  assert.equal(response.status, status, operation);
  const value = await response.clone().json();
  const data = await pending;
  if (typeof data.getNextPage === 'function') assert.deepEqual(data.data, value.data);
  else assert.deepEqual(data, value);
  calls.push({operation, status, value});
  return value;
}
const created = await request('create', owner.evals.create({
  name: 'Project scope decision',
  data_source_config: {type: 'custom', item_schema: {
    type: 'object', properties: {decision: {type: 'object'}, expected: {type: 'integer'}},
    required: ['decision', 'expected']}},
  testing_criteria: [{name: 'Required paths', type: 'string_check',
    operation: 'eq', input: '{{item.decision.counts.required_inference_or_application_service}}',
    reference: '{{item.expected}}'}],
  metadata: {goal: decision.goal}
}), 201);
assert.ok(created.id.startsWith('eval_'));
assert.deepEqual(created.data_source_config.schema.required, ['item']);
const path = created.id;
const retrieved = await request('retrieve', owner.evals.retrieve(path), 200);
assert.deepEqual(retrieved, created);
await assert.rejects(peer.evals.retrieve(path), err => err.status === 404);
const updated = await request('update', owner.evals.update(path, {name: 'Reviewed project scope',
  metadata: {goal: decision.goal, date: decision.date}}), 200);
assert.equal(updated.name, 'Reviewed project scope');
assert.deepEqual(updated.testing_criteria, created.testing_criteria);
const listing = await request('list', owner.evals.list({order_by: 'updated_at', order: 'desc', limit: 10}), 200);
assert.ok(listing.data.some(row => row.id === path && row.name === updated.name));
await assert.rejects(peer.evals.delete(path), err => err.status === 404);
let device;
if (plan.model) {
  const criteria = updated.testing_criteria[0];
  const field = criteria.input.match(/^\{\{item\.decision\.counts\.(\w+)\}\}$/)?.[1];
  assert.ok(Object.hasOwn(decision.counts, field));
  const completed = await owner.chat.completions.create({model: plan.model,
    temperature: 0, max_tokens: 64, reasoning_effort: 'none', messages: [
      {role: 'user', content: 'Read this JSON record and return just the integer value of counts.' +
        field + '. Record: ' + JSON.stringify(decision)}]});
  const actual = completed.choices[0].message.content.trim();
  assert.equal(actual, String(decision.counts[field]));
  const independent = await peer.chat.completions.create({model: plan.model,
    temperature: 0, max_tokens: 32, reasoning_effort: 'none',
    messages: [{role: 'user', content: 'Return only the decimal answer to 6 times 7.'}]});
  const peerAnswer = independent.choices[0].message.content.trim();
  assert.equal(peerAnswer, '42');
  device = {criterion: criteria.name, field, answer: actual, independent_chat: peerAnswer};
}
const deleted = await request('delete', owner.evals.delete(path), 200);
assert.equal(deleted.eval_id, path);
assert.equal(deleted.deleted, true);
await assert.rejects(owner.evals.retrieve(path), err => err.status === 404);
process.stdout.write(JSON.stringify({sdk: version, calls, cross_project: 404, deleted_read: 404, device}));
