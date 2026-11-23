// Discover a deferred function and return its actual project release record.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { setTimeout as delay } from 'node:timers/promises';

import { loadSdk } from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const url = new URL(plan.base);
assert.ok(url.protocol === 'https:' && url.hostname === '127.0.0.1' &&
          url.pathname === '/v1' && !url.username && !url.password);
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
function agentsFor(key) {
  return new OpenAI({
    apiKey: key, baseURL: url.href, maxRetries: 0, timeout: 60000,
  }).beta.agents;
}

const agents = agentsFor(plan.key);
const peer = agentsFor(plan.other);
const missing = e => e instanceof OpenAI.APIError && e.status === 404;
const record = new URL('../../docs/evidence/serve-agent-workspaces-20261124/delivery.json',
                       import.meta.url);
const name = 'read_release_record';
const saved = await agents.create({
  model: 'qwen3.8-flash-next',
  reasoning: { effort: 'none' },
  instructions: 'When asked for a release, first use the server tool search to load ' +
    'read_release_record. Then call that function. After the tool returns, ' +
    'reply with its publication revision number only.',
  tools: [
    { type: 'tool_search' },
    {
      type: 'function', name, defer_loading: true,
      description: 'Read the published project release record.',
      parameters: { type: 'object', properties: {}, required: [], additionalProperties: false },
    },
  ],
});
assert.equal(saved.tools.length, 2);
const { id } = await agents.sessions.create({
  agent_id: saved.id, environment: { type: 'none' },
  input: 'Look up the release. Discover read_release_record with tool search, ' +
    'call it with {}, and report its revision.',
});
const visible = (await agents.sessions.retrieve(id)).agent.tools;
assert.deepEqual(visible.map(t => [t.type, t.name, t.defer_loading]), [['function', name, true]]);
await assert.rejects(peer.sessions.retrieve(id), missing);

async function waitForTurn() {
  const end = Date.now() + 60000;
  do {
    const row = await agents.sessions.retrieve(id);
    if (row.status !== 'in_progress') return row;
    await delay(180);
  } while (Date.now() < end);
  throw Error('Session model step exceeded 60 seconds');
}
const waiting = await waitForTurn();
assert.equal(waiting.status, 'requires_action');
assert.equal(waiting.required_actions.length, 1);
const [action] = waiting.required_actions;
assert.equal(action.name, name);
assert.deepEqual(action.arguments, {});
const updated = await agents.sessions.update(id, { agent: { tools: [] } });
assert.deepEqual(updated.agent.tools, []);

const revision = JSON.parse(readFileSync(record)).publication.publication_revision;
const returned = JSON.stringify({ publication_revision: revision });
const events = [{
  type: 'agent.session.input.tool_result', turn_id: action.turn_id,
  call_id: action.call_id, success: true, output: returned,
}];
await assert.rejects(peer.sessions.events.create(id, { events }), missing);
assert.equal((await agents.sessions.events.create(id, { events }).asResponse()).status, 202);
assert.equal((await waitForTurn()).status, 'idle');
const all = (await agents.sessions.turns.items.list(action.turn_id, {
  session_id: id, order: 'asc',
})).data;
assert.ok(all.every(i => !i.type.startsWith('tool_search')));
const calls = all.filter(i => i.type === 'function_call');
const results = all.filter(i => i.type === 'function_call_output');
assert.deepEqual(calls.map(i => [i.name, i.call_id]), [[name, action.call_id]]);
assert.deepEqual(results.map(i => [i.call_id, i.output]), [[action.call_id, returned]]);
const answer = all.filter(i => i.role === 'assistant')
    .flatMap(i => i.content).map(c => c.text).join('').trim();
assert.ok(answer.match(/\d+/g)?.includes(String(revision)), answer);
assert.equal((await agents.delete(saved.id)).deleted, true);
process.stdout.write(JSON.stringify({
  sdk: version, session: id, turn: action.turn_id, call: action.call_id,
  discovery_switch_visible: false, public_functions: 1, revoked_before_result: true,
  required_actions: 1, results: 1, foreign: 404, revision, answer,
}) + '\n');
