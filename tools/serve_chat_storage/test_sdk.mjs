// Official SDK against native management and real isolated storage over HTTP.
// Creation is explicitly a CPU synthetic fixture, not model acceptance.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { readFileSync, existsSync, mkdirSync, rmSync, openSync, closeSync } from 'node:fs';
import { resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import test from 'node:test';
import { loadSdk } from '../serve_protocol/sdk.mjs';

const { OpenAI } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
assert.ok(process.env.Q4T_HOST_TESTS && process.env.Q4T_CHAT_STORAGE_OUTPUT);
const executable = resolve(process.env.Q4T_HOST_TESTS);
const output = resolve(process.env.Q4T_CHAT_STORAGE_OUTPUT);
let launches = 0;
async function peer(name, work) {
  const root = resolve(output, name);
  mkdirSync(root, { recursive: true });
  const ready = resolve(root, 'ready.json');
  rmSync(ready, { force: true });
  const log = openSync(resolve(root, `process-${++launches}.log`), 'w');
  const child = spawn(executable, ['--chat-storage-api-fixture', root], { stdio: ['ignore', log, log] });
  closeSync(log);
  const exited = once(child, 'exit');
  let base;
  try {
    const deadline = Date.now() + 5000;
    while (!existsSync(ready) && child.exitCode === null && Date.now() < deadline) await delay(10);
    assert.ok(existsSync(ready), 'native fixture readiness');
    const { port, pid } = JSON.parse(readFileSync(ready, 'utf8'));
    assert.equal(pid, child.pid);
    base = `http://127.0.0.1:${port}`;
    const sdk = key => new OpenAI({ apiKey: key, baseURL: base + '/v1', maxRetries: 0, timeout: 5000 });
    return await work(sdk('cpu-client-a'), sdk('cpu-client-b'), base);
  } finally {
    try {
      if (base && child.exitCode === null)
        await fetch(base + '/__fixture/stop', { method: 'POST', headers: { Authorization: 'Bearer cpu-client-a' }, signal: AbortSignal.timeout(3000) });
      assert.deepEqual(await Promise.race([exited, delay(5000).then(() => { throw new Error('fixture exit timeout'); })]), [0, null]);
    } finally {
      if (child.exitCode === null) child.kill('SIGKILL');
    }
  }
}
const request = extra => ({ model: 'q4t', store: true, messages: [{ role: 'user', content: '项目资料' }], ...extra });

test('official stored Chat create/update/retrieve/delete survives process restart', async () => {
  let original;
  await peer('durable', async sdk => {
    original = await sdk.chat.completions.create(request({ n: 2, metadata: { project: 'q4t' } }));
    assert.equal(original.choices.length, 2);
    assert.equal(original.usage.completion_tokens, 6);
    assert.equal(original.metadata.project, 'q4t');
    original = await sdk.chat.completions.update(original.id, { metadata: { project: '运行记录' } });
  });
  await peer('durable', async sdk => {
    assert.deepEqual(await sdk.chat.completions.retrieve(original.id), original);
    assert.equal((await sdk.chat.completions.update(original.id, { metadata: null })).metadata, null);
    assert.deepEqual(await sdk.chat.completions.delete(original.id), { id: original.id, object: 'chat.completion.deleted', deleted: true });
    await assert.rejects(sdk.chat.completions.retrieve(original.id), error => error.status === 404);
  });
});

test('official filtered auto-pagination and original messages retain tool/media history', async () => {
  await peer('pagination', async sdk => {
    const messages = [
      { role: 'developer', content: 'Read project records' },
      { role: 'user', name: 'reader', content: [{ type: 'text', text: '资料✅' }, { type: 'image_url', image_url: { url: 'data:image/png;base64,AQ==', detail: 'auto' } }] },
      { role: 'assistant', content: null, tool_calls: [{ id: 'call_doc', type: 'function', function: { name: 'read_document', arguments: '{"path":"docs/MODEL.md"}' } }] },
      { role: 'tool', tool_call_id: 'call_doc', content: 'real fixture document' },
    ];
    const expected = [];
    for (let i = 0; i < 5; ++i) {
      const result = await sdk.chat.completions.create(request({ messages, metadata: { project: i % 2 ? 'skip' : 'q4t', topic: '运行记录' } }));
      if (!(i % 2)) expected.push(result);
    }
    const ids = expected.sort((a, b) => a.created - b.created ||
      (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)).map(c => c.id);
    const found = [];
    for await (const item of sdk.chat.completions.list({ limit: 1, order: 'asc', model: 'q4t', metadata: { project: 'q4t', topic: '运行记录' } })) found.push(item.id);
    assert.deepEqual(found, ids);
    const saved = [];
    for await (const item of sdk.chat.completions.messages.list(ids[0], { limit: 1, order: 'asc' })) saved.push(item);
    assert.deepEqual(saved.map(m => m.role), messages.map(m => m.role));
    assert.equal(saved[1].content, '资料✅');
    assert.deepEqual(saved[1].content_parts, messages[1].content);
    assert.deepEqual(saved[2].tool_calls, messages[2].tool_calls);
    assert.equal(saved[3].tool_call_id, 'call_doc');
    const descending = await sdk.chat.completions.messages.list(ids[0], { order: 'desc', limit: 1 });
    assert.equal(descending.data[0].id, saved[3].id);
    assert.equal(descending.has_more, true);
    assert.equal((await sdk.chat.completions.list({ metadata: null, model: 'missing' })).data.length, 0);
  });
});

test('official SDK stream leaves a complete retrievable multi-choice record', async () => {
  await peer('stream', async sdk => {
    const stream = await sdk.chat.completions.create(request({ stream: true, n: 2, metadata: { transport: 'stream' } }));
    const texts = ['', ''];
    const finishes = [];
    let id;
    for await (const chunk of stream) {
      id = chunk.id;
      for (const choice of chunk.choices) {
        texts[choice.index] += choice.delta.content || '';
        if (choice.finish_reason) finishes.push(choice.finish_reason);
      }
    }
    assert.deepEqual(finishes, ['stop', 'stop']);
    const saved = await sdk.chat.completions.retrieve(id);
    assert.deepEqual(saved.choices.map(c => c.message.content), texts);
    assert.equal(saved.metadata.transport, 'stream');
    assert.equal(saved.usage.completion_tokens, 6);
  });
});

test('official authentication scopes and errors do not expose or mutate other clients', async () => {
  await peer('errors', async (sdk, other) => {
    const stored = await sdk.chat.completions.create(request({}));
    for (const action of [() => other.chat.completions.retrieve(stored.id),
      () => other.chat.completions.messages.list(stored.id),
      () => other.chat.completions.update(stored.id, { metadata: { bad: 'foreign' } }),
      () => other.chat.completions.delete(stored.id)])
      await assert.rejects(action(), error => error.status === 404);
    assert.equal((await other.chat.completions.list()).data.length, 0);
    await assert.rejects(sdk.chat.completions.list({ limit: 0 }), error => error.status === 400 && error.param === 'limit');
    await assert.rejects(sdk.chat.completions.update(stored.id, { metadata: { bad: 1 } }), error => error.status === 400 && error.param.startsWith('metadata'));
    assert.equal((await sdk.chat.completions.retrieve(stored.id)).metadata, null);
    const unretained = await sdk.chat.completions.create(request({ store: null, metadata: { response: 'only' } }));
    assert.equal(unretained.metadata.response, 'only');
    await assert.rejects(sdk.chat.completions.retrieve(unretained.id), error => error.status === 404);
  });
});
