// One explicit SDK connection per invocation. Fault injection belongs to owner.
import fs from 'node:fs';
import path from 'node:path';
import { deepStrictEqual } from 'node:assert';
import { EventEmitter } from 'node:events';
import { pathToFileURL } from 'node:url';
const MAX = 4 * 1024 * 1024;
const check = (value, message) => { if (!value) throw new Error(message); };
let input = '';
for await (const part of process.stdin) {
  input += part;
  check(Buffer.byteLength(input) <= 65536, 'input bound');
}
const args = JSON.parse(input);
check(['complete', 'hold_stream', 'hold_complete'].includes(args.mode), 'fixed recovery mode');
const sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR);
for (const [name, version] of [['openai', '7.47.0'], ['ws', '8.21.0']])
  check(JSON.parse(fs.readFileSync(path.join(sdk, 'node_modules', name, 'package.json'))).version === version, 'SDK identity');
const { default: OpenAI } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/index.mjs')));
const { OpenAIRealtimeWS } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/realtime/ws.mjs')));
const url = new URL(args.base_url);
check(['http:', 'https:'].includes(url.protocol) && url.hostname === '127.0.0.1' &&
  url.pathname === '/v1' && !url.username && !url.password && !url.search && !url.hash,
  'explicit local native endpoint required');
check((url.protocol === 'https:') === Boolean(args.ca_file), 'TLS mode must not change');
check(fs.statSync(args.file).size > 0 && fs.statSync(args.file).size <= 2 * 1024 * 1024,
  'audio bound');
const audio = fs.readFileSync(args.file);
const client = new OpenAI({ baseURL: url.toString(), apiKey: args.api_key, maxRetries: 0 });
const options = { maxPayload: MAX, perMessageDeflate: false,
  followRedirects: false, handshakeTimeout: 10000 };
const transport = args.ca_file ? 'openai-realtime-wss' : 'ws-loopback';
let rt;
if (args.ca_file) {
  rt = new OpenAIRealtimeWS({ intent: 'transcription', options: {
    ...options, ca: fs.readFileSync(args.ca_file) } }, client);
} else {
  // The pinned OpenAI SDK always constructs wss:, including for http baseURL.
  // Explicit loopback plaintext uses its pinned ws peer directly. Never change
  // a requested TLS connection into plaintext, nor label this as SDK coverage.
  const { default: WebSocket } = await import(pathToFileURL(
    path.join(sdk, 'node_modules/ws/wrapper.mjs')));
  const plain = new URL(url);
  plain.protocol = 'ws:'; plain.pathname += '/realtime';
  plain.searchParams.set('intent', 'transcription');
  rt = new EventEmitter();
  rt.socket = new WebSocket(plain, { ...options,
    headers: { Authorization: `Bearer ${args.api_key}` } });
  rt.send = (event) => rt.socket.send(JSON.stringify(event));
  rt.close = () => rt.socket.close(1000);
  rt.socket.on('error', (cause) => rt.emit('error', { cause, name: 'TransportError' }));
  rt.socket.on('message', (data) => {
    try {
      const value = JSON.parse(data.toString());
      check(value && !Array.isArray(value) && typeof value.type === 'string',
        'invalid event shape');
      rt.emit('event', value);
    } catch {
      rt.emit('event', { type: 'error', error: { code: 'invalid_event' } });
      rt.socket.terminate();
    }
  });
}
let fatal = null, closed = false, closeCode = null, connections = 0, armed = false;
let bytes = 0, sent = 0, sentAudio = 0, partial = null, turn = null;
const events = [], transportErrors = [];
const deadline = Date.now() + 150000;
const timer = setTimeout(() => { fatal = new Error('SDK deadline'); rt.socket.terminate(); }, 150000);
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
  if (bytes > MAX || events.length >= 1024) {
    fatal = new Error('event bound'); rt.socket.terminate(); return;
  }
  events.push(event);
  if (event.type === 'error') { fatal = new Error('server error: ' + event.error.code); rt.socket.terminate(); }
});
async function until(predicate, allowClosed = false) {
  while (!predicate()) {
    if (fatal) throw fatal;
    check(Date.now() < deadline && (allowClosed || !closed), 'deadline or premature close');
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  if (fatal) throw fatal;
}
function send(value) { check(++sent <= 256, 'client event bound'); rt.send(value); }
async function event(type, start, predicate = () => true) {
  let found;
  await until(() => {
    found = events.slice(start).find((value) => value.type === type && predicate(value));
    return found;
  });
  return found;
}
async function append(limit) {
  while (sentAudio < limit) {
    await until(() => rt.socket.bufferedAmount <= 1048576);
    const end = Math.min(sentAudio + 12000, limit);
    send({ type: 'input_audio_buffer.append', audio: audio.subarray(sentAudio, end).toString('base64') });
    sentAudio = end;
  }
}
function result(status, error) {
  return { phase: 'result', status, ...(error ? { error } : {}), mode: args.mode, transport,
    connections, close_code: closeCode, automatic_replay: false, partial, turn,
    sent_audio_bytes: sentAudio, total_audio_bytes: audio.length, sent_events: sent,
    event_bytes: bytes, events, transport_errors: transportErrors };
}
try {
  await event('session.created', 0);
  const begin = events.length;
  send({ type: 'session.update', session: args.settings });
  const effective = await event('session.updated', begin);
  deepStrictEqual(effective.session.audio.input, { ...args.settings.audio.input,
    transcription: { language: '', prompt: '', languages: [], keywords: [],
      ...args.settings.audio.input.transcription } }, 'effective session');
  const start = events.length;
  if (args.mode === 'hold_stream') {
    check(args.settings.audio.input.format.type === 'audio/pcmu' &&
      args.settings.audio.input.transcription.delay === 'low' && audio.length > 24000,
      'qualified continuous prefix required');
    await append(24000);
    const delta = await event('conversation.item.input_audio_transcription.delta', start,
      (value) => value.delta.length > 0);
    partial = { item_id: delta.item_id, delta: delta.delta };
  } else {
    await append(audio.length);
    send({ type: 'input_audio_buffer.commit' });
    const committed = await event('input_audio_buffer.committed', start);
    const terminal = await event('conversation.item.input_audio_transcription.completed', start,
      (value) => value.item_id === committed.item_id);
    const delta = events.slice(start).filter((value) => value.item_id === committed.item_id &&
      value.type === 'conversation.item.input_audio_transcription.delta').map((value) => value.delta).join('');
    check(delta === terminal.transcript && committed.previous_item_id === null, 'fresh transcript/history');
    send({ type: 'conversation.item.retrieve', item_id: committed.item_id });
    const retrieved = await event('conversation.item.retrieved', start,
      (value) => value.item.id === committed.item_id);
    check(retrieved.item.content.length === 1 &&
      retrieved.item.content[0].transcript === terminal.transcript, 'retained actual transcript');
    turn = { item_id: committed.item_id, committed, terminal, delta, retrieved };
  }
  if (args.mode === 'complete') {
    rt.close(); await until(() => closed, true);
    check(closeCode === 1000, 'normal close missing');
  } else {
    armed = true;
    process.stdout.write(JSON.stringify({ phase: 'ready', mode: args.mode, transport, connections,
      item_id: partial?.item_id || turn.item_id, delta: partial?.delta || turn.delta,
      sent_audio_bytes: sentAudio, total_audio_bytes: audio.length }) + '\n');
    await until(() => closed, true);
    check([1001, 1006, 1011, 1012].includes(closeCode), 'fault did not close existing connection');
  }
  check(connections === 1, 'automatic reconnect detected');
  if (partial) check(!events.some((value) => value.type === 'input_audio_buffer.committed' ||
    value.type === 'conversation.item.input_audio_transcription.completed'), 'unfinished input was completed');
  process.stdout.write(JSON.stringify(result('PASS')) + '\n');
} catch (error) {
  rt.socket.terminate(); process.exitCode = 1;
  process.stdout.write(JSON.stringify(result('FAIL', error.message)) + '\n');
} finally { clearTimeout(timer); }
