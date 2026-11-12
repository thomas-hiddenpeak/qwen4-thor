// Consume real native CPU captures; this is protocol, not model evidence.
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { readFileSync } from 'node:fs';
import http from 'node:http';
import { resolve } from 'node:path';
import test from 'node:test';
import { loadSdk } from '../serve_protocol/sdk.mjs';
import { execute } from './sdk.mjs';

const root = process.env.Q4T_COMPACTION_WIRE;
assert.ok(root, 'native compaction wire required');
const capture = name => readFileSync(resolve(root, `${name}.wire`));
const { OpenAI } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
function fields(actual, expected) {
  if (Array.isArray(expected)) {
    assert.equal(actual.length, expected.length);
    expected.forEach((value, i) => fields(actual[i], value));
  } else if (expected && typeof expected === 'object') {
    for (const [key, value] of Object.entries(expected)) {
      assert.ok(Object.hasOwn(actual, key), `missing ${key}`);
      fields(actual[key], value);
    }
  } else assert.deepEqual(actual, expected);
}
async function peer(path, data, callback) {
  let count = 0;
  const server = http.createServer(async (req, res) => {
    try {
      ++count;
      assert.equal(req.method, 'POST');
      assert.equal(req.url, path);
      for await (const _ of req) {} // Drain bounded test body.
      res.setHeader('Content-Type', path.endsWith('compact')
        ? 'application/json' : 'text/event-stream');
      for (let i = 0; i < data.length; i += 7) res.write(data.subarray(i, i + 7));
      res.end();
    } catch (error) { res.destroy(error); }
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try {
    await callback(`http://127.0.0.1:${server.address().port}/v1`);
    assert.equal(count, 1);
  } finally {
    server.closeAllConnections();
    await new Promise(done => server.close(done));
  }
}

test('official stream helper preserves native compaction and continuation', async () => {
  await peer('/v1/responses', capture('automatic-events'), async baseURL => {
    const client = new OpenAI({ baseURL, apiKey: 'cpu', maxRetries: 0, timeout: 3000 });
    const stream = client.responses.stream({ model: 'cpu-model', input: 'continue',
      context_management: [{ type: 'compaction', compact_threshold: 600 }] });
    const events = [];
    for await (const event of stream) events.push(event);
    const result = await stream.finalResponse();
    fields(result, JSON.parse(capture('automatic-result')));
    events.forEach((event, i) => assert.equal(event.sequence_number, i));
    assert.equal(events.filter(e => e.type === 'response.compaction.compacting').length, 1);
    assert.equal(result.output[0].type, 'compaction');
    assert.ok(!Object.hasOwn(result.output[0], 'status'));
    assert.equal(result.output_text, '42');
  });
});

test('official compact method and acceptance bridge preserve native input window', async () => {
  await peer('/v1/responses/compact', capture('standalone-result'), async base_url => {
    const output = await execute({ action: 'compact', base_url, timeout_ms: 3000,
      request: { model: 'cpu-model', input: 'compact this' } });
    assert.equal(output.ok, true, JSON.stringify(output.error));
    assert.equal(output.exchanges.length, 1);
    assert.deepEqual(output.result, JSON.parse(capture('standalone-result')));
    assert.equal(output.result.output[1].role, 'user');
  });
});
