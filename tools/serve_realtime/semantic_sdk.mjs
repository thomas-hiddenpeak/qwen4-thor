// Actual OpenAI WSS SDK actor. It never supplies a transcript to the server.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const check = (ok, message) => { if (!ok) throw new Error(message); };
const MAX = 4 * 1024 * 1024;
let input = '';
for await (const part of process.stdin) {
  input += part;
  check(Buffer.byteLength(input) <= 65536, 'input bound');
}
const args = JSON.parse(input);
const sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR);
for (const [name, version] of [['openai', '7.47.0'], ['ws', '8.21.0']])
  check(JSON.parse(fs.readFileSync(path.join(sdk, 'node_modules', name,
    'package.json'))).version === version, 'SDK version');
const { default: OpenAI } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/index.mjs')));
const { OpenAIRealtimeWS } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/realtime/ws.mjs')));
const url = new URL(args.base_url);
check(url.protocol === 'https:' && url.hostname === '127.0.0.1' &&
  url.pathname === '/v1' && !url.search && !url.hash && !url.username &&
  !url.password, 'explicit loopback TLS required');
const audio = fs.readFileSync(args.file);
check(audio.length > 0 && audio.length <= 15 * 48000 && audio.length % 2 === 0,
  'bounded actual mono 24k PCM16');
check(Number.isInteger(args.split_samples) && args.split_samples >= 0 &&
  args.split_samples * 2 < audio.length, 'split position');
check(args.settings?.audio?.input?.turn_detection?.type === 'semantic_vad',
  'semantic policy required');
const client = new OpenAI({ baseURL: url.toString(), apiKey: args.api_key, maxRetries: 0 });
const rt = new OpenAIRealtimeWS({ intent: 'transcription', options: {
  maxPayload: MAX, perMessageDeflate: false, followRedirects: false,
  handshakeTimeout: 10000, ca: fs.readFileSync(args.ca_file) } }, client);
let fatal = null, closed = false, closeCode = null, connections = 0;
let bytes = 0, sent = 0, samples = 0;
const events = [], feed = [];
const deadline = Date.now() + 120000;
const guard = setTimeout(() => {
  fatal = new Error('SDK absolute deadline'); rt.socket.terminate();
}, 120000);
rt.socket.on('open', () => { ++connections; });
rt.socket.on('close', (code) => { closed = true; closeCode = code; });
rt.on('error', (error) => {
  if (!error.error) {
    fatal = new Error('SDK transport: ' + (error.cause?.code || error.name));
    rt.socket.terminate();
  }
});
rt.on('event', (value) => {
  bytes += Buffer.byteLength(JSON.stringify(value));
  if (bytes > MAX || events.length >= 2048) {
    fatal = new Error('event bound'); rt.socket.terminate(); return;
  }
  events.push(value);
});
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(predicate, allowClosed = false) {
  for (;;) {
    if (fatal) throw fatal;
    const failure = events.find((v) => v.type === 'error' ||
      v.type === 'conversation.item.input_audio_transcription.failed');
    if (failure) throw new Error('server: ' + failure.error.code);
    if (predicate()) return;
    check(Date.now() < deadline && (allowClosed || !closed), 'deadline or closed');
    await pause(2);
  }
}
function send(value) {
  check(++sent <= 256, 'client event bound'); rt.send(value);
}
async function append(raw, label) {
  const first = samples;
  const started = Date.now();
  for (let at = 0; at < raw.length; at += 4800) {
    await until(() => rt.socket.bufferedAmount <= 1048576);
    const block = raw.subarray(at, Math.min(at + 4800, raw.length));
    send({ type: 'input_audio_buffer.append', audio: block.toString('base64') });
    samples += block.length / 2;
    await pause(block.length * 1000 / 48000);
  }
  feed.push({ label, begin_samples: first, end_samples: samples,
    elapsed_ms: Date.now() - started });
}
let continuation = null;
try {
  await until(() => events.some((v) => v.type === 'session.created'));
  send({ type: 'session.update', session: args.settings });
  await until(() => events.some((v) => v.type === 'session.updated'));
  await append(Buffer.alloc(24000), 'leading_silence');
  if (args.split_samples) {
    await append(audio.subarray(0, args.split_samples * 2), 'unfinished_prefix');
    await append(Buffer.alloc(24000), 'continuation_pause');
    check(!events.some((v) => v.type === 'input_audio_buffer.committed'),
      'unfinished speech was committed before continuation');
    continuation = { original_samples_sent: args.split_samples,
      total_original_samples: audio.length / 2, pause_samples: 12000,
      no_commit: true, event_count: events.length };
    await append(audio.subarray(args.split_samples * 2), 'continuation');
  } else {
    await append(audio, 'complete_utterance');
  }
  await append(Buffer.alloc(3 * 48000), 'trailing_silence');
  await until(() => events.some((v) =>
    v.type === 'conversation.item.input_audio_transcription.completed'));
  const completed = events.filter((v) =>
    v.type === 'conversation.item.input_audio_transcription.completed');
  const commits = events.filter((v) => v.type === 'input_audio_buffer.committed');
  const starts = events.filter((v) => v.type === 'input_audio_buffer.speech_started');
  const stops = events.filter((v) => v.type === 'input_audio_buffer.speech_stopped');
  check(completed.length === 1 && commits.length === 1 && starts.length === 1 &&
    stops.length === 1, 'exactly one actual turn');
  const terminal = completed[0], id = terminal.item_id;
  check([commits[0], starts[0], stops[0]].every((v) => v.item_id === id),
    'turn identity');
  const delta = events.filter((v) => v.item_id === id &&
    v.type === 'conversation.item.input_audio_transcription.delta').map((v) => v.delta).join('');
  check(delta === terminal.transcript && delta.length > 0, 'actual transcript stream');
  const semantic = terminal.q4t_semantic_vad;
  check(semantic?.profile === 'q4t-native-semantic-turn-v1' &&
    semantic.accepted > 0 && semantic.input_tokens > 0 && semantic.output_tokens > 0 &&
    semantic.included_in_usage === true, 'real classifier and included work required');
  send({ type: 'conversation.item.retrieve', item_id: id });
  await until(() => events.some((v) => v.type === 'conversation.item.retrieved' && v.item.id === id));
  const retrieved = events.find((v) => v.type === 'conversation.item.retrieved');
  check(retrieved.item.content[0].transcript === terminal.transcript &&
    retrieved.item.content[0].audio.length > 0, 'retained real turn');
  rt.close(); await until(() => closed, true);
  check(closeCode === 1000 && connections === 1, 'single orderly connection');
  process.stdout.write(JSON.stringify({ status: 'PASS', connections,
    close_code: closeCode, automatic_replay: false, events, feed,
    continuation, terminal, delta, retrieved, sent_samples: samples }) + '\n');
} catch (error) {
  rt.socket.terminate();
  process.stdout.write(JSON.stringify({ status: 'FAIL', error: String(error),
    connections, close_code: closeCode, events, feed, continuation,
    sent_samples: samples, automatic_replay: false }) + '\n');
  process.exitCode = 1;
} finally { clearTimeout(guard); }
