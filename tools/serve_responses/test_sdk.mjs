// Consume the actual C++ Responses state machine's captured wire over HTTP.
// This is a CPU protocol fixture, not evidence of model generation.
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { readFileSync } from 'node:fs';
import http from 'node:http';
import { resolve } from 'node:path';
import test from 'node:test';
import { loadSdk } from '../serve_protocol/sdk.mjs';

const { OpenAI } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
assert.ok(process.env.Q4T_RESPONSES_FIXTURES, 'C++ fixtures required');
const root = resolve(process.env.Q4T_RESPONSES_FIXTURES);
const fixture = (name, suffix) => readFileSync(resolve(root, `${name}.${suffix}`));
const expected = name => JSON.parse(fixture(name, 'json'));

function compareWire(actual, wanted) {
  // The SDK adds parsed/output_text convenience properties. Every property
  // actually emitted by the native producer must still match exactly.
  if (Array.isArray(wanted)) {
    assert.ok(Array.isArray(actual));
    assert.equal(actual.length, wanted.length);
    wanted.forEach((value, index) => compareWire(actual[index], value));
  } else if (wanted && typeof wanted === 'object') {
    assert.ok(actual && typeof actual === 'object');
    for (const [key, value] of Object.entries(wanted)) {
      assert.ok(Object.hasOwn(actual, key), `missing ${key}`);
      compareWire(actual[key], value);
    }
  } else {
    assert.deepEqual(actual, wanted);
  }
}

async function peer(name, callback, mutate = value => value) {
  let requests = 0;
  const server = http.createServer(async (req, res) => {
    try {
      ++requests;
      assert.equal(req.method, 'POST');
      assert.equal(req.url, '/v1/responses');
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks));
      assert.equal(body.store, false);
      const wire = mutate(fixture(name, body.stream ? 'wire' : 'json'));
      res.setHeader('Content-Type', body.stream ? 'text/event-stream' : 'application/json');
      const bytes = Buffer.from(wire);
      for (let i = 0; i < bytes.length; i += 7) res.write(bytes.subarray(i, i + 7));
      res.end();
    } catch (error) {
      res.destroy(error);
    }
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try {
    const sdk = new OpenAI({ apiKey: 'cpu-fixture', maxRetries: 0, timeout: 3000,
      baseURL: `http://127.0.0.1:${server.address().port}/v1` });
    await callback(sdk);
    assert.equal(requests, 1);
  } finally {
    server.closeAllConnections();
    await new Promise(resolveClose => server.close(resolveClose));
  }
}
const request = { model: 'org/alias', input: 'CPU protocol fixture', store: false };

test('official create consumes native JSON, reasoning and probabilities', async () => {
  await peer('text', async sdk => {
    const response = await sdk.responses.create(request);
    compareWire(response, expected('text'));
    assert.equal(response.output_text, '答案是42✅');
  });
});

for (const name of ['text', 'tools', 'partial', 'failure']) {
  test(`official stream accumulates native ${name} lifecycle`, async () => {
    await peer(name, async sdk => {
      const stream = sdk.responses.stream(request);
      const events = [];
      for await (const event of stream) events.push(event);
      const final = await stream.finalResponse();
      compareWire(final, expected(name));
      assert.equal(events[0].type, 'response.created');
      events.forEach((event, index) => assert.equal(event.sequence_number, index));
      const status = { text: 'completed', tools: 'completed',
        partial: 'incomplete', failure: 'failed' }[name];
      assert.equal(events.at(-1).type, `response.${status}`);
      assert.equal(final.status, status);
      if (name === 'partial' || name === 'failure') {
        assert.ok(!events.some(event => event.type === 'response.function_call_arguments.done'));
        assert.notEqual(final.status, 'completed');
      }
      if (name === 'tools') {
        assert.deepEqual(JSON.parse(final.output[0].arguments), { path: 'docs/STATUS.md' });
        assert.equal(final.output[1].input, 'STATUS\n末行');
      }
    });
  });
}

test('official streaming create consumes identical native events', async () => {
  await peer('text', async sdk => {
    const events = [];
    for await (const event of await sdk.responses.create({ ...request, stream: true }))
      events.push(event);
    assert.equal(events.at(-1).type, 'response.completed');
    compareWire(events.at(-1).response, expected('text'));
  });
});

test('official helper rejects changing response identity', async () => {
  await peer('text', async sdk => {
    const stream = sdk.responses.stream(request);
    await assert.rejects(stream.finalResponse(), /identity changed/i);
  }, data => data.toString().replace(/("id":"resp_text")(?=[^]*response.in_progress)/,
    '"id":"resp_different"'));
});
