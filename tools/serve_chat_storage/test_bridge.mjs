import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import test from 'node:test';
import { execute } from './sdk.mjs';

async function peer(handler, work) {
  const requests = [];
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const row = { method: req.method, url: req.url, body: Buffer.concat(chunks).toString() };
    requests.push(row);
    res.setHeader('Connection', 'close');
    handler(row, res);
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try { await work(`http://127.0.0.1:${server.address().port}/v1`, requests); }
  finally { await new Promise(resolve => server.close(resolve)); }
}

test('official management bridge sends one request with exact method/body/query and retains failures', async () => {
  await peer((row, res) => {
    res.setHeader('Content-Type', 'application/json');
    if (row.url.endsWith('/missing')) {
      res.writeHead(404);
      res.end(JSON.stringify({ error: { message: 'gone', type: 'invalid_request_error', code: 'object_not_found' } }));
    } else res.end(JSON.stringify({ object: 'list', data: [], first_id: null, last_id: null, has_more: false }));
  }, async (base_url, rows) => {
    for (const [action, extra, method, path, body] of [
      ['list', { query: { metadata: null, limit: 1 } }, 'GET', '/v1/chat/completions?metadata=&limit=1', ''],
      ['retrieve', { id: 'chat_1' }, 'GET', '/v1/chat/completions/chat_1', ''],
      ['messages', { id: 'chat_1', query: { limit: 1 } }, 'GET', '/v1/chat/completions/chat_1/messages?limit=1', ''],
      ['update', { id: 'chat_1', request: { metadata: null } }, 'POST', '/v1/chat/completions/chat_1', { metadata: null }],
      ['delete', { id: 'chat_1' }, 'DELETE', '/v1/chat/completions/chat_1', ''],
    ]) {
      const result = await execute({ base_url, action, ...extra });
      assert.equal(result.ok, true, JSON.stringify(result.error));
      assert.equal(result.exchanges.length, 1);
      assert.equal(rows.at(-1).method, method);
      assert.equal(rows.at(-1).url, path);
      assert.deepEqual(body ? JSON.parse(rows.at(-1).body) : rows.at(-1).body, body);
      assert.equal(JSON.parse(Buffer.from(result.wire_base64, 'base64')).object, 'list');
      assert.equal(result.result.first_id, null);
      assert.equal(result.result.last_id, null);
    }
    const failed = await execute({ base_url, action: 'retrieve', id: 'missing' });
    assert.equal(failed.ok, false);
    assert.equal(failed.error.status, 404);
    assert.equal(failed.error.code, 'object_not_found');
    assert.equal(failed.exchanges.length, 1);
    assert.equal(rows.length, 6);
  });
});

test('official stream bridge retains every choice and wire bytes and rejects missing DONE', async () => {
  await peer((row, res) => {
    const body = JSON.parse(row.body);
    res.setHeader('Content-Type', 'text/event-stream');
    for (let i = 0; i < 2; ++i) {
      const frame = { id: 'chat_1', model: 'q4t', created: 1, object: 'chat.completion.chunk',
        choices: [{ index: i, delta: { content: '资料✅' }, finish_reason: null }] };
      res.write('data: ' + JSON.stringify(frame) + '\n\n');
      frame.choices[0].delta = {};
      frame.choices[0].finish_reason = 'stop';
      res.write('data: ' + JSON.stringify(frame) + '\n\n');
    }
    res.end(body.complete ? 'data: [DONE]\n\n' : '');
  }, async (base_url, rows) => {
    const success = await execute({ base_url, action: 'create', request: { stream: true, complete: true } });
    assert.equal(success.ok, true, JSON.stringify(success.error));
    assert.equal(success.chunks.length, 4);
    assert.equal(success.chunks[2].choices[0].delta.content, '资料✅');
    assert.equal(success.exchanges.length, 1);
    assert.ok(Buffer.from(success.wire_base64, 'base64').toString().endsWith('data: [DONE]\n\n'));
    const failed = await execute({ base_url, action: 'create', request: { stream: true, complete: false } });
    assert.equal(failed.ok, false);
    assert.equal(failed.chunks.length, 4);
    assert.match(failed.error.message, /missing or repeated Chat terminal/);
    assert.equal(rows.length, 2);
  });
});
