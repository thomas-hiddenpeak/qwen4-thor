// Real SDK iteration, local function dispatch, finalResult and owner-only replay.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { loadSdk } from '../serve_protocol/sdk.mjs';
let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.ok(base.protocol === 'https:' && base.hostname === '127.0.0.1' &&
          base.pathname === '/v1' && !base.username && !base.password);
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const options = { baseURL: base.href, maxRetries: 0, timeout: 80000 };
const api = new OpenAI({ ...options, apiKey: plan.key }).beta.agents.sessions;
const peer = new OpenAI({ ...options, apiKey: plan.other }).beta.agents.sessions;
const source = new URL('../../docs/evidence/serve-agent-registration-20261123/delivery.json', import.meta.url);
const checkClock = plan.verify_session_clock === true;
let calls = 0;
const stream = (await api.create({ environment: { type: 'none' }, stream: true,
  agent: { model: 'qwen3.8-flash-next', reasoning: { effort: 'none' },
    ...(checkClock ? { experimental: { time_awareness: true } } : {}),
    instructions: 'For a project release query call read_release_record first. After receiving its result answer with the revision number.',
    tools: [{ type: 'function', name: 'read_release_record',
      description: 'Read the actual q4t publication receipt.',
      parameters: { type: 'object', properties: {}, additionalProperties: false, required: [] } }] },
  input: 'Call read_release_record now with {}. After the tool result, tell me the actual publication revision.',
  toolHandlers: { read_release_record(args) {
    assert.deepEqual(args, {});
    assert.equal(++calls, 1);
    return { publication_revision: JSON.parse(readFileSync(source, 'utf8')).publication.publication_revision };
  } } })).withResultCollection();
const ids = [], types = [];
const firstItems = [];
let streamed = '';
for await (const event of stream) {
  assert.ok(event.event_id.startsWith('evt_'));
  ids.push(event.event_id);
  types.push(event.type);
  if (event.type === 'error') throw Error(event.error.message);
  if (event.type === 'agent.session.turn.item.added' && event.output_index != null)
    firstItems.push({ type: event.item.type, index: event.output_index });
  if (event.type === 'agent.session.turn.output_text.delta') streamed += event.delta;
}
const result = await stream.finalResult();
assert.equal(calls, 1);
assert.equal(ids.length, new Set(ids).size);
assert.equal(types[0], 'agent.session.created');
assert.equal(types.at(-1), 'agent.session.idle');
for (const type of ['agent.session.requires_action','agent.session.turn.completed','agent.session.turn.created'])
  assert.equal(types.filter(x => x === type).length, 1);
assert.equal(result.turn.status, 'completed');
assert.equal(streamed.trim(), result.output_text.trim());
assert.match(result.output_text, /\b442\b/);
const id = result.session_id;
if (checkClock) {
  assert.deepEqual(firstItems.map(item => item.index), firstItems.map((_, i) => i));
  const callIndex = firstItems.findIndex(item => item.type === 'function_call');
  assert.ok(callIndex >= 0 && firstItems.slice(callIndex + 1).some(item => item.type === 'message'));
}
await assert.rejects(peer.events.stream(id), e => e instanceof OpenAI.APIError && e.status === 404);
// The SDK opens GET /events on an idle session before POSTing the new input.
// Old pending actions must never be delivered to this new local dispatcher.
const beforeFollow = new Date();
const follow = api.stream(id, {
  input: 'The records lookup is complete. Do not call a tool. Repeat the revision number from the tool result.' +
    (checkClock ? ' Then give the current UTC minute from your system reminder as YYYY-MM-DDTHH:MMZ.' : ''),
  toolHandlers: { read_release_record() { throw Error('Unexpected repeat of the first turn'); } },
}).withResultCollection();
const later = [];
const nextIndices = [];
for await (const event of follow) {
  assert.ok(!ids.includes(event.event_id));
  later.push(event.type);
  if (event.type === 'agent.session.turn.item.added' && event.output_index != null)
    nextIndices.push(event.output_index);
}
const second = await follow.finalResult();
assert.notEqual(second.turn_id, result.turn_id);
assert.match(second.output_text, /\b442\b/);
assert.equal(later.filter(x => x === 'agent.session.requires_action').length, 0);
const items = (await api.items.list(id, { order:'asc' })).data;
assert.equal(items.filter(x => x.type === 'function_call_output').length, 1);
if (checkClock) {
  assert.ok(nextIndices.length > 0);
  assert.deepEqual(nextIndices, nextIndices.map((_, i) => i));
  const stamp = second.output_text.match(/\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?Z/)?.[0];
  assert.ok(stamp, second.output_text);
  const minute = stamp.slice(0, 16);
  assert.ok(minute >= beforeFollow.toISOString().slice(0, 16) &&
            minute <= new Date().toISOString().slice(0, 16), stamp);
  assert.ok(items.every(item => item.role !== 'system' && item.role !== 'developer'));
}
assert.equal((await api.delete(id)).deleted, true);
process.stdout.write(JSON.stringify({ sdk: version, calls, events:ids.length, types,
  ...(checkClock ? { clock_checked:true, first_items:firstItems, next_indices:nextIndices } : {}),
  output:result.output_text, usage:result.turn.usage, new_turn_events:later.length, new_turn_output:second.output_text, foreign_status:404 }) + '\n');
