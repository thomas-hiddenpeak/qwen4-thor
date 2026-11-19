// Bounded development acceptance client using the actual pinned OpenAI SDK.
// Not a production dependency, websocket substitute or automatic retry loop.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const MAX = 4 * 1024 * 1024;
const check = (value, message) => { if (!value) throw new Error(message); };
let input = '';
for await (const part of process.stdin) {
  input += part;
  check(Buffer.byteLength(input) <= 256 * 1024, 'input budget');
}
const args = JSON.parse(input);
const sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR);
check(JSON.parse(fs.readFileSync(path.join(sdk, 'node_modules/openai/package.json'))).version === '7.47.0', 'SDK identity');
check(JSON.parse(fs.readFileSync(path.join(sdk, 'node_modules/ws/package.json'))).version === '8.21.0', 'WebSocket dependency identity');
const { default: OpenAI } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/index.mjs')));
const { OpenAIRealtimeWS } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/realtime/ws.mjs')));
const url = new URL(args.base_url);
check(url.protocol === 'https:' &&
  ['127.0.0.1', 'localhost'].includes(url.hostname) && url.pathname === '/v1' &&
  !url.username && !url.password && !url.search && !url.hash, 'local HTTPS acceptance endpoint required by official WSS SDK');
check(Array.isArray(args.clips) && args.clips.length <= 5, 'turn budget');
const client = new OpenAI({ baseURL: url.toString(), apiKey: args.api_key || 'local-acceptance', maxRetries: 0 });
const options = { maxPayload: MAX, perMessageDeflate: false, followRedirects: false,
  handshakeTimeout: 10000, headers: args.headers || {} };
if (args.ca_file) options.ca = fs.readFileSync(args.ca_file);
const rt = new OpenAIRealtimeWS({ intent: 'transcription', options }, client);
const events = [], errors = [], turns = [];
let bytes = 0, closed = false, closeCode = null, fatal = null, connections = 0;
const deadline = Date.now() + 120000;
const guard = setTimeout(() => { fatal = new Error('client deadline'); rt.socket.terminate(); }, 120000);
rt.socket.on('open', () => { ++connections; });
rt.socket.on('close', (code) => { closed = true; closeCode = code; });
rt.on('error', (error) => {
  if (error.error) errors.push(error.error);
  else {
    fatal = new Error('SDK transport failed: ' + (error.cause?.code || error.name));
    rt.socket.terminate();
  }
});
rt.on('event', (event) => {
  bytes += Buffer.byteLength(JSON.stringify(event));
  if (bytes > MAX || events.length >= 4096) {
    fatal = new Error('event budget'); rt.socket.terminate(); return;
  }
  events.push(event);
});
async function until(predicate, allowClosed = false) {
  while (!predicate()) {
    if (fatal) throw fatal;
    check(Date.now() < deadline, 'client deadline');
    check(allowClosed || !closed, 'premature session close');
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
}
async function event(type, start, item) {
  let found;
  await until(() => {
    const recent = events.slice(start);
    const error = recent.find((v) => v.type === 'error');
    if (type !== 'error' && error) throw new Error('server error: ' + error.error.code);
    return (found = recent.find((v) => v.type === type && (!item || v.item_id === item)));
  });
  return found;
}
async function settings(session) {
  const start = events.length;
  rt.send({ type: 'session.update', session });
  return await event('session.updated', start);
}
try {
  await event('session.created', 0);
  if (args.probes) {
    let start = events.length;
    rt.send({ type: 'input_audio_buffer.append', event_id: 'bad-base64', audio: 'AB==' });
    const invalid = await event('error', start);
    check(invalid.error.event_id === 'bad-base64', 'error correlation');
    start = events.length;
    rt.send({ type: 'input_audio_buffer.commit', event_id: 'empty-buffer' });
    const empty = await event('error', start);
    check(empty.error.event_id === 'empty-buffer', 'empty commit correlation');
    start = events.length;
    rt.send({ type: 'input_audio_buffer.append', audio: 'AQ==' });
    rt.send({ type: 'input_audio_buffer.clear' });
    await event('input_audio_buffer.cleared', start);
  }
  for (const clip of args.clips) {
    if (clip.auto_items !== undefined) check(Number.isInteger(clip.auto_items) &&
      clip.auto_items >= 1 && clip.auto_items <= 3 && !clip.cancel_after_delta,
      'automatic turn budget');
    const update = await settings(clip.settings || { type: 'transcription',
      include: ['item.input_audio_transcription.logprobs'],
      audio: { input: { format: { type: 'audio/pcm', rate: 24000 },
        transcription: { model: 'qwen3-asr-0.6b' }, turn_detection: null,
        noise_reduction: null } } });
    check(fs.statSync(clip.file).size <= 2 * 1024 * 1024, 'audio byte budget');
    const audio = fs.readFileSync(clip.file);
    check(audio.length > 0, 'empty fixture');
    const start = events.length;
    let at = 0, part = 0;
    const sizes = [1, 7, 16381, 16000];
    while (at < audio.length) {
      check(part < 1024, 'append count');
      await until(() => rt.socket.bufferedAmount <= 1024 * 1024);
      const end = Math.min(at + sizes[part++ % sizes.length], audio.length);
      rt.send({ type: 'input_audio_buffer.append', audio: audio.subarray(at, end).toString('base64') });
      at = end;
    }
    if (clip.auto_items === undefined) rt.send({ type: 'input_audio_buffer.commit' });
    for (let ordinal = 0; ordinal < (clip.auto_items || 1); ++ordinal) {
      let committed;
      await until(() => {
        const recent = events.slice(start);
        const error = recent.find((v) => v.type === 'error');
        if (error) throw new Error('server error: ' + error.error.code);
        return (committed = recent.filter((v) =>
          v.type === 'input_audio_buffer.committed')[ordinal]);
      });
      const item = committed.item_id;
      if (clip.cancel_after_delta) {
        await event('conversation.item.input_audio_transcription.delta', start, item);
        rt.close();
        await until(() => closed, true);
        check(!events.slice(start).some((v) => v.item_id === item &&
          v.type === 'conversation.item.input_audio_transcription.completed'), 'cancel fabricated success');
        turns.push({ item_id: item, cancelled: true });
        break;
      }
      let terminal;
      await until(() => (terminal = events.slice(start).find((v) => v.item_id === item &&
        ['conversation.item.input_audio_transcription.completed',
          'conversation.item.input_audio_transcription.failed'].includes(v.type))));
      check(terminal.type.endsWith(clip.expect_failure ? '.failed' : '.completed'),
        'item terminal: ' + (terminal.error?.code || terminal.type));
      const delta = events.slice(start).filter((v) => v.item_id === item &&
        v.type === 'conversation.item.input_audio_transcription.delta').map((v) => v.delta).join('');
      if (!clip.expect_failure) check(delta === terminal.transcript, 'delta/final mismatch');
      else check(terminal.error.code === clip.expect_failure, 'unexpected failed item');
      turns.push({ item_id: item, terminal, delta, update });
    }
    if (clip.cancel_after_delta) break;
    if (clip.auto_items !== undefined) {
      // Bounded observation after the actual trailing-silence fixture. Exact
      // sample/idle correctness is independently covered by native CPU tests.
      await new Promise((resolve) => setTimeout(resolve, 300));
      check(events.slice(start).filter((v) =>
        v.type === 'input_audio_buffer.committed').length === clip.auto_items,
        'automatic item count differs');
      const end = events.length;
      rt.send({ type: 'input_audio_buffer.clear' });
      await event('input_audio_buffer.cleared', end);
    }
  }
  if (!closed) { rt.close(); await until(() => closed, true); }
  check(connections === 1 && closeCode === 1000, 'connection/close boundary');
  process.stdout.write(JSON.stringify({ status: 'PASS', connections, close_code: closeCode,
    events, turns, errors, event_bytes: bytes, automatic_replay: false }) + '\n');
} catch (error) {
  rt.socket.terminate();
  process.stdout.write(JSON.stringify({ status: 'FAIL', error: error.message,
    connections, events, turns, errors, event_bytes: bytes }) + '\n');
  process.exitCode = 1;
} finally {
  clearTimeout(guard);
}
