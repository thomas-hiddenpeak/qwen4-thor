// Drive a caller-owned, read-only project lookup through the public session API.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { loadSdk } from '../serve_protocol/sdk.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.ok(base.protocol === 'https:' && base.hostname === '127.0.0.1' &&
          base.pathname === '/v1' && !base.username && !base.password);
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const options = { baseURL: base.href, maxRetries: 0, timeout: 30000 };
const sessions = new OpenAI({ ...options, apiKey: plan.key }).beta.agents.sessions;
const peer = new OpenAI({ ...options, apiKey: plan.other }).beta.agents.sessions;
const expected = status => err => err instanceof OpenAI.APIError && err.status === status;
const session = await sessions.create({
  agent: { model: 'qwen3.8-flash-next', reasoning: { effort: 'none' },
    instructions: 'You are a q4t project records assistant. Call read_release_record first for a release query. After receiving the result, answer with the requested field from that result.',
    tools: [{ type: 'function', name: 'read_release_record',
      description: 'Look up the current actual q4t publication receipt.',
      parameters: { type: 'object', properties: {}, required: [],
                    additionalProperties: false } }] },
  environment: { type: 'none' },
  input: 'Call read_release_record now with {} to get the actual publication revision. After the tool returns, reply with the revision number.',
});
const id = session.id;
async function settled() {
  const deadline = Date.now() + 70000;
  do {
    const state = await sessions.retrieve(id);
    if (state.status !== 'in_progress') return state;
    await new Promise(resolve => setTimeout(resolve, 350));
  } while (Date.now() < deadline);
  throw Error('Session model step exceeded its deadline');
}
const waiting = await settled();
assert.equal(waiting.status, 'requires_action');
assert.equal(waiting.required_actions.length, 1);
const [action] = waiting.required_actions;
assert.equal(action.type, 'function_call');
assert.equal(action.name, 'read_release_record');
assert.deepEqual(action.arguments, {});
const publication = JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-agent-registration-20261123/delivery.json',
  import.meta.url), 'utf8')).publication;
const output = JSON.stringify({ publication_revision: publication.publication_revision,
                                desired: publication.desired });
const event = { type: 'agent.session.input.tool_result', call_id: action.call_id,
                turn_id: action.turn_id, success: true, output };
const request = { events: [event], 'Idempotency-Key': 'sdk-release-lookup-1' };
await assert.rejects(peer.events.create(id, request), expected(404));
const wrong = { events: [{ ...event, call_id: 'absent' }] };
await assert.rejects(sessions.events.create(id, wrong), expected(409));
assert.equal((await sessions.events.create(id, request).asResponse()).status, 202);
assert.equal((await sessions.events.create(id, request).asResponse()).status, 202);
await assert.rejects(sessions.events.create(id, { ...request,
  events: [{ ...event, output: 'different' }] }), expected(409));
const result = await settled();
assert.equal(result.status, 'idle');
assert.deepEqual(result.required_actions, []);
const turns = (await sessions.turns.list(id)).data;
assert.equal(turns.length, 1);
assert.equal(turns[0].id, action.turn_id);
const items = (await sessions.turns.items.list(action.turn_id,
  { session_id: id, order: 'asc' })).data;
assert.equal(items.filter(i => i.type === 'function_call_output').length, 1);
assert.equal(items.find(i => i.type === 'function_call_output').output, output);
const text = items.filter(i => i.role === 'assistant').flatMap(i => i.content)
  .map(p => p.text).join('').trim();
assert.ok(text.includes(String(publication.publication_revision)), text);
assert.equal((await sessions.delete(id)).deleted, true);
process.stdout.write(JSON.stringify({ sdk: version, lookup: action.name,
  publication_revision: publication.publication_revision, answer: text,
  turns: turns.length, items: items.length, usage: result.usage,
  replay_status: 202, conflict_status: 409, foreign_status: 404, deleted: true }) + '\n');
