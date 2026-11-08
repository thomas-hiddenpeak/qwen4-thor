// Actual pinned SDK over in-memory Fetch responses; no HTTP/model execution.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { loadSdk } from '../../serve_protocol/sdk.mjs';
import { ChatClient, ProtocolError, parseJson, parseJsonWire, parseSseWire,
  prepareToolCalls, appendToolResults, appendFinal, makeRequest, validateCapabilities,
  ExactJsonNumber, stringifyJson, withJsonSchema } from './chat_client.mjs';

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
function client(bytes, { status = 200, stream = false, timeoutMs = 5000, apiKey } = {}) {
  const requests = [];
  return { requests, api: new ChatClient(OpenAI, { timeoutMs, apiKey,
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

test('legacy function JSON and incremental SSE SDK roundtrip', async () => {
  const fn = { name: 'read', arguments: '{"path":"中国/MODEL.md"}' };
  const message = { role: 'assistant', content: null, function_call: fn };
  const events = [event({ role: 'assistant', content: '' }),
    event({ function_call: { name: 're', arguments: '{' } }),
    event({ function_call: { name: 'ad', arguments: '"path":"中国/' } }),
    event({ function_call: { arguments: 'MODEL.md"}' } }), event({}, 'function_call'),
    { ...event({}), choices: [], usage: COUNTS }];
  const left = client(Buffer.from(JSON.stringify(jsonResponse(message, 'function_call'))));
  const right = client(encode(events), { stream: true });
  const a = await left.api.chat(request(false)), b = await right.api.chat(request(true));
  assert.deepEqual(a.message, b.message);
  const calls = prepareToolCalls(b, { read: value => { assert.equal(value.path, '中国/MODEL.md'); } });
  assert.equal(calls[0].call_id, 'read');
  assert.deepEqual(appendToolResults([], b, { read: '真实记录' }).at(-1),
    { role: 'function', name: 'read', content: '真实记录' });
  assert.throws(() => appendToolResults([], b, { wrong: 'x' }), ProtocolError);
  assert.throws(() => appendFinal([], b), ProtocolError);
});

test('legacy function partial cancellation and mismatched terminal never execute', async () => {
  const partial = [event({ role: 'assistant', content: '' }),
    event({ function_call: { name: 'read', arguments: '{}' } })];
  for (const events of [partial, [...partial, event({}, 'tool_calls')],
      [...partial, event({}, 'stop')], [...partial, { error: { message: 'cancelled',
        type: 'server_error', code: 'cancelled', param: null } }]]) {
    const run = client(encode(events), { stream: true });
    await assert.rejects(run.api.chat(request(true)));
  }
  assert.throws(() => parseSseWire(encode([...partial, event({}, 'function_call'),
    { ...event({}), choices: [], usage: COUNTS }], false)));
});

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

test('explicit API key reaches only the request and is absent from client serialization', async () => {
  const apiKey = 'explicit-client-key', run = client(Buffer.from(JSON.stringify(jsonResponse())), { apiKey });
  await run.api.chat(request(false));
  assert.equal(new Headers(run.requests[0].headers).get('authorization'), 'Bearer ' + apiKey);
  assert.equal(run.requests.length, 1);
  assert.ok(!JSON.stringify(run.api).includes(apiKey));
});

test('optional connection settings reject ambiguity and malformed keys before requests', () => {
  for (const apiKey of ['', 'x\r\nInjected: y', '含空格', 'x'.repeat(4097)])
    assert.throws(() => new ChatClient(OpenAI, { apiKey }), ProtocolError);
  for (const options of [{ caFile: 'unused', fetchImpl: async () => {} },
    { connectHost: '127.0.0.1', fetchImpl: async () => {} }])
    assert.throws(() => new ChatClient(OpenAI, options), ProtocolError);
  assert.throws(() => new ChatClient(OpenAI, { caFile: 'unused' }), /CA requires HTTPS/);
  assert.throws(() => new ChatClient(OpenAI, { connectHost: 'host.invalid' }), /connection IP required/);
});

test('capability validation binds protocol and template identities', async () => {
  const file = new URL('../../../docs/evidence/serve-preflight-20261103/execution-final/business/candidate-mtp/capabilities.json', import.meta.url);
  const caps = JSON.parse(JSON.parse(readFileSync(fileURLToPath(file), 'utf8')).raw);
  assert.equal(validateCapabilities(caps, MODEL, caps.protocol_source_sha256).model, MODEL);
  const current = structuredClone(caps);
  current.template.message_profile = 'qwen3.8-openai-v3';
  assert.equal(validateCapabilities(current, MODEL).template.message_profile, 'qwen3.8-openai-v3');
  current.template.message_profile = 'unreviewed';
  assert.throws(() => validateCapabilities(current, MODEL), ProtocolError);
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

test('sampling logprobs survive JSON and interleaved choices SSE', async () => {
  const probability = { token: '中', logprob: -0.5, bytes: [228, 184, 173],
    top_logprobs: [{ token: '中', logprob: -0.5, bytes: [228, 184, 173] }] };
  const probabilities = { content: [probability], refusal: null };
  const value = jsonResponse();
  value.choices = [0, 1].map(index => ({ index, message: { role: 'assistant', content: '中' },
    finish_reason: 'stop', logprobs: probabilities }));
  const events = [];
  for (const index of [1, 0]) {
    const frame = event({ role: 'assistant', content: '' });
    frame.choices[0].index = index; events.push(frame);
  }
  for (const index of [0, 1]) {
    const frame = event({ content: '中' });
    frame.choices[0].index = index; frame.choices[0].logprobs = probabilities; events.push(frame);
    const terminal = event({}, 'stop'); terminal.choices[0].index = index; events.push(terminal);
  }
  events.push({ ...events[0], choices: [], usage: COUNTS });
  const a = client(Buffer.from(JSON.stringify(value)));
  const b = client(encode(events), { stream: true });
  const json = await a.api.chat({ ...request(false), n: 2, logprobs: true });
  const sse = await b.api.chat({ ...request(true), n: 2, logprobs: true });
  assert.deepEqual(json.choices, sse.choices);
  assert.deepEqual(json.choices[0].logprobs.content[0].bytes, [228, 184, 173]);
  assert.equal(a.requests.length, 1); assert.equal(b.requests.length, 1);
  assert.throws(() => prepareToolCalls(sse, {})); // Explicit choice selection required.
});

test('sampling rejects duplicate/missing choices and incomplete choice terminals', () => {
  for (const indices of [[0, 0], [1], [0, 2]]) {
    const value = jsonResponse();
    value.choices = indices.map(index => ({ ...value.choices[0], index }));
    assert.throws(() => parseJsonWire(Buffer.from(JSON.stringify(value))));
  }
  const events = streamEvents();
  const extra = event({ role: 'assistant', content: 'unfinished' }); extra.choices[0].index = 1;
  events.splice(2, 0, extra);
  assert.throws(() => parseSseWire(encode(events)));
});

test('sampling validates probability values, UTF8 fragments and alternatives', () => {
  const value = jsonResponse();
  const valid = { content: [{ token: '�', logprob: -9999, bytes: [228], top_logprobs: [] }], refusal: null };
  value.choices[0].logprobs = valid;
  assert.deepEqual(parseJsonWire(Buffer.from(JSON.stringify(value))).result.logprobs, valid);
  for (const corrupt of [p => p.content[0].logprob = 1, p => p.content[0].bytes = [256],
    p => p.content[0].top_logprobs = Array(21).fill({ token: 'x', logprob: -1, bytes: [120] })]) {
    value.choices[0].logprobs = structuredClone(valid); corrupt(value.choices[0].logprobs);
    assert.throws(() => parseJsonWire(Buffer.from(JSON.stringify(value))));
  }
});

test('sampling client rejects fewer choices than requested', async () => {
  const run = client(Buffer.from(JSON.stringify(jsonResponse())));
  await assert.rejects(run.api.chat({ ...request(false), n: 2 }), /choice count/);
  assert.equal(run.requests.length, 1);
});

test('prediction usage survives real SDK JSON/SSE assembly', async () => {
  const counts = { prompt_tokens: 10, completion_tokens: 7, total_tokens: 17,
    completion_tokens_details: { reasoning_tokens: 1, accepted_prediction_tokens: 4,
      rejected_prediction_tokens: 2 } };
  const data = jsonResponse(); data.usage = counts;
  const events = streamEvents(); events.at(-1).usage = counts;
  const json = parseJsonWire(Buffer.from(JSON.stringify(data)));
  const sse = parseSseWire(encode(events));
  assert.deepEqual(json.result, sse.result);
  assert.equal(json.result.usage.completion_tokens, 7);
  const actual = await client(Buffer.from(JSON.stringify(data))).api.chat(request(false));
  const actualStream = await client(encode(events), { stream: true }).api.chat(request(true));
  assert.deepEqual(actual.usage, counts);
  assert.deepEqual(actualStream.usage, counts);
});

test('prediction inconsistent counts fail closed', () => {
  for (const pair of [[-1, 0], [0, true], [2, 2], [undefined, 0]]) {
    const data = jsonResponse();
    data.usage = { ...COUNTS, completion_tokens_details: { reasoning_tokens: 0,
      accepted_prediction_tokens: pair[0], rejected_prediction_tokens: pair[1] } };
    assert.throws(() => parseJsonWire(Buffer.from(JSON.stringify(data))), ProtocolError);
  }
});

test('tool stream provisional fragments cannot complete before terminal and EOF', async () => {
  let controller, completed = false;
  const api = new ChatClient(OpenAI, { timeoutMs: 2000, fetchImpl: async () =>
    new Response(new ReadableStream({ start(c) { controller = c; } }),
      { headers: { 'content-type': 'text/event-stream' } }) });
  const msg = toolMessage();
  const first = event({ tool_calls: [{ index: 0, id: 'a', type: 'function',
    function: { name: 'read', arguments: '{"path":"docs/' } }] });
  const result = api.chat(request(true)).then(value => { completed = true; return value; });
  while (!controller) await new Promise(resolve => setTimeout(resolve, 1));
  controller.enqueue(encode([event({ role: 'assistant', content: '' }), first], false));
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(completed, false);
  controller.enqueue(encode([event({ tool_calls: [{ index: 0, function: {
    arguments: 'MODEL.md","start_line":1,"line_count":8}' } }] })], false));
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(completed, false);
  controller.enqueue(encode([event({}, 'tool_calls'), streamEvents().at(-1)]));
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(completed, false);
  controller.close();
  const actual = await result;
  assert.equal(completed, true);
  assert.deepEqual(actual.message.tool_calls, [msg.tool_calls[0]]);
});

test('tool stream later error invalidates all earlier complete fragments', async () => {
  const msg = toolMessage(), events = [event({ role: 'assistant', content: '' })];
  for (const [index, call] of msg.tool_calls.entries()) {
    events.push(event({ tool_calls: [{ index, id: call.id, type: 'function',
      function: { name: call.function.name, arguments: '' } }] }));
    for (const argumentsPart of call.function.arguments)
      events.push(event({ tool_calls: [{ index, function: { arguments: argumentsPart } }] }));
  }
  events.push({ error: { code: 'structured_output_incomplete', message: 'output budget exhausted' } });
  const run = client(encode(events), { stream: true });
  let executions = 0;
  await assert.rejects(async () => {
    const value = await run.api.chat(request(true));
    for (const _ of prepareToolCalls(value, { read: () => {} })) executions++;
  });
  assert.equal(executions, 0);
  assert.equal(run.requests.length, 1);
});

test('custom raw input survives SDK mixed JSON/SSE and explicit validators', async () => {
  const input = 'READ "中国"\n</tool_call>\\😀\0';
  const calls = [{ id: 'c0', type: 'custom', custom: { name: 'read', input } },
    { id: 'f1', type: 'function', function: { name: 'noop', arguments: '{}' } }];
  const msg = { role: 'assistant', content: null, tool_calls: calls };
  const events = [event({ role: 'assistant', content: '' }),
    event({ tool_calls: [{ index: 0, id: 'c0', type: 'custom', custom: { name: 'read', input: '' } }] })];
  for (const c of input) events.push(event({ tool_calls: [{ index: 0, custom: { input: c } }] }));
  events.push(event({ tool_calls: [{ index: 1, ...calls[1] }] }), event({}, 'tool_calls'), streamEvents().at(-1));
  const streamed = await client(encode(events), { stream: true }).api.chat(request(true));
  assert.deepEqual(streamed.message, msg);
  const json = { id: ID, object: 'chat.completion', model: MODEL, created: 42,
    choices: [{ index: 0, message: msg, logprobs: null, finish_reason: 'tool_calls' }], usage: COUNTS };
  const full = await client(Buffer.from(JSON.stringify(json))).api.chat(request(false));
  assert.deepEqual(full.message, streamed.message);
  const plan = prepareToolCalls(streamed, { noop: () => {} }, { read: value => { assert.equal(value, input); } });
  assert.deepEqual(plan[0], { call_id: 'c0', type: 'custom', name: 'read', input });
  assert.throws(() => prepareToolCalls(streamed, { noop: () => {}, read: () => {} }), ProtocolError);
  const history = appendToolResults([], streamed, { c0: 'record', f1: 'done' });
  assert.deepEqual(history[0], msg);
  assert.deepEqual(history.slice(1).map(v => v.tool_call_id), ['c0', 'f1']);
});

test('custom streaming rejects type mutation and partial terminal', async () => {
  const prefix = [event({ role: 'assistant', content: '' }),
    event({ tool_calls: [{ index: 0, id: 'c0', type: 'custom', custom: { name: 'read', input: 'partial' } }] })];
  const mutation = event({ tool_calls: [{ index: 0, type: 'function', function: { arguments: '{}' } }] });
  for (const rows of [prefix, [...prefix, mutation], [...prefix, event({}, 'length'), streamEvents().at(-1)]]) {
    let count = 0;
    await assert.rejects(async () => {
      const result = await client(encode(rows), { stream: true }).api.chat(request(true));
      for (const _ of prepareToolCalls(result, {}, { read: () => {} })) count++;
    });
    assert.equal(count, 0);
  }
});

test('custom project DSL validates without executing arbitrary input', async () => {
  const { parseCustomRead } = await import('./project_profile.mjs');
  assert.deepEqual(parseCustomRead('READ docs/MODEL.md 1 16'), { path: 'docs/MODEL.md', start_line: 1, line_count: 16 });
  for (const value of ['READ docs/MODEL.md 1 81', 'READ /etc/passwd 1 8',
    'READ docs/MODEL.md 1 8\n', 'READ docs/MODEL.md 1 8; ls']) assert.throws(() => parseCustomRead(value));
});


test('schema-v2 exact numeric data survives actual SDK serialization and tool response', async () => {
  const argumentsText = '{"n":1e400,"tiny":1e-400,"x":0.30000000000000001,"i":9007199254740993}';
  const msg = { role: 'assistant', content: null, tool_calls: [{ id: 'x', type: 'function',
    function: { name: 'read', arguments: argumentsText } }] };
  const run = client(Buffer.from(JSON.stringify(jsonResponse(msg, 'tool_calls'))));
  const schema = { type: 'object', properties: { n: { type: 'number', enum: [
    new ExactJsonNumber('1e400'), new ExactJsonNumber('1e-400'),
    new ExactJsonNumber('0.30000000000000001'), 9007199254740993n] } },
    required: ['n'], additionalProperties: false };
  const body = withJsonSchema({ ...request(false), tools: [{ type: 'function', function: { name: 'read' } }],
    tool_choice: 'required' }, schema);
  const result = await run.api.chat(body);
  assert.equal(run.requests.length, 1);
  const encoded = run.requests[0].body;
  assert.match(encoded, /"enum":\[1e400,1e-400,0\.30000000000000001,9007199254740993\]/);
  const captured = parseJson(encoded, { exactNumbers: true });
  assert.equal(captured.response_format.json_schema.schema.properties.n.enum[0].lexeme, '1e400');
  assert.equal(result.message.tool_calls[0].function.arguments, argumentsText);
  const [call] = prepareToolCalls(result, { read: args => {
    assert.ok(args.n instanceof ExactJsonNumber);
    assert.equal(args.x.lexeme, '0.30000000000000001');
  } });
  assert.equal(stringifyJson(call.arguments), argumentsText);
  assert.throws(() => new ExactJsonNumber('NaN'), ProtocolError);
  assert.throws(() => new ExactJsonNumber('1]'), ProtocolError);
  assert.throws(() => parseJson('{"x":1,"x":2}', { exactNumbers: true }), ProtocolError);
});

test('schema-v2 root references and no-user instructions preserve caller history', () => {
  const schema = { $ref: '#/$defs/root', $defs: { root: { type: 'object', additionalProperties: false } } };
  const original = { ...request(false), messages: [{ role: 'system', content: 'Brief answer' }] };
  const before = structuredClone(original);
  const result = withJsonSchema(original, schema);
  assert.deepEqual(original, before);
  assert.deepEqual(result.messages.slice(1), before.messages);
  assert.deepEqual(result.response_format.json_schema.schema, schema);
});
