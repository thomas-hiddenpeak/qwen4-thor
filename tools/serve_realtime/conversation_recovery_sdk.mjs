// Actual tool turns on one connection. Only the enclosing owner injects faults.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { EventEmitter } from 'node:events';
import { pathToFileURL } from 'node:url';
import { check, terminal, usage, text, functionCall, streamAgrees } from './conversation_checks.mjs';
let input = '';
for await (const part of process.stdin) {
  input += part;
  check(Buffer.byteLength(input) <= 65536, 'input bound');
}
const args = JSON.parse(input);
check(['complete', 'hold_stream', 'hold_complete'].includes(args.mode), 'fixed recovery mode');
const qualityPolicy = args.quality_policy ?? null;
check([null, 'record_exact_source_answer_separately'].includes(qualityPolicy), 'fixed quality policy');
const sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR);
for (const [name, version] of [['openai', '7.47.0'], ['ws', '8.21.0']])
  check(JSON.parse(fs.readFileSync(path.join(sdk, 'node_modules', name,
    'package.json'))).version === version, 'SDK identity');
const { default: OpenAI } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/index.mjs')));
const { OpenAIRealtimeWS } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/realtime/ws.mjs')));
const url = new URL(args.base_url);
check(['http:', 'https:'].includes(url.protocol) && url.hostname === '127.0.0.1' &&
  url.pathname === '/v1' && !url.username && !url.password && !url.search && !url.hash,
  'explicit local native endpoint required');
check((url.protocol === 'https:') === Boolean(args.ca_file), 'TLS mode must not change');
const options = { maxPayload: 4 << 20, perMessageDeflate: false,
  followRedirects: false, handshakeTimeout: 10000 };
const transport = args.ca_file ? 'official-openai-wss' : 'ws-loopback';
let rt;
if (args.ca_file) {
  const client = new OpenAI({ baseURL: url.toString(), apiKey: args.api_key, maxRetries: 0 });
  rt = new OpenAIRealtimeWS({ model: 'qwen3.8-flash-next', options: {
    ...options, ca: fs.readFileSync(args.ca_file) } }, client);
} else {
  // The pinned SDK constructs wss: even for http:. This explicit local ws peer
  // is never called official SDK coverage or used as a failed TLS fallback.
  const { default: WebSocket } = await import(pathToFileURL(
    path.join(sdk, 'node_modules/ws/wrapper.mjs')));
  const plain = new URL(url);
  plain.protocol = 'ws:'; plain.pathname += '/realtime';
  plain.searchParams.set('model', 'qwen3.8-flash-next');
  rt = new EventEmitter();
  rt.socket = new WebSocket(plain, { ...options,
    headers: { Authorization: `Bearer ${args.api_key}` } });
  rt.send = (event) => rt.socket.send(JSON.stringify(event));
  rt.close = () => rt.socket.close(1000);
  rt.socket.on('error', (cause) => rt.emit('error', { cause, name: 'TransportError' }));
  rt.socket.on('message', (data) => {
    try {
      const event = JSON.parse(data.toString());
      check(event && !Array.isArray(event) && typeof event.type === 'string', 'event shape');
      rt.emit('event', event);
    } catch {
      rt.emit('event', { type: 'error', error: { code: 'invalid_event' } });
      rt.socket.terminate();
    }
  });
}
let fatal = null, closed = false, closeCode = null, connections = 0, armed = false;
let bytes = 0, sent = 0, creates = 0, partial = null, toolRecord = null;
let modelQuality = null;
const events = [], responses = [], transportErrors = [];
const deadline = Date.now() + 180000;
const timer = setTimeout(() => { fatal = new Error('absolute deadline'); rt.socket.terminate(); }, 180000);
rt.socket.on('open', () => { ++connections; });
rt.socket.on('close', (code) => { closed = true; closeCode = code; });
rt.on('error', (error) => {
  if (!error.error) {
    const reason = error.cause?.code || error.name;
    if (armed) transportErrors.push(reason);
    else { fatal = new Error('SDK transport: ' + reason); rt.socket.terminate(); }
  }
});
rt.on('event', (event) => {
  bytes += Buffer.byteLength(JSON.stringify(event));
  if (bytes > (4 << 20) || events.length >= 4096) {
    fatal = new Error('event bound'); rt.socket.terminate(); return;
  }
  events.push(event);
  if (event.type === 'error') { fatal = new Error('server error: ' + event.error.code); rt.socket.terminate(); }
});
async function until(predicate, allowClosed = false) {
  for (;;) {
    if (fatal) throw fatal;
    if (predicate()) return;
    check(Date.now() < deadline && (allowClosed || !closed), 'deadline or premature close');
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
}
function send(event) { check(++sent <= 32, 'client event bound'); rt.send(event); }
async function add(item) {
  const at = events.length;
  send({ type: 'conversation.item.create', item });
  await until(() => events.slice(at).some((e) => e.type === 'conversation.item.done' &&
    e.item?.id === item.id));
}
async function create(value, hold = false) {
  check(++creates <= (args.mode === 'hold_stream' ? 3 : 2), 'response bound');
  const at = events.length;
  send({ type: 'response.create', response: { ...value,
    metadata: { acceptance_case: `recovery_${creates}` } } });
  let id;
  await until(() => { id = events.slice(at).find((e) => e.type === 'response.created')?.response.id; return id; });
  if (hold) {
    let delta;
    await until(() => {
      check(!events.slice(at).some((e) => e.type === 'response.done'), 'completed before fault');
      delta = events.slice(at).find((e) => e.type === 'response.output_text.delta' &&
        e.response_id === id && e.delta.length > 0);
      return delta;
    });
    partial = { response_id: id, item_id: delta.item_id, delta: delta.delta };
    return;
  }
  await until(() => events.slice(at).some((e) => e.type === 'response.done' && e.response?.id === id));
  const row = terminal(events, id, 'completed'); usage(row); streamAgrees(events, row);
  responses.push(row); return row;
}
function result(status, error) {
  return { phase: 'result', status, ...(error ? { error } : {}), mode: args.mode,
    quality_policy: qualityPolicy, model_quality: modelQuality,
    transport, connections, close_code: closeCode, automatic_replay: false,
    response_creates: creates, partial, tool_record: toolRecord, responses,
    sent_events: sent, event_bytes: bytes, events, transport_errors: transportErrors };
}
try {
  await until(() => events.some((e) => e.type === 'session.created') &&
    events.some((e) => e.type === 'conversation.created'));
  const initial = events.find((e) => e.type === 'session.created').session;
  check(initial.type === 'realtime' && initial.model === 'qwen3.8-flash-next', 'session identity');
  check(fs.statSync(args.record_file).size <= (1 << 20), 'bounded project record');
  const record = fs.readFileSync(args.record_file);
  const digest = crypto.createHash('sha256').update(record).digest('hex');
  const version = record.toString().match(/默认\s*rev(\d+)/)?.[1];
  check(digest === args.record_sha256 && version && `rev${version}` === args.expected_version,
    'source-bound actual current project record');
  const session = args.prompt_reference ? { type: 'realtime',
    prompt: args.prompt_reference } : { type: 'realtime',
    instructions: 'Use project records exactly. Only execute the declared lookup tool when requested.',
    output_modalities: ['text'], max_output_tokens: 128,
    tools: [{ type: 'function', name: 'lookup_project_record',
      description: 'Read the actual local project release record.', parameters: {
        type: 'object', properties: { record_key: { type: 'string', enum: ['release'] } },
        required: ['record_key'], additionalProperties: false } }],
  };
  send({ type: 'session.update', session });
  await until(() => events.some((e) => e.type === 'session.updated'));
  if (args.prompt_reference) {
    const resolved = events.find((e) => e.type === 'session.updated').session;
    check(resolved.prompt?.id === args.prompt_reference.id &&
      resolved.prompt.version === args.prompt_reference.version &&
      resolved.max_output_tokens === 128 &&
      resolved.tools?.[0]?.name === 'lookup_project_record', 'stored Prompt resolution');
  }
  await add({ type: 'message', id: 'user_lookup', role: 'user', content: [{
    type: 'input_text', text: 'Call lookup_project_record with record_key release. After receiving the result reply only with its version value.' }] });
  const call = functionCall(await create({ tool_choice: { type: 'function', name: 'lookup_project_record' } }));
  toolRecord = { record_key: 'release', version: `rev${version}`,
    source: 'docs/STATUS.md', source_sha256: digest };
  await add({ type: 'function_call_output', id: 'tool_result',
    call_id: call.call_id, output: JSON.stringify(toolRecord) });
  const answer = await create({ tools: [], tool_choice: 'none',
    instructions: 'Reply only with the exact version field from the actual tool result.' });
  const actual = text(answer);
  modelQuality = { status: actual === toolRecord.version ? 'PASS' : 'FAIL',
    expected: toolRecord.version, actual, criterion: 'exact_source_version',
    response_id: answer.id };
  if (!qualityPolicy) check(modelQuality.status === 'PASS', 'actual source-bound model answer');
  if (args.mode === 'hold_stream') {
    await create({ conversation: 'none', input: [], tools: [], tool_choice: 'none',
      max_output_tokens: 2048,
      instructions: 'Write a long numbered list of the integers from 1 to 10000, one per line. Continue until the output limit.' }, true);
  }
  if (args.mode === 'complete') {
    rt.close(); await until(() => closed, true);
    check(closeCode === 1000, 'normal close missing');
  } else {
    armed = true;
    process.stdout.write(JSON.stringify({ phase: 'ready', mode: args.mode, transport,
      connections, response_creates: creates, partial,
      completed_ids: responses.map((r) => r.id) }) + '\n');
    await until(() => closed, true);
    check([1001, 1006, 1011, 1012].includes(closeCode), 'fault did not close owned connection');
  }
  check(connections === 1, 'automatic reconnect detected');
  if (partial) check(!events.some((e) => e.type === 'response.done' &&
    e.response?.id === partial.response_id), 'interrupted request produced a terminal before disconnect');
  process.stdout.write(JSON.stringify(result(qualityPolicy ? 'PROTOCOL_PASS' : 'PASS')) + '\n');
} catch (error) {
  rt.socket.terminate(); process.exitCode = 1;
  process.stdout.write(JSON.stringify(result('FAIL', error.message)) + '\n');
} finally { clearTimeout(timer); }
