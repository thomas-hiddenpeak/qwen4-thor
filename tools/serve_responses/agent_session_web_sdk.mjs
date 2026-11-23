// Read a real release record through an operator-configured HTTP source and its cache.
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
const sessions = new OpenAI({ ...options, apiKey: plan.key }).beta.agents.sessions;
const peer = new OpenAI({ ...options, apiKey: plan.other }).beta.agents.sessions;
const record = new URL('../../docs/evidence/serve-agent-workspaces-20261124/artifact-delivery.json',
                       import.meta.url);
const revision = JSON.parse(readFileSync(record)).publication.publication_revision;
const phases = {};
for (const mode of ['live', 'cached', 'disabled']) {
  const enabled = mode !== 'disabled';
  const stream = (await sessions.create({
    environment: { type: 'none' }, stream: true,
    agent: {
      model: 'qwen3.8-flash-next', reasoning: { effort: 'none' },
      instructions: enabled ? 'Use the web search tool once to search for Q4T publication revision. ' +
        'Answer with the exact revision number from the source.' : 'Answer directly.',
      tools: [{ type: 'web_search', mode, context_size: 'low', allowed_domains: ['127.0.0.1'] }],
    },
    input: enabled ? 'According to the Q4T publication record, what revision is published? ' +
      'Search for Q4T publication revision to verify the number.' : 'Compute 17 + 25. Reply with the number.',
  })).withResultCollection();
  const seen = new Map();
  for await (const event of stream) {
    if (event.type === 'error') throw Error(event.error.message);
    if (event.type !== 'agent.session.turn.item.done' || event.item.type !== 'web_search_call')
      continue;
    assert.equal(event.item.status, 'completed');
    assert.deepEqual(Object.keys(event.item.action).sort(), ['queries', 'query', 'type']);
    assert.equal(event.item.action.type, 'search');
    assert.ok(event.item.action.queries.length > 0 && event.item.action.query);
    seen.set(event.item.id, event.item.action);
  }
  const result = await stream.finalResult();
  assert.equal(result.turn.status, 'completed');
  const items = (await sessions.turns.items.list(result.turn_id, {
    session_id: result.session_id, order: 'asc',
  })).data.filter(item => item.type === 'web_search_call');
  assert.deepEqual(items.map(i => [i.id, i.action]), [...seen.entries()]);
  assert.equal(seen.size > 0, enabled);
  assert.ok(result.output_text.match(/\d+/g)?.includes(String(enabled ? revision : 42)),
            result.output_text);
  await assert.rejects(peer.retrieve(result.session_id),
                       err => err instanceof OpenAI.APIError && err.status === 404);
  phases[mode] = { answer: result.output_text, actions: [...seen.values()], foreign: 404 };
}
process.stdout.write(JSON.stringify({ sdk: version, revision, phases }) + '\n');
