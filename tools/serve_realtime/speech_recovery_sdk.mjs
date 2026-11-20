// Native speech on one connection; only the enclosing service owner injects faults.
import fs from 'node:fs';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { pathToFileURL } from 'node:url';
import { check, terminal, usage } from './conversation_checks.mjs';
let input = '';
for await (const part of process.stdin) {
  input += part;
  check(Buffer.byteLength(input) <= 65536, 'input bound');
}
const args = JSON.parse(input);
check(['complete', 'hold_stream', 'hold_complete'].includes(args.mode), 'fixed recovery mode');
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
let bytes = 0, sent = 0, creates = 0, partial = null;
const events = [], responses = [], retrievals = [], transportErrors = [];
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
async function action(event, type, predicate = () => true) {
  check(++sent <= 32, 'client event bound');
  const at = events.length; rt.send(event); let found;
  await until(() => { found = events.slice(at).find((e) => e.type === type && predicate(e)); return found; });
  return found;
}
async function user(id, task) {
  await action({ type: 'conversation.item.create', item: { id, type: 'message', role: 'user',
    content: [{ type: 'input_text', text: task }] } }, 'conversation.item.done', (e) => e.item?.id === id);
}
const phrase = 'Hello, this is a native speech test.';
async function create(hold = false) {
  check(++creates <= (args.mode === 'hold_stream' ? 2 : 1), 'response bound');
  const at = events.length;
  const created = await action({ type: 'response.create', response: {
    output_modalities: ['audio'], tools: [], tool_choice: 'none', max_output_tokens: 3457,
    metadata: { acceptance_case: hold ? 'recovery_interrupted_speech' : 'recovery_speech' },
  } }, 'response.created');
  const id = created.response.id;
  if (hold) {
    let delta;
    await until(() => {
      check(!events.slice(at).some((e) => e.type === 'response.done'), 'completed before fault');
      delta = events.slice(at).find((e) => e.type === 'response.output_audio.delta' &&
        e.response_id === id && Buffer.from(e.delta, 'base64').length > 0);
      return delta;
    });
    partial = { response_id: id, item_id: delta.item_id, delta: delta.delta };
    return;
  }
  await until(() => events.slice(at).some((e) => e.type === 'response.done' && e.response?.id === id));
  const row = terminal(events, id, 'completed'); usage(row); responses.push(row);
  check(row.output.length === 1 && row.output[0].content.length === 1 &&
    row.output[0].content[0].type === 'output_audio' &&
    row.output[0].content[0].transcript === phrase, 'actual speech content');
  const fetched = await action({ type: 'conversation.item.retrieve', item_id: row.output[0].id },
    'conversation.item.retrieved', (e) => e.item?.id === row.output[0].id);
  retrievals.push(fetched.item); return row;
}
function result(status, error) {
  return { phase: 'result', status, ...(error ? { error } : {}), mode: args.mode,
    transport, connections, close_code: closeCode, automatic_replay: false,
    response_creates: creates, partial, responses, retrievals,
    sent_events: sent, event_bytes: bytes, events, transport_errors: transportErrors };
}
try {
  await until(() => events.some((e) => e.type === 'session.created') &&
    events.some((e) => e.type === 'conversation.created'));
  const initial = events.find((e) => e.type === 'session.created').session;
  check(initial.type === 'realtime' && initial.model === 'qwen3.8-flash-next', 'session identity');
  await action({ type: 'session.update', session: { type: 'realtime',
    output_modalities: ['audio'], max_output_tokens: 3457,
    audio: { output: { voice: 'ryan', speed: 1, format: { type: 'audio/pcm', rate: 24000 } } },
  } }, 'session.updated');
  await user('user_speech', 'Repeat exactly without adding words: ' + phrase);
  await create();
  if (args.mode === 'hold_stream') {
    // The pinned API defines speed on the session, not response.create.audio.
    await action({ type: 'session.update', session: { type: 'realtime',
      audio: { output: { speed: .25 } } } }, 'session.updated');
    await user('user_interrupt', 'Read the following text aloud exactly: The service is ready. '+
      'We will read the project records carefully and explain the current status. '+
      'The first check confirms that every request belongs to the correct client. '+
      'The next check confirms that every response reports its actual usage. '+
      'When a connection fails, the application records the interruption and waits for an explicit new request.');
    await create(true);
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
    e.response?.id === partial.response_id), 'interrupted request completed before disconnect');
  process.stdout.write(JSON.stringify(result('PASS')) + '\n');
} catch (error) {
  rt.socket.terminate(); process.exitCode = 1;
  process.stdout.write(JSON.stringify(result('FAIL', error.message)) + '\n');
} finally { clearTimeout(timer); }
