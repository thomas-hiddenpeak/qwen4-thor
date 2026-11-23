// Exercise both phases through the public TLS endpoint of the same object store.
import assert from 'node:assert/strict';
import { loadSdk } from '../serve_protocol/sdk.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.ok(base.protocol === 'https:' && base.hostname === '127.0.0.1' &&
          base.pathname === '/v1' && !base.username && !base.password &&
          !base.search && !base.hash);
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const options = { baseURL: base.href, maxRetries: 0, timeout: 15000 };
const client = new OpenAI({ ...options, apiKey: plan.key });
const agents = client.beta.agents;
const foreign = new OpenAI({ ...options, apiKey: plan.other }).beta.agents;
const model = 'qwen3.8-flash-next';
const expectedStatus = (status) => (error) => error instanceof OpenAI.APIError &&
                                            error.status === status;
const tools = [
  { type: 'function', name: 'lookup', description: 'Read project records',
    parameters: { type: 'object', properties: {}, additionalProperties: false } },
  { type: 'web_search' }, { type: 'tool_search' }, { type: 'computer_use' },
  { type: 'automations' }, { type: 'programmatic_tool_calling' },
  { type: 'mcp', server_label: 'records', transport: { type: 'http',
    server_url: 'https://records.example.test/mcp', headers: { 'x-project': 'q4t' } } },
];
let result;
if (plan.phase === 'create') {
  const agent = await agents.create({ model, tools, metadata: { project: 'q4t' },
    name: 'project records', instructions: 'Read the project records. '.repeat(280),
    multi_agent: { enabled: true } });
  assert.match(agent.id, /^agent_[A-Za-z0-9_-]+$/);
  assert.equal(agent.object, 'agent');
  assert.equal(agent.multi_agent.max_concurrent_subagents, 6);
  assert.equal(agent.compaction.async, true);
  assert.deepEqual(agent.tools.map((tool) => tool.type), tools.map((tool) => tool.type));
  assert.equal(agent.tools[0].defer_loading, false);
  assert.equal(agent.tools[1].mode, 'live');
  assert.equal(agent.tools[5].enabled, true);
  assert.deepEqual(agent.tools[6].transport.headers, { 'x-project': 'q4t' });
  const second = await agents.create({ model });
  const ids = [];
  for await (const entry of agents.list({ order: 'asc', limit: 1 })) {
    ids.push(entry.id);
    assert.ok(ids.length <= 2);
  }
  assert.deepEqual(ids.sort(), [agent.id, second.id].sort());
  assert.equal((await foreign.list()).data.length, 0);
  await assert.rejects(foreign.retrieve(agent.id), expectedStatus(404));
  await assert.rejects(client.get('/agents'), expectedStatus(400));
  await assert.rejects(agents.create({ model: 'absent-fixture-model' }), expectedStatus(404));
  await assert.rejects(agents.update(agent.id, { unknown: true }), expectedStatus(400));
  const capabilities = await client.get('/capabilities');
  assert.deepEqual(capabilities.agent_registration.operations, ['create','retrieve','update','list','delete']);
  result = { id: agent.id, second: second.id, tool_types: tools.map((tool) => tool.type),
    pages: ids.length, foreign_status: 404, selector_required: true };
} else {
  assert.equal(plan.phase, 'resume');
  const { id, second } = plan;
  const saved = await agents.retrieve(id);
  assert.equal(saved.instructions, 'Read the project records. '.repeat(280));
  assert.deepEqual(saved.metadata, { project: 'q4t' });
  assert.equal(saved.tools.length, tools.length);
  const modified = await agents.update(id, { name: null, metadata: null,
    experimental: { time_awareness: true }, compaction: { async: false } });
  assert.equal(modified.name, null);
  assert.deepEqual(modified.metadata, {});
  assert.equal(modified.compaction.async, false);
  const reset = await agents.update(id, { experimental: {}, compaction: null });
  assert.equal(reset.compaction.async, true);
  assert.equal(reset.experimental.time_awareness, true);
  assert.deepEqual(reset.tools, saved.tools);
  await assert.rejects(foreign.delete(id), expectedStatus(404));
  assert.deepEqual(await agents.delete(id), { id, object: 'agent.deleted', deleted: true });
  await assert.rejects(agents.retrieve(id), expectedStatus(404));
  assert.deepEqual((await agents.list()).data.map((agent) => agent.id), [second]);
  assert.equal((await agents.delete(second)).deleted, true);
  result = { persisted: true, updated: true, deleted: true, foreign_status: 404,
    sibling_preserved: true };
}
process.stdout.write(JSON.stringify({ sdk: version, ...result }) + '\n');
