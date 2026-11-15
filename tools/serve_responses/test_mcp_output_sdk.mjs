// Consume native C++ CPU captures with the official SDK; no model claim.
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { readFileSync } from 'node:fs';
import http from 'node:http';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';
import { loadSdk } from '../serve_protocol/sdk.mjs';

assert.ok(process.env.Q4T_MCP_OUTPUT, 'native MCP captures required');
const root = resolve(process.env.Q4T_MCP_OUTPUT);
const { OpenAI } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
const { toResponseInputItems } = await import(pathToFileURL(resolve(
  process.env.Q4T_NODE_SDK_DIR,
  'node_modules/openai/lib/responses/ResponseInputItems.mjs')));
const bytes = name => readFileSync(resolve(root, name));
const json = name => JSON.parse(bytes(name));
const request = {model: 'mcp-output-fixture', input: 'read project data',
  tools: [{type: 'mcp', server_label: 'project',
    server_url: 'https://project.example/mcp'}]};

async function peer(name, stream, callback) {
  let requests = 0;
  const server = http.createServer(async (req, res) => {
    try {
      ++requests;
      assert.equal(req.method, 'POST');
      assert.equal(req.url, '/v1/responses');
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks));
      assert.deepEqual(body.tools, request.tools);
      res.setHeader('Content-Type', stream ? 'text/event-stream' : 'application/json');
      const data = bytes(name);
      for (let i = 0; i < data.length; i += 7) res.write(data.subarray(i, i + 7));
      res.end();
    } catch (error) { res.destroy(error); }
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try {
    const sdk = new OpenAI({apiKey: 'cpu', timeout: 3000, maxRetries: 0,
      baseURL: `http://127.0.0.1:${server.address().port}/v1`});
    await callback(sdk);
    assert.equal(requests, 1);
  } finally {
    server.closeAllConnections();
    await new Promise(done => server.close(done));
  }
}

test('MCP approval JSON retains the official continuation contract', async () => {
  await peer('approval.json', false, async sdk => {
    const response = await sdk.responses.create(request);
    assert.deepEqual(response.output, json('approval.json').output);
    const [approval] = toResponseInputItems(response.output);
    assert.equal(approval.type, 'mcp_approval_request');
    assert.equal(approval.id, 'mcpr_x');
    assert.deepEqual(JSON.parse(approval.arguments), {path: 'docs/MODEL.md'});
    assert.ok(!Object.hasOwn(approval, 'status'));
    assert.equal(response.status, 'completed');
  });
});

test('MCP SSE keeps incremental arguments and list/call/answer order', async () => {
  await peer('response.wire', true, async sdk => {
    const stream = sdk.responses.stream(request);
    const events = [];
    for await (const event of stream) events.push(event);
    const response = await stream.finalResponse();
    const expected = json('response.json').output;
    // The SDK enriches plain output_text with parsed:null. MCP items must
    // still match every native field exactly; no generic key stripping.
    for (const item of expected)
      if (item.type === 'message')
        for (const part of item.content)
          if (part.type === 'output_text') part.parsed = null;
    assert.deepEqual(response.output, expected);
    assert.equal(response.output_text, 'The project document was retrieved.');
    events.forEach((event, i) => assert.equal(event.sequence_number, i));
    const deltas = events.filter(e => e.type === 'response.mcp_call_arguments.delta');
    assert.equal(deltas.length, 2);
    assert.deepEqual(JSON.parse(deltas.map(e => e.delta).join('')),
      {path: 'docs/MODEL.md'});
    const done = events.findIndex(e => e.type === 'response.mcp_call_arguments.done');
    const called = events.findIndex(e => e.type === 'response.mcp_call.completed');
    const text = events.findIndex(e => e.type === 'response.output_text.delta');
    assert.ok(done >= 0 && called > done && text > called);
    const [catalog, call] = toResponseInputItems(response.output);
    assert.equal(catalog.type, 'mcp_list_tools');
    assert.equal(catalog.tools[0].input_schema.type, 'object');
    assert.ok(!Object.hasOwn(catalog, 'status'));
    assert.equal(call.type, 'mcp_call');
    assert.equal(call.status, 'completed');
    assert.equal(call.output, 'Project documentation fixture');
  });
});
