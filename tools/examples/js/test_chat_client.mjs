// Actual pinned SDK over in-memory Fetch responses; no HTTP/model execution.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { loadSdk } from '../../serve_protocol/sdk.mjs';
import { ChatClient, ProtocolError, parseJson, parseJsonWire, parseSseWire,
  prepareToolCalls, appendToolResults, appendFinal, makeRequest, validateCapabilities } from './chat_client.mjs';

const { OpenAI } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
const MODEL = 'qwen3.8-flash-next', ID = 'js-unit', HEX = 'a'.repeat(64);
const COUNTS = { prompt_tokens: 10, completion_tokens: 2, total_tokens: 12 };
const request = stream => makeRequest([{ role: 'user', content: 'q' }], {
  stream, request_id: ID, cancel_token: HEX, max_completion_tokens: 128 });
function event(delta, finish = null) {
  return { id: ID, model: MODEL, object: 'chat.completion.chunk', created: 42,
    choices: [{ index: 0, delta, finish_reason: finish, logprobs: null }], usage: null };
}
function streamEvents() {
  return [event({ role: 'assistant', content: '' }), event({ content: '中文710003' }),
    event({}, 'stop'), { id: ID, model: MODEL, object: 'chat.completion.chunk', created: 42,
      choices: [], usage: COUNTS }];
}
function encode(events, done = true) {
  return Buffer.from(': keep-alive\r\n\r\n' + events.map(e => 'data: ' + JSON.stringify(e) + '\r\n\r\n').join('') +
    (done ? 'data: [DONE]\r\n\r\n' : ''));
}
function jsonResponse(message = { role: 'assistant', content: '中文710003' }, finish = 'stop') {
  return { id: ID, model: MODEL, created: 42, object: 'chat.completion', usage: COUNTS,
    choices: [{ index: 0, message, finish_reason: finish, logprobs: null }] };
}
function client(bytes, { status = 200, stream = false, timeoutMs = 5000 } = {}) {
  const requests = [];
  return { requests, api: new ChatClient(OpenAI, { timeoutMs,
    fetchImpl: async (url, init) => {
      requests.push({ url: String(url), ...init });
      // Split every byte, including UTF-8 scalars and SSE/JSON delimiters.
      return new Response(new ReadableStream({ start(controller) {
        for (let i = 0; i < bytes.length; i++) controller.enqueue(bytes.subarray(i, i + 1));
        controller.close();
      } }), { status, headers: { 'content-type': stream ? 'text/event-stream' : 'application/json', 'x-request-id': 'trace-unit' } });
    } }) };
}
function toolMessage() {
  return { role: 'assistant', content: null, tool_calls: ['a', 'b'].map(id => ({ id,
    type: 'function', function: { name: 'read', arguments: JSON.stringify({ path: 'docs/MODEL.md', start_line: 1, line_count: 8 }) } })) };
}

test('JSON and SSE SDK interpretation agrees with strict captured bytes', async () => {
  const raw = Buffer.from(JSON.stringify(jsonResponse())), sse = encode(streamEvents());
  const left = client(raw), right = client(sse, { stream: true });
  const chunks = [];
  const a = await left.api.chat(request(false)), b = await right.api.chat(request(true), { sink: b => chunks.push(b) });
  assert.equal(a.message.content, '中文710003'); assert.deepEqual(a.message, b.message);
  assert.deepEqual(a.usage, b.usage); assert.deepEqual(Buffer.concat(chunks), sse);
  assert.deepEqual(b.raw, sse); assert.equal(right.requests.length, 1);
  assert.equal(JSON.parse(right.requests[0].body).request_id, ID);
  assert.equal(right.requests[0].redirect, 'error');
});

test('SDK models discovery uses one GET with no unsupported query', async () => {
  const models = { object: 'list', data: [{ id: MODEL, object: 'model', created: 42, owned_by: 'q4t' }] };
  const run = client(Buffer.from(JSON.stringify(models)));
  assert.deepEqual((await run.api.models()).models, models);
  assert.equal(new URL(run.requests[0].url).search, ''); assert.equal(run.requests[0].method, 'GET');
});

test('capability validation binds protocol and template identities', async () => {
  const file = new URL('../../../docs/evidence/serve-preflight-20261103/execution-final/business/candidate-mtp/capabilities.json', import.meta.url);
  const caps = JSON.parse(JSON.parse(readFileSync(fileURLToPath(file), 'utf8')).raw);
  assert.equal(validateCapabilities(caps, MODEL, caps.protocol_source_sha256).model, MODEL);
  assert.throws(() => validateCapabilities(caps, MODEL, '0'.repeat(64)), ProtocolError);
  const run = client(Buffer.from(JSON.stringify(caps)));
  assert.equal((await run.api.capabilities()).model, MODEL);
});

for (const [name, raw] of [
  ['duplicate object keys', '{"a":1,"a":2}'], ['nested duplicate keys', '{"b":{"a":1,"a":2}}'],
  ['near integer rounding', '{"n":2.0000000000000000001}'], ['unsafe integer', '{"n":9007199254740992}'],
  ['nonfinite number', '{"n":1e999}'], ['unpaired surrogate', '{"s":"\\ud800"}'],
  ['trailing document', '{}{}'], ['trailing comma', '{"a":1,}']]) {
  test('strict JSON rejects ' + name, () => assert.throws(() => parseJson(raw)));
}
test('strict JSON accepts exact integral exponents and harmless prototype keys', () => {
  assert.equal(parseJson('{"n":20e-1}').n, 2);
  assert.equal(parseJson('{"__proto__":{"x":true}}').__proto__.x, true);
  assert.equal({}.x, undefined);
});

const corruptions = {
  'missing DONE': events => encode(events, false),
  'missing usage': events => encode(events.slice(0, -1)),
  'duplicate usage': events => encode([...events, events.at(-1)]),
  'choice after finish': events => encode([...events.slice(0, 3), event({ content: 'late' }), events[3]]),
  'changed ID': events => { events[1].id = 'other'; return encode(events); },
  'changed created': events => { events[1].created++; return encode(events); },
  'changed model': events => { events[1].model = 'other'; return encode(events); },
  'mixed reasoning fields': events => { events[1].choices[0].delta = { reasoning: 'x', reasoning_content: 'x' }; return encode(events); },
  'duplicate role': events => { events[1].choices[0].delta.role = 'assistant'; return encode(events); },
  'data after DONE ignored by SDK': events => Buffer.concat([encode(events), encode([event({ content: 'late' })], false)]),
  'partial last frame': events => encode(events).subarray(0, -1),
  'usage arithmetic': events => { events.at(-1).usage = { ...COUNTS, total_tokens: 999 }; return encode(events); },
};
for (const [name, mutate] of Object.entries(corruptions)) {
  test('reject complete-looking SSE with ' + name, async () => {
    const raw = mutate(streamEvents()), run = client(raw, { stream: true });
    await assert.rejects(run.api.chat(request(true)), error => {
      assert.ok(error instanceof Error); assert.deepEqual(error.capture[0].raw, raw); return true;
    });
    assert.equal(run.requests.length, 1);
  });
}

test('split tool deltas produce one complete validated batch', async () => {
  const msg = toolMessage(), events = [event({ role: 'assistant', content: '' })];
  for (const [index, call] of msg.tool_calls.entries()) {
    events.push(event({ tool_calls: [{ index, id: call.id, type: call.type, function: { name: 'read', arguments: call.function.arguments.slice(0, 5) } }] }));
    events.push(event({ tool_calls: [{ index, function: { arguments: call.function.arguments.slice(5) } }] }));
  }
  events.push(event({}, 'tool_calls'), { ...streamEvents().at(-1) });
  const run = client(encode(events), { stream: true }), result = await run.api.chat(request(true));
  const checked = [];
  const planned = prepareToolCalls(result, { read: args => { assert.equal(args.path, 'docs/MODEL.md'); checked.push(args); } });
  assert.equal(checked.length, 2); assert.deepEqual(planned.map(p => p.call_id), ['a', 'b']);
  const history = appendToolResults(request(true).messages, result, { b: 'second', a: 'first' });
  assert.deepEqual(history.slice(-2).map(m => m.tool_call_id), ['a', 'b']);
  assert.throws(() => appendToolResults([], result, { a: 'only' }), ProtocolError);
  assert.throws(() => appendFinal([], result), ProtocolError);
});

test('whole batch validation rejects a later unknown tool before execution', () => {
  const msg = toolMessage(); msg.tool_calls[1].function.name = 'unregistered';
  const result = parseJsonWire(Buffer.from(JSON.stringify(jsonResponse(msg, 'tool_calls')))).result;
  let executions = 0;
  assert.throws(() => { const plan = prepareToolCalls(result, { read: () => {} });
    for (const _ of plan) executions++; }, ProtocolError);
  assert.equal(executions, 0);
});

for (const [name, mutate] of [
  ['duplicate ID', msg => { msg.tool_calls[1].id = 'a'; }],
  ['incomplete argument', msg => { msg.tool_calls[0].function.arguments = '{"a":'; }],
  ['rounded argument', msg => { msg.tool_calls[0].function.arguments = '{"start_line":1.0000000000000000001}'; }],
  ['wrong terminal', msg => { msg.tool_calls = []; }]]) {
  test('tool completion rejects ' + name, () => {
    const msg = toolMessage(); mutate(msg);
    assert.throws(() => parseJsonWire(Buffer.from(JSON.stringify(jsonResponse(msg, 'tool_calls')))));
  });
}

for (const status of [400, 503]) {
  test('SDK HTTP ' + status + ' error retains code/param with zero retries', async () => {
    const raw = Buffer.from(JSON.stringify({ error: { message: 'invalid request', type: 'invalid_request_error', code: 'invalid_request', param: 'unknown' } }));
    const run = client(raw, { status });
    await assert.rejects(run.api.chat(request(false)), error => {
      assert.ok(error instanceof OpenAI.APIError); assert.equal(error.status, status);
      assert.equal(error.code, 'invalid_request'); assert.equal(error.param, 'unknown'); return true;
    });
    assert.equal(run.requests.length, 1);
  });
}

test('an open stream respects the total deadline and cannot complete or dispatch', async () => {
  let submissions = 0;
  const api = new ChatClient(OpenAI, { timeoutMs: 50, fetchImpl: async (_, init) => {
    submissions++;
    return new Response(new ReadableStream({ start(controller) {
      controller.enqueue(encode([event({ role: 'assistant', content: '' })], false));
      init.signal.addEventListener('abort', () => { try { controller.error(new Error('aborted transport')); } catch {} }, { once: true });
    } }), { headers: { 'content-type': 'text/event-stream' } });
  } });
  await assert.rejects(api.chat(request(true)));
  assert.equal(submissions, 1);
});

test('reasoning and cached usage are checked independently of visible content', () => {
  const response = jsonResponse();
  response.usage = { ...COUNTS, completion_tokens_details: { reasoning_tokens: 2 },
    q4t_thinking_budget: { limit: 1, consumed: 1, forced_closure_tokens: 1 },
    prompt_tokens_details: { cached_tokens: 5 } };
  assert.equal(parseJsonWire(Buffer.from(JSON.stringify(response))).result.usage.prompt_tokens, 10);
  response.usage.q4t_thinking_budget.forced_closure_tokens = 2;
  assert.throws(() => parseJsonWire(Buffer.from(JSON.stringify(response))), ProtocolError);
});

test('request creation uses fresh independent cancellation credentials', () => {
  const a = makeRequest(request(false).messages), b = makeRequest(request(false).messages);
  assert.notEqual(a.request_id, b.request_id); assert.notEqual(a.cancel_token, b.cancel_token);
  assert.match(a.cancel_token, /^[0-9a-f]{64}$/); assert.equal(a.stream_options.include_usage, true);
});
