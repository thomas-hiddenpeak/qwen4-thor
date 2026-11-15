// Official SDK consumption of native CPU captures; no model generation here.
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { readFileSync } from 'node:fs';
import http from 'node:http';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';
import { loadSdk } from '../serve_protocol/sdk.mjs';

const directory = process.env.Q4T_PROGRAM_WIRE;
assert.ok(directory, 'native program wire required');
const { OpenAI } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
const { toResponseInputItems } = await import(pathToFileURL(resolve(
  process.env.Q4T_NODE_SDK_DIR, 'node_modules/openai/lib/responses/ResponseInputItems.mjs')));
const bytes = name => readFileSync(resolve(directory, name));
const json = name => JSON.parse(bytes(name));
function compare(actual, expected) {
  if (Array.isArray(expected)) {
    assert.ok(Array.isArray(actual));
    assert.equal(actual.length, expected.length);
    expected.forEach((value, i) => compare(actual[i], value));
  } else if (expected && typeof expected === 'object') {
    for (const [key, value] of Object.entries(expected)) {
      assert.ok(Object.hasOwn(actual, key), `missing ${key}`);
      compare(actual[key], value);
    }
  } else assert.deepEqual(actual, expected);
}
async function peer(name, stream, callback) {
  let requests = 0;
  const server = http.createServer(async (req, res) => {
    try {
      ++requests;
      assert.equal(req.method, 'POST');
      assert.equal(req.url, '/v1/responses');
      for await (const _ of req) {};
      res.setHeader('Content-Type', stream ? 'text/event-stream' : 'application/json');
      const data = bytes(name);
      for (let i = 0; i < data.length; i += 7) res.write(data.subarray(i, i + 7));
      res.end();
    } catch (error) { res.destroy(error); }
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try {
    await callback(new OpenAI({ apiKey: 'cpu', maxRetries: 0, timeout: 3000,
      baseURL: `http://127.0.0.1:${server.address().port}/v1` }));
    assert.equal(requests, 1);
  } finally {
    server.closeAllConnections();
    await new Promise(done => server.close(done));
  }
}
const request = {model: 'cpu', input: 'read project data',
  tools: [{type: 'programmatic_tool_calling'}]};

test('program and child JSON survive official continuation projection', async () => {
  await peer('pending.json', false, async sdk => {
    const response = await sdk.responses.create(request);
    compare(response, json('pending.json'));
    const [program, call] = toResponseInputItems(response.output);
    assert.equal(program.type, 'program');
    assert.ok(program.fingerprint && program.code);
    assert.ok(!Object.hasOwn(program, 'status'));
    assert.equal(call.type, 'function_call');
    assert.deepEqual(call.caller, {type: 'program', caller_id: program.call_id});
    assert.equal(call.name, 'read');
    assert.deepEqual(JSON.parse(call.arguments), {path: 'docs/MODEL.md'});
  });
});

test('program output SSE completes before real final message', async () => {
  await peer('response.wire', true, async sdk => {
    const stream = sdk.responses.stream(request);
    const events = [];
    for await (const event of stream) events.push(event);
    const response = await stream.finalResponse();
    compare(response, json('response.json'));
    events.forEach((event, i) => assert.equal(event.sequence_number, i));
    assert.equal(response.output[0].type, 'program_output');
    assert.equal(response.output[0].result, '42');
    assert.equal(response.output[0].status, 'completed');
    assert.equal(response.output_text, 'The project value doubled is 42.');
    assert.equal(events.filter(e => e.type === 'response.completed').length, 1);
    const done = events.findIndex(e => e.type === 'response.output_item.done' &&
      e.item.type === 'program_output');
    const text = events.findIndex(e => e.type === 'response.output_text.delta');
    assert.ok(done >= 0 && text > done);
    compare(toResponseInputItems(response.output)[0], response.output[0]);
  });
});
