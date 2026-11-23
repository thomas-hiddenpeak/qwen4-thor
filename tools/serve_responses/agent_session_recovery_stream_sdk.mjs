// Complete the owner's pending lookup through the recovered loopback service.
import assert from 'node:assert/strict';
import { loadSdk } from '../serve_protocol/sdk.mjs';
import { continuationIndices, outputPrefix } from './agent_session_output_indices.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.ok(base.protocol === 'http:' && base.hostname === '127.0.0.1' &&
  base.pathname === '/v1' && !base.username && !base.password);
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const sessions = new OpenAI({ baseURL: base.href, apiKey: plan.key, maxRetries: 0,
  timeout: 60000 }).beta.agents.sessions;
const restored = await sessions.retrieve(plan.id);
assert.equal(restored.status, 'requires_action');
assert.deepEqual(restored.agent.tools, []);
assert.deepEqual(restored.required_actions, [plan.action]);
const prefix = await outputPrefix(sessions, plan.id, plan.action);
const projected = continuationIndices(prefix);
const stream = await sessions.events.stream(plan.id);
const request = { events: plan.events, 'Idempotency-Key': plan.idempotency };
const replay = [];
let completed = 0, idle = 0, answer = '';
const start = new Date();
try {
  for (let attempt = 0; attempt < 2; ++attempt)
    replay.push((await sessions.events.create(plan.id, request).asResponse()).status);
  for await (const event of stream) {
    if (event.type === 'error') throw Error(event.error.message);
    if (event.type === 'agent.session.turn.failed') throw Error(JSON.stringify(event.turn.error));
    projected.observe(event);
    if (event.type === 'agent.session.turn.output_text.delta') {
      answer += event.delta;
    }
    if (event.type === 'agent.session.turn.completed') ++completed;
    if (event.type === 'agent.session.idle') {
      ++idle;
      break;
    }
  }
} finally {
  stream.controller.abort();
}
assert.deepEqual(replay, [202, 202]);
assert.ok(projected.indices.length);
assert.equal(completed, 1);
assert.equal(idle, 1);
const revision = JSON.parse(plan.events[0].output).publication_revision;
assert.ok(answer.match(/\d+/g)?.includes(String(revision)), answer);
const stamp = answer.match(/\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?Z/)?.[0];
assert.ok(stamp, answer);
const minute = stamp.slice(0, 16);
assert.ok(minute >= start.toISOString().slice(0, 16) &&
  minute <= new Date().toISOString().slice(0, 16), stamp);
process.stdout.write(JSON.stringify({ sdk: version, answer, revision, minute, prefix, indices: projected.indices,
  completed, idle, replay, revoked: true, call: plan.action.call_id }) + '\n');
