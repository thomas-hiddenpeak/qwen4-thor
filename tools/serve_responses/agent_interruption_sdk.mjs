// Coordinate an owned model fault only after the SDK has seen durable agent work.
import assert from 'node:assert/strict';
import { loadSdk } from '../serve_protocol/sdk.mjs';

const say = (value) => process.stdout.write(JSON.stringify(value) + '\n');
let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.equal(base.hostname, '127.0.0.1');
assert.ok(['http:', 'https:'].includes(base.protocol) && base.pathname === '/v1' &&
          !base.username && !base.password && !base.search && !base.hash);
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const options = { baseURL: base.href, apiKey: plan.key, maxRetries: 0, timeout: 30000 };
const api = new OpenAI(options).beta.responses;
const betas = ['responses_multi_agent=v1'];

if (plan.mode === 'recover' || plan.mode === 'recover-drain') {
  const draining = plan.mode === 'recover-drain';
  let restored;
  const observed = [];
  for (let attempt = 0; attempt < 8; ++attempt) {
    restored = await api.retrieve(plan.id, { betas });
    observed.push(restored.status);
    if (restored.status !== 'in_progress') break;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  const other = new OpenAI({ ...options, apiKey: plan.other }).beta.responses;
  await assert.rejects(other.retrieve(plan.id, { betas }), error => error.status === 404);
  assert.equal(restored.id, plan.id);
  assert.equal(restored.status, 'failed');
  const cause = draining ? 'service_draining' : 'background_execution_interrupted';
  if (plan.legacy_error_code === true) {
    assert.equal(restored.error.code, cause);
  } else {
    assert.equal(restored.error.code, 'server_error');
    assert.equal(restored.error.type, cause);
  }
  // Original store fixtures retain the historical SDK shape.
  const items = plan.items?.map(item => {
    if (plan.legacy_error_code || item.type !== 'multi_agent_call_output') return item;
    return { ...item, output: item.output.map(part => ({ ...part, logprobs: part.logprobs ?? [] })) };
  });
  let additional = [];
  if (draining) {
    assert.deepEqual(restored.output.slice(0, items.length), items);
    additional = restored.output.slice(plan.items.length);
    assert.ok(additional.length <= 1);
    for (const item of additional) {
      assert.equal(item.type, 'multi_agent_call');
      assert.equal(item.action, 'wait_agent');
      assert.ok(item.call_id && !plan.items.some(old => old.call_id === item.call_id));
    }
  } else if (items) {
    assert.deepEqual(restored.output, items);
  }
  const replay = [];
  if (Number.isInteger(plan.sequence)) {
    const stream = await api.retrieve(plan.id, { stream: true, starting_after: plan.sequence, betas });
    for await (const event of stream) {
      assert.equal(event.sequence_number, plan.sequence + 1 + replay.length);
      replay.push(event.type);
      if (draining && event.item) {
        assert.equal(event.output_index, plan.items.length);
        assert.deepEqual(event.item, additional[0]);
      }
      if (event.type === 'response.failed') {
        assert.deepEqual(event.response.output, restored.output);
        assert.deepEqual(event.response.error, restored.error);
      }
      assert.ok(replay.length <= 64);
    }
    assert.equal(replay.at(-1), 'response.failed');
    assert.equal(replay.filter(type => type === 'response.failed').length, 1);
    if (draining) {
      const committed = additional.length ? ['response.output_item.added', 'response.output_item.done'] : [];
      assert.deepEqual(replay, [...committed, 'response.failed']);
    }
  }
  say({ status: restored.status, error: restored.error, id: restored.id, other: 404, observed, replay });
} else {
  assert.ok(['cancel', 'interrupt'].includes(plan.mode));
  const stream = await api.create({ model: 'qwen3.8-flash-next',
    reasoning: { effort: 'none' }, temperature: 0, store: true, background: true,
    stream: true, max_output_tokens: 384, betas,
    input: 'First use multi_agent_spawn_agent with name "reader" and task ' +
      '"List every integer from 1 to 500 in order. Do not delegate." ' +
      'Then use multi_agent_wait_agent for root/reader. Report its answer.',
    multi_agent: { enabled: true, max_concurrent_subagents: 1 } });
  let id, sequence = -1, spawned = false, cancelled;
  try {
    for await (const event of stream) {
      assert.equal(event.sequence_number, ++sequence);
      if (event.response) {
        id ??= event.response.id;
        assert.equal(id, event.response.id);
      }
      if (event.type === 'response.output_item.done' &&
          event.item.type === 'multi_agent_call_output' &&
          event.item.action === 'spawn_agent') {
        assert.ok(id && !spawned);
        spawned = true;
        say({ event: 'spawned', id, sequence, call_id: event.item.call_id });
        if (plan.mode === 'cancel') {
          cancelled = await api.cancel(id, { betas });
          assert.equal(cancelled.status, 'cancelled');
          break;
        }
      }
      assert.notEqual(event.type, 'response.completed', 'fixture finished before interruption');
    }
  } catch (error) {
    if (!spawned || plan.mode !== 'interrupt' || error instanceof assert.AssertionError) throw error;
    say({ event: 'stream_closed', error: error.name });
  }
  assert.ok(spawned);
  if (plan.mode === 'cancel') {
    const stored = await api.retrieve(id, { betas });
    assert.equal(stored.status, cancelled.status);
    assert.equal(stored.id, id);
    say({ event: 'finished', id, status: stored.status, sequence });
  } else {
    say({ event: 'ended', id, sequence });
  }
}
