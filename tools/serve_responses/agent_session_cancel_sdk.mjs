// Cancel an unfulfilled tool request and a running generation over native TLS.
import assert from 'node:assert/strict';
import { loadSdk } from '../serve_protocol/sdk.mjs';

let raw = '';
for await (const part of process.stdin) {
  raw += part;
  assert.ok(Buffer.byteLength(raw) <= 16384);
}
const plan = JSON.parse(raw);
const base = new URL(plan.base);
assert.ok(base.protocol === 'https:' && base.hostname === '127.0.0.1' &&
          base.pathname === '/v1' && !base.username && !base.password);
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const options = { baseURL: base.href, maxRetries: 0, timeout: 65000 };
const api = new OpenAI({ ...options, apiKey: plan.key }).beta.agents.sessions;
const peer = new OpenAI({ ...options, apiKey: plan.other }).beta.agents.sessions;
const cancel = { events: [{ type: 'agent.session.input.cancel' }],
                 'Idempotency-Key': 'release-cancel' };
const expected = status => error => error instanceof OpenAI.APIError && error.status === status;
const agent = { model: 'qwen3.8-flash-next', reasoning: { effort: 'none' } };
const environment = { type: 'none' };
const created = await api.create({ agent: { ...agent,
  instructions: 'Call read_release_record for the release number.',
  tools: [{ type: 'function', name: 'read_release_record',
    description: 'Read the q4t release receipt.', parameters: {
      type: 'object', properties: {}, required: [], additionalProperties: false } }] },
  environment, input: 'Call read_release_record with {} now to obtain the release number.' });
const id = created.id;

async function settled() {
  const until = Date.now() + 45000;
  do {
    const state = await api.retrieve(id);
    if (state.status !== 'in_progress') return state;
    await new Promise(resolve => setTimeout(resolve, 200));
  } while (Date.now() < until);
  throw Error('Session did not settle');
}

const waiting = await settled();
assert.equal(waiting.required_actions.length, 1);
const action = waiting.required_actions[0];
assert.equal(action.name, 'read_release_record');
await assert.rejects(peer.events.create(id, cancel), expected(404));
const stream = await api.events.stream(id);
assert.equal((await api.events.create(id, cancel).asResponse()).status, 202);
const observed = [];
for await (const event of stream) {
  observed.push(event);
  if (event.type === 'agent.session.idle') break;
}
assert.deepEqual(observed.map(e => e.type), ['agent.session.turn.cancelled', 'agent.session.idle']);
assert.equal(observed[0].turn.id, action.turn_id);
assert.equal((await api.turns.retrieve(action.turn_id, { session_id: id })).status, 'cancelled');
const late = { events: [{ type: 'agent.session.input.tool_result',
  turn_id: action.turn_id, call_id: action.call_id, success: true, output: 'late result' }] };
await assert.rejects(api.events.create(id, late), expected(409));

await api.update(id, { agent: { tools: null } });
await api.events.create(id, { events: [{ type: 'agent.session.input.message', input: [{
  role: 'user', content: [{ type: 'input_text', text: 'Reply only with the sum of 17 and 25.' }] }] }] });
assert.equal((await api.events.create(id, cancel).asResponse()).status, 202);
const completed = await settled();
assert.equal(completed.status, 'idle');
const turns = (await api.turns.list(id, { order: 'asc' })).data;
assert.deepEqual(turns.map(t => t.status), ['cancelled', 'completed']);
const items = (await api.turns.items.list(turns[1].id, { session_id: id })).data;
const answer = items.filter(i => i.role === 'assistant').flatMap(i => i.content)
  .map(i => i.text).join('').trim();
assert.match(answer, /\b42\b/);
const extra = { events: [{ type: 'agent.session.input.message', input: [{role: 'user',
  content: [{ type: 'input_text', text: 'Reply only with the decimal number 42.' }] }] }],
  'Idempotency-Key': 'retry-after-data-release' };

const running = (await api.create({ agent, environment, stream: true,
  input: 'Write a long detailed report on the history of mathematics, with at least 25 paragraphs.' }));
let activeId, signalled = false, terminal = 0;
for await (const event of running) {
  if (event.type === 'error') throw Error(event.error.message);
  if (event.type === 'agent.session.created') activeId = event.session.id;
  if (event.type === 'agent.session.turn.in_progress' && !signalled) {
    assert.ok(activeId);
    if (plan.single_slot) {
      await assert.rejects(api.events.create(id, extra),
        error => expected(429)(error) && error.code === 'client_capacity_exhausted');
    }
    assert.equal((await api.events.create(activeId, cancel).asResponse()).status, 202);
    signalled = true;
  }
  if (event.type === 'agent.session.turn.cancelled') ++terminal;
}
assert.ok(signalled);
assert.equal(terminal, 1);
const final = (await api.turns.list(activeId)).data;
assert.equal(final.length, 1);
assert.equal(final[0].status, 'cancelled');
assert.equal((await api.delete(activeId)).deleted, true);
if (plan.single_slot) {
  assert.equal((await api.events.create(id, extra).asResponse()).status, 202);
  assert.equal((await settled()).status, 'idle');
  const history = (await api.turns.list(id, { order: 'asc' })).data;
  assert.deepEqual(history.map(t => t.status), ['cancelled', 'completed', 'completed']);
}
assert.equal((await api.delete(id)).deleted, true);
process.stdout.write(JSON.stringify({ sdk: version, foreign: 404, late_result: 409,
  waiting_terminal: observed[0].turn.status, retry_accepted: 202,
  next_turn: turns[1].status, answer, active_terminal: final[0].status,
  active_terminal_count: terminal, both_deleted: true, saturated_submit: plan.single_slot ? 429 : null,
  resubmitted_after_release: Boolean(plan.single_slot) }) + '\n');
