// The official SDK calls the actual native handler and persistent worker over
// loopback HTTP. No model, TLS, or production authentication is claimed here.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { readFileSync, existsSync, mkdirSync, rmSync, openSync, closeSync } from 'node:fs';
import { resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import test from 'node:test';
import { loadSdk } from '../serve_protocol/sdk.mjs';

const { OpenAI } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
assert.ok(process.env.Q4T_HOST_TESTS && process.env.Q4T_CONVERSATION_OUTPUT);
const executable = resolve(process.env.Q4T_HOST_TESTS);
const output = resolve(process.env.Q4T_CONVERSATION_OUTPUT);
let launches = 0;
async function peer(name, work) {
  const root = resolve(output, name);
  mkdirSync(root, { recursive: true });
  const ready = resolve(root, 'ready.json');
  rmSync(ready, { force: true });
  const log = openSync(resolve(root, `process-${++launches}.log`), 'w');
  const child = spawn(executable, ['--conversation-api-fixture', root], { stdio: ['ignore', log, log] });
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
      const code = await Promise.race([exited, delay(5000).then(() => { throw new Error('native fixture exit timeout'); })]);
      assert.deepEqual(code, [0, null]);
    } finally {
      if (child.exitCode === null) child.kill('SIGKILL');
    }
  }
}

test('official conversation CRUD survives a native worker/process restart', async () => {
  let id;
  await peer('durable', async sdk => {
    const created = await sdk.conversations.create({ metadata: { topic: '项目资料' }, items: [{ role: 'user', content: 'remember 42' }] });
    id = created.id;
    assert.equal(created.object, 'conversation');
    assert.ok(created.created_at > 0);
    const updated = await sdk.conversations.update(id, { metadata: { topic: '运行记录' } });
    assert.equal(updated.metadata.topic, '运行记录');
  });
  await peer('durable', async sdk => {
    const restored = await sdk.conversations.retrieve(id);
    assert.equal(restored.metadata.topic, '运行记录');
    const page = await sdk.conversations.items.list(id);
    assert.equal(page.data[0].content[0].text, 'remember 42');
    const deleted = await sdk.conversations.delete(id);
    assert.equal(deleted.object, 'conversation.deleted');
    assert.equal(deleted.deleted, true);
    await assert.rejects(sdk.conversations.retrieve(id), error => error.status === 404);
  });
});

test('official item append and auto-pagination preserve input order', async () => {
  await peer('pagination', async sdk => {
    const conversation = await sdk.conversations.create();
    const appended = await sdk.conversations.items.create(conversation.id, { items: [0, 1, 2].map(i => ({ role: 'user', content: `turn-${i}` })) });
    assert.equal(appended.data.length, 3);
    assert.equal(appended.has_more, false);
    const found = [];
    for await (const item of sdk.conversations.items.list(conversation.id, { limit: 1, order: 'asc' })) found.push(item.content[0].text);
    assert.deepEqual(found, ['turn-0', 'turn-1', 'turn-2']);
    const middle = appended.data[1].id;
    const item = await sdk.conversations.items.retrieve(middle, { conversation_id: conversation.id });
    assert.equal(item.content[0].text, 'turn-1');
    const remaining = await sdk.conversations.items.delete(middle, { conversation_id: conversation.id });
    assert.equal(remaining.id, conversation.id);
    assert.equal(remaining.object, 'conversation');
    await assert.rejects(sdk.conversations.items.retrieve(middle, { conversation_id: conversation.id }), error => error.status === 404);
  });
});

test('official tools can be recorded before results without executing them', async () => {
  await peer('tools', async sdk => {
    const conversation = await sdk.conversations.create({ items: [{ type: 'function_call', call_id: 'call_doc', name: 'read_document', arguments: '{', status: 'in_progress' }] });
    await sdk.conversations.items.create(conversation.id, { items: [{ type: 'function_call_output', call_id: 'call_doc', output: 'read result' }] });
    const items = await sdk.conversations.items.list(conversation.id, { order: 'asc' });
    assert.equal(items.data[0].arguments, '{');
    assert.equal(items.data[0].status, 'in_progress');
    assert.equal(items.data[1].call_id, 'call_doc');
    assert.equal(items.data[1].output, 'read result');
  });
});

test('official include arrays affect views without removing retained input', async () => {
  await peer('include', async sdk => {
    const image = 'data:image/png;base64,AQ==';
    const conversation = await sdk.conversations.create({ items: [{ role: 'user', content: [{ type: 'input_image', detail: 'auto', image_url: image }] }] });
    const hidden = await sdk.conversations.items.list(conversation.id);
    assert.equal(hidden.data[0].content[0].image_url, null);
    const visible = await sdk.conversations.items.list(conversation.id, { include: ['message.input_image.image_url'] });
    assert.equal(visible.data[0].content[0].image_url, image);
    const another = await sdk.conversations.create({ items: [{ id: hidden.data[0].id }] });
    const referenced = await sdk.conversations.items.list(another.id, { include: ['message.input_image.image_url'] });
    assert.equal(referenced.data[0].content[0].image_url, image);
  });
});

test('official errors preserve client isolation and reject malformed fields', async () => {
  await peer('errors', async (sdk, other) => {
    const conversation = await sdk.conversations.create({ items: [{ role: 'user', content: 'private' }] });
    await assert.rejects(other.conversations.retrieve(conversation.id), error => error.status === 404);
    await assert.rejects(other.conversations.delete(conversation.id), error => error.status === 404);
    const item = (await sdk.conversations.items.list(conversation.id)).data[0];
    await assert.rejects(other.conversations.create({ items: [{ id: item.id }] }), error => error.status === 404);
    await assert.rejects(sdk.conversations.items.list(conversation.id, { limit: 0 }), error => error.status === 400 && error.param === 'limit');
    await assert.rejects(sdk.conversations.items.create(conversation.id, { items: [{ type: 'function_call', call_id: 'call_1', arguments: '{}' }] }), error => error.status === 400 && error.param.startsWith('items['));
    assert.equal((await sdk.conversations.items.list(conversation.id)).data.length, 1);
  });
});
