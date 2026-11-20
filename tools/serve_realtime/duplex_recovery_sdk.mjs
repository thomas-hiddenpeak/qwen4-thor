// Same-conversation input recovery; only the owning Guardian injects faults.
import fs from 'node:fs';
import crypto from 'node:crypto';
import { openJournal } from './bounded_journal.mjs';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { pathToFileURL } from 'node:url';
import { check, terminal, usage, text, streamAgrees } from './conversation_checks.mjs';
let input = '';
for await (const part of process.stdin) {
  input += part;
  check(Buffer.byteLength(input) <= 65536, 'input bound');
}
const args = JSON.parse(input);
check(['complete', 'hold_stream', 'hold_complete'].includes(args.mode), 'fixed recovery mode');
const journal = openJournal(args.journal_file);
process.once('exit', () => journal.close());
const clip = fs.readFileSync(args.clip);
check(clip.length > 144000 && clip.length <= (1 << 20) && clip.length % 2 === 0 &&
  crypto.createHash('sha256').update(clip).digest('hex') === args.clip_sha256, 'actual input identity');
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
let bytes = 0, sent = 0, creates = 0, partial = null, partialBytes = 0;
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
  try { journal.append('server', event); } catch (error) {
    fatal = error; rt.socket.terminate(); return;
  }
  events.push(event);
  if (event.type === 'error' || event.type.endsWith('transcription.failed')) {
    fatal = new Error('server error: ' + event.error?.code); rt.socket.terminate();
  }
});
async function until(predicate, allowClosed = false) {
  for (;;) {
    if (fatal) throw fatal;
    if (predicate()) return;
    check(Date.now() < deadline && (allowClosed || !closed), 'deadline or premature close');
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
}
function send(event) {
  check(++sent <= 64, 'client event bound');
  journal.append('client', event); rt.send(event);
}
async function waitEvent(at, type, predicate = () => true) {
  let found;
  await until(() => { found = events.slice(at).find((e) => e.type === type && predicate(e)); return found; });
  return found;
}
async function action(event, type, predicate = () => true) {
  const at = events.length; send(event); return waitEvent(at, type, predicate);
}
async function append(raw) {
  for (let offset = 0; offset < raw.length; offset += 12000) {
    await until(() => rt.socket.bufferedAmount <= (1 << 20));
    send({ type: 'input_audio_buffer.append', audio: raw.subarray(offset, offset + 12000).toString('base64') });
  }
}
const instructions = '请逐字复述本轮用户提供的录音内容。保持原语言，完整输出一次，不翻译，不添加说明。';
function result(status, error) {
  return { phase: 'result', status, ...(error ? { error } : {}), mode: args.mode,
    transport, connections, close_code: closeCode, automatic_replay: false,
    response_creates: creates, partial, partial_audio_bytes: partialBytes,
    input_sha256: args.clip_sha256, complete_audio_bytes: clip.length,
    responses, retrievals, sent_events: sent, event_bytes: bytes, events,
    transport_errors: transportErrors };
}
try {
  await waitEvent(0, 'session.created'); await waitEvent(0, 'conversation.created');
  await action({ type: 'session.update', session: { type: 'realtime',
    instructions, output_modalities: ['text'], max_output_tokens: 128,
    audio: { input: { format: { type: 'audio/pcm', rate: 24000 },
      transcription: { model: 'qwen3-asr-0.6b' }, noise_reduction: null, turn_detection: null } },
  } }, 'session.updated');
  const at = events.length;
  await append(clip);
  const commit = await action({ type: 'input_audio_buffer.commit' }, 'input_audio_buffer.committed');
  await waitEvent(at, 'conversation.item.input_audio_transcription.completed',
    (e) => e.item_id === commit.item_id);
  const fetched = await action({ type: 'conversation.item.retrieve', item_id: commit.item_id },
    'conversation.item.retrieved', (e) => e.item?.id === commit.item_id);
  retrievals.push(fetched.item);
  check(++creates === 1, 'response bound');
  const created = await action({ type: 'response.create', response: {
    conversation: 'auto', output_modalities: ['text'], instructions,
    tools: [], tool_choice: 'none', max_output_tokens: 128,
    metadata: { acceptance_case: 'recovery_duplex' },
  } }, 'response.created');
  await waitEvent(at, 'response.done', (e) => e.response?.id === created.response.id);
  const row = terminal(events, created.response.id, 'completed');
  usage(row); streamAgrees(events, row); responses.push(row);
  // Content is evaluated from the raw output by the independent checker.
  if (args.mode === 'hold_stream') {
    await action({ type: 'session.update', session: { type: 'realtime',
      audio: { input: { transcription: { model: 'qwen3-asr-0.6b', delay: 'low' } } },
    } }, 'session.updated');
    const from = events.length;
    partialBytes = 144000; await append(clip.subarray(0, partialBytes));
    const delta = await waitEvent(from, 'conversation.item.input_audio_transcription.delta',
      (e) => e.delta.length > 0 && e.item_id !== commit.item_id);
    partial = { item_id: delta.item_id, delta: delta.delta };
    check(!events.slice(from).some((e) => e.type === 'input_audio_buffer.committed' ||
      e.type.endsWith('transcription.completed')), 'withheld input completed');
  }
  if (args.mode === 'complete') {
    rt.close(); await until(() => closed, true);
    check(closeCode === 1000, 'normal close missing');
  } else {
    armed = true;
    process.stdout.write(JSON.stringify({ phase: 'ready', mode: args.mode, transport,
      connections, response_creates: creates, partial, partial_audio_bytes: partialBytes,
      completed_ids: responses.map((r) => r.id), input_item_id: commit.item_id }) + '\n');
    await until(() => closed, true);
    check([1001, 1006, 1011, 1012].includes(closeCode), 'fault did not close owned connection');
  }
  check(connections === 1, 'automatic reconnect detected');
  if (partial) check(!events.some((e) => e.item_id === partial.item_id &&
    ['input_audio_buffer.committed', 'conversation.item.input_audio_transcription.completed'].includes(e.type)),
    'interrupted input completed before disconnect');
  process.stdout.write(JSON.stringify(result('PASS')) + '\n');
} catch (error) {
  rt.socket.terminate(); process.exitCode = 1;
  process.stdout.write(JSON.stringify(result('FAIL', error.message)) + '\n');
} finally { clearTimeout(timer); }
