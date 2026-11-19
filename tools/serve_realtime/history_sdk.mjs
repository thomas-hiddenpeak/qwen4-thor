// Actual pinned OpenAI WSS SDK, fixed history/retrieval acceptance only.
import fs from 'node:fs';
import { deepStrictEqual } from 'node:assert';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const MAX = 8 * 1024 * 1024;
const check = (value, message) => { if (!value) throw new Error(message); };
let input = '';
for await (const part of process.stdin) {
  input += part;
  check(Buffer.byteLength(input) <= 65536, 'input budget');
}
const args = JSON.parse(input);
check(['manual', 'near_stream'].includes(args.mode), 'fixed mode required');
const sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR);
for (const [name, version] of [['openai', '7.47.0'], ['ws', '8.21.0']])
  check(JSON.parse(fs.readFileSync(path.join(sdk, 'node_modules', name, 'package.json'))).version === version, 'SDK identity');
const { default: OpenAI } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/index.mjs')));
const { OpenAIRealtimeWS } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/realtime/ws.mjs')));
const url = new URL(args.base_url);
check(url.protocol === 'https:' && url.hostname === '127.0.0.1' && url.pathname === '/v1' &&
  !url.username && !url.password && !url.search && !url.hash, 'local native TLS required');
check(fs.statSync(args.file).size > 0 && fs.statSync(args.file).size <= 2 * 1024 * 1024, 'audio bound');
const audio = fs.readFileSync(args.file);
const client = new OpenAI({ baseURL: url.toString(), apiKey: args.api_key, maxRetries: 0 });
const rt = new OpenAIRealtimeWS({ intent: 'transcription', options: {
  maxPayload: MAX, perMessageDeflate: false, followRedirects: false,
  handshakeTimeout: 10000, ca: fs.readFileSync(args.ca_file) } }, client);
let fatal = null, closed = false, closeCode = null, connections = 0, bytes = 0, sent = 0;
const events = [], turns = [], checks = [];
const deadline = Date.now() + 120000;
const guard = setTimeout(() => { fatal = new Error('SDK deadline'); rt.socket.terminate(); }, 120000);
rt.socket.on('open', () => { ++connections; });
rt.socket.on('close', (code) => { closed = true; closeCode = code; });
rt.on('error', (error) => {
  if (!error.error) { fatal = new Error('SDK transport: ' + (error.cause?.code || error.name)); rt.socket.terminate(); }
});
rt.on('event', (event) => {
  bytes += Buffer.byteLength(JSON.stringify(event));
  if (bytes > MAX || events.length >= 2048) { fatal = new Error('event budget'); rt.socket.terminate(); return; }
  events.push(event);
});
async function until(predicate, allowClosed = false) {
  while (!predicate()) {
    if (fatal) throw fatal;
    check(Date.now() < deadline && (allowClosed || !closed), 'deadline or premature close');
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
}
function send(value) {
  check(++sent <= 512, 'client event bound');
  rt.send(value);
}
async function event(type, start, predicate = () => true) {
  let found;
  await until(() => {
    const recent = events.slice(start);
    const error = recent.find((v) => v.type === 'error');
    if (type !== 'error' && error) throw new Error('server error: ' + error.error.code);
    found = recent.find((v) => v.type === type && predicate(v));
    return found;
  });
  return found;
}
async function absent(id, label) {
  const start = events.length;
  send({ type: 'conversation.item.retrieve', item_id: id, event_id: label });
  const value = await event('error', start);
  check(value.error.code === 'item_not_found' && value.error.event_id === label, 'missing-item correlation');
  checks.push({ type: 'absent', item_id: id, label, event: value });
}
async function retrieve(id) {
  const start = events.length;
  send({ type: 'conversation.item.retrieve', item_id: id });
  const value = await event('conversation.item.retrieved', start);
  check(value.item.id === id && value.item.type === 'message' && value.item.role === 'user' &&
    value.item.content.length === 1 && value.item.content[0].type === 'input_audio', 'retrieved identity/content');
  return value;
}
async function remove(id) {
  const start = events.length;
  send({ type: 'conversation.item.delete', item_id: id });
  const value = await event('conversation.item.deleted', start);
  check(value.item_id === id, 'deleted identity');
  checks.push({ type: 'deleted', item_id: id, event: value });
  await absent(id, 'deleted-' + checks.length);
}
async function transcribe(continuous) {
  const start = events.length;
  let at = 0;
  const appendTo = async (limit) => {
    while (at < limit) {
      await until(() => rt.socket.bufferedAmount <= 1048576);
      const end = Math.min(at + 12000, limit);
      send({ type: 'input_audio_buffer.append', audio: audio.subarray(at, end).toString('base64') });
      at = end;
    }
  };
  let before = null;
  if (continuous) {
    check(audio.length > 24000, 'withheld future audio required');
    await appendTo(24000);
    const delta = await event('conversation.item.input_audio_transcription.delta', start,
      (v) => v.delta.length > 0);
    check(!events.slice(start).some((v) => v.type === 'input_audio_buffer.committed' ||
      v.type === 'conversation.item.input_audio_transcription.completed'), 'premature commit');
    before = { item_id: delta.item_id, sent_bytes: at, total_bytes: audio.length, delta: delta.delta };
    await absent(delta.item_id, 'uncommitted');
  }
  await appendTo(audio.length);
  // Earlier expected errors remain in the evidence, but cannot mask a new error.
  const commitStart = events.length;
  send({ type: 'input_audio_buffer.commit' });
  const committed = await event('input_audio_buffer.committed', commitStart);
  const item = committed.item_id;
  let terminal;
  await until(() => {
    const recent = events.slice(commitStart);
    const error = recent.find((v) => v.type === 'error');
    if (error) throw new Error('server error: ' + error.error.code);
    terminal = recent.find((v) => v.item_id === item &&
      ['conversation.item.input_audio_transcription.completed', 'conversation.item.input_audio_transcription.failed'].includes(v.type));
    return terminal;
  });
  check(terminal.type.endsWith('.completed'), 'transcription failed: ' + terminal.error?.code);
  const delta = events.slice(start).filter((v) => v.item_id === item &&
    v.type === 'conversation.item.input_audio_transcription.delta').map((v) => v.delta).join('');
  check(delta === terminal.transcript && (!before || before.item_id === item), 'actual delta/final identity');
  const retrieved = await retrieve(item);
  check(retrieved.item.content[0].transcript === terminal.transcript, 'retained actual transcript');
  turns.push({ item_id: item, committed, terminal, delta, retrieved, before_commit: before });
}
try {
  await event('session.created', 0);
  const start = events.length;
  send({ type: 'session.update', session: args.settings });
  const update = await event('session.updated', start);
  deepStrictEqual(update.session.audio.input, { ...args.settings.audio.input,
    transcription: { language: '', prompt: '', languages: [], keywords: [],
      ...args.settings.audio.input.transcription } }, 'effective audio settings');
  if (args.mode === 'manual') {
    await transcribe(false);
    await transcribe(false);
    await remove(turns[0].item_id);
    await remove(turns[1].item_id);
    await transcribe(false);
    check(turns[2].committed.previous_item_id === null, 'deleted history predecessor retained');
  } else {
    check(typeof args.foreign_id === 'string' && args.foreign_id.length > 0, 'foreign session probe required');
    await absent(args.foreign_id, 'foreign-session');
    await transcribe(true);
  }
  rt.close(); await until(() => closed, true);
  check(connections === 1 && closeCode === 1000, 'connection boundary');
  for (const deleted of checks.filter((v) => v.type === 'deleted')) {
    const index = events.findIndex((v) => v.event_id === deleted.event.event_id);
    check(!events.slice(index + 1).some((v) => v.item_id === deleted.item_id &&
      v.type.startsWith('conversation.item.input_audio_transcription.')), 'deleted item revived');
  }
  process.stdout.write(JSON.stringify({ status: 'PASS', connections, close_code: closeCode,
    events, turns, checks, event_bytes: bytes, sent_events: sent, automatic_replay: false }) + '\n');
} catch (error) {
  rt.socket.terminate();
  process.stdout.write(JSON.stringify({ status: 'FAIL', error: error.message, connections,
    events, turns, checks, event_bytes: bytes, sent_events: sent }) + '\n');
  process.exitCode = 1;
} finally { clearTimeout(guard); }
