// Official SDK exercises native response execution/state over real HTTP.
// Generation deltas are CPU fixtures, not evidence of model/GPU quality.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { readFileSync, existsSync, mkdirSync, rmSync, openSync, closeSync, appendFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import test from 'node:test';
import { loadSdk } from '../serve_protocol/sdk.mjs';

const { OpenAI } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
assert.ok(process.env.Q4T_HOST_TESTS && process.env.Q4T_RESPONSES_STATE_OUTPUT);
const executable = resolve(process.env.Q4T_HOST_TESTS);
const output = resolve(process.env.Q4T_RESPONSES_STATE_OUTPUT);
let launches = 0;
async function peer(name, work) {
  const root = resolve(output, name);
  mkdirSync(root, { recursive: true });
  const ready = resolve(root, 'ready.json');
  rmSync(ready, { force: true });
  const log = openSync(resolve(root, `process-${++launches}.log`), 'w');
  const child = spawn(executable, ['--responses-api-fixture', root], { stdio: ['ignore', log, log] });
  closeSync(log);
  const exited = once(child, 'exit');
  let base;
  const traces = [];
  try {
    const deadline = Date.now() + 5000;
    while (!existsSync(ready) && child.exitCode === null && Date.now() < deadline) await delay(10);
    assert.ok(existsSync(ready), 'native fixture readiness');
    const { port, pid } = JSON.parse(readFileSync(ready, 'utf8'));
    assert.equal(pid, child.pid);
    base = `http://127.0.0.1:${port}`;
    const sdk = key => new OpenAI({ apiKey: key, baseURL: base + '/v1', maxRetries: 0, timeout: 5000,
      fetch: async (url, init) => {
        const response = await fetch(url, init);
        traces.push(response.clone().text().then(body => appendFileSync(resolve(root, 'wire.jsonl'),
          JSON.stringify({ url: String(url), method: init?.method, request: init?.body, status: response.status, body }) + '\n')));
        return response;
      } });
    return await work(sdk('cpu-a'), sdk('cpu-b'), base);
  } finally {
    await Promise.all(traces);
    try {
      if (base && child.exitCode === null)
        await fetch(base + '/__fixture/stop', { method: 'POST', headers: { Authorization: 'Bearer cpu-a' }, signal: AbortSignal.timeout(3000) });
      const code = await Promise.race([exited, delay(5000).then(() => { throw new Error('native fixture exit timeout'); })]);
      assert.deepEqual(code, [0, null]);
    } finally {
      if (child.exitCode === null) child.kill('SIGKILL');
    }
  }
}

const input = { model: 'cpu', input: 'read project record', reasoning: { effort: 'none' } };

test('official stored response survives restart and is actually deleted', async () => {
  let id;
  await peer('durable', async sdk => {
    const created = await sdk.responses.create({ ...input, metadata: { topic: '运行记录' } });
    id = created.id;
    assert.equal(created.status, 'completed');
    assert.equal(created.output_text, 'CPU fixture answer');
    assert.equal(created.usage.total_tokens, 7);
  });
  await peer('durable', async sdk => {
    const restored = await sdk.responses.retrieve(id);
    assert.equal(restored.metadata.topic, '运行记录');
    assert.equal(restored.output_text, 'CPU fixture answer');
    const response = await sdk.responses.delete(id).asResponse();
    assert.deepEqual(await response.json(), { id, object: 'response.deleted', deleted: true });
    await assert.rejects(sdk.responses.retrieve(id), error => error.status === 404);
    await assert.rejects(sdk.responses.inputItems.list(id), error => error.status === 404);
  });
});

test('official inputItems auto-pagination contains resolved prior input and output', async () => {
  await peer('history', async sdk => {
    const first = await sdk.responses.create(input);
    const second = await sdk.responses.create({ ...input, input: 'second question', previous_response_id: first.id });
    const items = [];
    for await (const item of sdk.responses.inputItems.list(second.id, { limit: 1, order: 'asc' })) items.push(item);
    assert.equal(items.length, 3);
    assert.deepEqual(items.map(item => item.role), ['user', 'assistant', 'user']);
    assert.equal(items[0].content[0].text, input.input);
    assert.equal(items[1].id, first.output[0].id);
    assert.equal(items[2].content[0].text, 'second question');
    const reverse = await sdk.responses.inputItems.list(second.id);
    assert.deepEqual(reverse.data.map(item => item.id), items.toReversed().map(item => item.id));
    await assert.rejects(sdk.responses.inputItems.list(second.id, { after: 'unowned' }), error => error.status === 400);
  });
});

test('official SSE retrieve resumes exact stored sequence and terminal', async () => {
  await peer('stream', async sdk => {
    const original = [];
    for await (const event of await sdk.responses.create({ ...input, stream: true, stream_options: { include_obfuscation: false } })) original.push(event);
    const id = original[0].response.id;
    assert.equal(original[0].type, 'response.created');
    assert.equal(original.at(-1).type, 'response.completed');
    const replay = [];
    for await (const event of await sdk.responses.retrieve(id, {
      stream: true, starting_after: 1, include_obfuscation: false,
      include: ['message.output_text.logprobs'],
    })) replay.push(event);
    assert.deepEqual(replay, original.slice(2));
    const empty = [];
    for await (const event of await sdk.responses.retrieve(id, { stream: true, starting_after: original.at(-1).sequence_number })) empty.push(event);
    assert.deepEqual(empty, []);
    const padded = [];
    for await (const event of await sdk.responses.retrieve(id, { stream: true })) padded.push(event);
    assert.ok(padded.every(event => typeof event.obfuscation === 'string' && event.obfuscation.length > 0));
  });
});

test('official errors and ownership prevent other clients from reading or deleting', async () => {
  await peer('isolation', async (sdk, other) => {
    const response = await sdk.responses.create(input);
    await assert.rejects(other.responses.retrieve(response.id), error => error.status === 404);
    await assert.rejects(other.responses.delete(response.id), error => error.status === 404);
    await assert.rejects(other.responses.inputItems.list(response.id), error => error.status === 404);
    await assert.rejects(sdk.responses.retrieve(response.id, { stream: true, starting_after: -1 }), error => error.status === 400 && error.param === 'starting_after');
    await assert.rejects(sdk.responses.retrieve(response.id, { include: ['wrong'] }), error => error.status === 400 && error.param === 'include');
    const stateless = await sdk.responses.create({ ...input, store: false });
    await assert.rejects(sdk.responses.retrieve(stateless.id), error => error.status === 404);
    assert.equal((await sdk.responses.retrieve(response.id)).status, 'completed');
  });
});
