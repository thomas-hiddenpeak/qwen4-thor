import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import test from 'node:test';
import { combineChunks, execute } from './sdk.mjs';

const identity = { id: 'cmpl-cpu', object: 'text_completion', created: 42, model: 'org/alias' };
const usage = { prompt_tokens: 3, completion_tokens: 6, total_tokens: 9 };
const probability = { tokens: ['你', '好'], token_logprobs: [null, -0.4],
  top_logprobs: [null, { '好': -0.4 }], text_offset: [0, 1] };
const choice = { index: 0, text: '你好', logprobs: probability, finish_reason: 'length' };

async function peer(handler, call) {
  let requests = 0;
  const server = http.createServer(async (req, res) => {
    ++requests;
    const parts = [];
    for await (const part of req) parts.push(part);
    handler(req, res, Buffer.concat(parts).toString('utf8'));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try {
    const value = await call(`http://127.0.0.1:${server.address().port}/v1`);
    assert.equal(requests, 1);
    return value;
  } finally { server.closeAllConnections(); await new Promise(r => server.close(r)); }
}

test('official SDK preserves batch token IDs and actual legacy probability shape', async () => {
  const request = { model: 'org/alias', prompt: [[4, 5], []], echo: true, max_tokens: 0,
    n: 2, logprobs: 3, seed: -17, suffix: 'tail' };
  const result = await peer((req, res, body) => {
    assert.equal(req.url, '/v1/completions');
    assert.equal(req.method, 'POST');
    assert.deepEqual(JSON.parse(body), request);
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ ...identity, choices: [choice], usage }));
  }, base_url => execute({ base_url, action: 'completion', request }));
  assert.equal(result.ok, true);
  assert.deepEqual(result.result.choices[0].logprobs, probability);
  assert.equal(result.sdk_version, '7.47.0');
});

test('official SDK consumes split SSE, per-choice finish and aggregate usage', async () => {
  const chunks = [
    { ...identity, choices: [{ ...choice, text: '你', finish_reason: null,
      logprobs: { tokens: ['你'], token_logprobs: [null], top_logprobs: [null], text_offset: [0] } }], usage: null },
    { ...identity, choices: [{ ...choice, text: '好', logprobs: {
      tokens: ['好'], token_logprobs: [-0.4], top_logprobs: [{ '好': -0.4 }], text_offset: [1] } }], usage: null },
    { ...identity, choices: [], usage, obfuscation: 'abcd' },
  ];
  const wire = chunks.map(c => `data: ${JSON.stringify(c)}\n\n`).join('') + 'data: [DONE]\n\n';
  const result = await peer((_req, res) => {
    res.setHeader('Content-Type', 'text/event-stream');
    const bytes = Buffer.from(wire);
    for (let i = 0; i < bytes.length; i += 3) res.write(bytes.subarray(i, i + 3));
    res.end();
  }, base_url => execute({ base_url, action: 'completion', request: {
    model: 'org/alias', prompt: '你好', stream: true, stream_options: { include_usage: true } } }));
  assert.equal(result.ok, true);
  assert.deepEqual(result.result.choices, [choice]);
  assert.deepEqual(result.result.usage, usage);
  assert.equal(Buffer.from(result.wire_base64, 'base64').toString(), wire);
});

test('official model SDK encodes one identifier segment and does not follow URLs', async () => {
  const model = 'org/模型%id';
  const result = await peer((req, res) => {
    assert.equal(req.url, '/v1/models/' + encodeURIComponent(model));
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ id: model, object: 'model', created: 42, owned_by: 'q4t' }));
  }, base_url => execute({ base_url, action: 'retrieve', model }));
  assert.equal(result.ok, true);
  assert.equal(result.result.id, model);
});

test('official Models list exposes objects without invented pagination', async () => {
  const data = [{ id: 'base', object: 'model', created: 42, owned_by: 'q4t' }];
  const result = await peer((req, res) => {
    assert.equal(req.url, '/v1/models');
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ object: 'list', data }));
  }, base_url => execute({ base_url, action: 'models' }));
  assert.equal(result.ok, true);
  assert.deepEqual(result.result.data, data);
});

test('official DELETE preserves typed denial; request failures never retry', async () => {
  for (const status of [403, 503]) {
    const result = await peer((req, res) => {
      assert.equal(req.method, 'DELETE');
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: { message: 'denied', type: 'permission_error',
        code: 'model_not_deletable', param: 'model' } }));
    }, base_url => execute({ base_url, action: 'delete', model: 'base' }));
    assert.equal(result.ok, false);
    assert.equal(result.error.status, status);
    assert.equal(result.error.code, 'model_not_deletable');
  }
});

test('stream validator rejects missing terminal, duplicate finish and early usage', () => {
  const finished = { ...identity, choices: [choice] };
  assert.throws(() => combineChunks([finished], ''), /DONE/);
  assert.throws(() => combineChunks([finished, finished], 'data: [DONE]'), /after finish/);
  assert.throws(() => combineChunks([{ ...identity, choices: [], usage }],
    'data: [DONE]'), /early usage/);
  assert.throws(() => combineChunks([finished], 'data: [DONE]\n\nextra'), /after DONE/);
});
