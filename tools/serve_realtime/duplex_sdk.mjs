// Bounded actual duplex actor. No reconnection, replay or secondary model.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { check, terminal, usage, text, functionCall, streamAgrees } from './conversation_checks.mjs';
import { openJournal } from './bounded_journal.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part; check(Buffer.byteLength(input) <= 65536, 'argument bound');
}
const args = JSON.parse(input), sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR);
const journal = openJournal(args.journal_file);
process.once('exit', () => journal.close());
for (const [name, version] of [['openai', '7.47.0'], ['ws', '8.21.0']])
  check(JSON.parse(fs.readFileSync(path.join(sdk, 'node_modules', name, 'package.json')))
    .version === version, 'fixed SDK version');
const { default: OpenAI } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/index.mjs')));
const { OpenAIRealtimeWS } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/realtime/ws.mjs')));
const url = new URL(args.base_url);
check(url.protocol === 'https:' && url.hostname === '127.0.0.1' && url.pathname === '/v1'
  && !url.search && !url.hash && !url.username && !url.password && args.ca_file &&
  ['manual', 'vad', 'idle', 'mixed', 'vad_once'].includes(args.mode), 'verified local TLS scope');
const clip = fs.readFileSync(args.clip);
check(clip.length > 0 && clip.length <= (1 << 20) && clip.length % 2 === 0 &&
  crypto.createHash('sha256').update(clip).digest('hex') === args.clip_sha256, 'actual clip identity');
const client = new OpenAI({ baseURL: url.toString(), apiKey: args.api_key, maxRetries: 0 });
const rt = new OpenAIRealtimeWS({ model: 'qwen3.8-flash-next', options: {
  ca: fs.readFileSync(args.ca_file), maxPayload: 4 << 20, perMessageDeflate: false,
  followRedirects: false, handshakeTimeout: 10000,
} }, client);
let fatal = null, closed = false, closeCode = null, connections = 0, bytes = 0, sent = 0, creates = 0;
const events = [], responses = [], retrievals = [], checks = [], quality = [];
const deadline = Date.now() + 420000;
const guard = setTimeout(() => { fatal = new Error('SDK deadline'); rt.socket.terminate(); }, 420000);
rt.socket.on('open', () => { ++connections; });
rt.socket.on('close', (code) => { closed = true; closeCode = code; });
rt.on('error', (error) => {
  if (!error.error) { fatal = new Error('SDK transport: ' + (error.cause?.code || error.name)); rt.socket.terminate(); }
});
rt.on('event', (value) => {
  bytes += Buffer.byteLength(JSON.stringify(value));
  if (bytes > (12 << 20) || events.length >= 8192) {
    fatal = new Error('event bound'); rt.socket.terminate(); return;
  }
  try { journal.append('server', value); } catch (error) {
    fatal = error; rt.socket.terminate(); return;
  }
  events.push(value);
});
async function until(predicate, allowClosed = false) {
  for (;;) {
    if (fatal) throw fatal;
    if (predicate()) return;
    check(Date.now() < deadline && (allowClosed || !closed), 'deadline or closed');
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
}
function send(value) {
  check(++sent <= 200, 'event attempt bound');
  journal.append('client', value); rt.send(value);
}
async function waitEvent(at, type, predicate = () => true) {
  let found;
  await until(() => {
    const fresh = events.slice(at), error = fresh.find((e) => e.type === 'error');
    if (error && type !== 'error') throw new Error('event rejected: ' + error.error.code);
    found = fresh.find((e) => e.type === type && predicate(e)); return Boolean(found);
  });
  return found;
}
async function action(value, type, predicate) {
  const at = events.length; send(value); return waitEvent(at, type, predicate);
}
async function update(input) {
  return action({ type: 'session.update', session: { type: 'realtime', ...input } }, 'session.updated');
}
async function retrieve(id, label) {
  const value = await action({ type: 'conversation.item.retrieve', item_id: id },
    'conversation.item.retrieved', (e) => e.item?.id === id);
  retrievals.push({ label, item: value.item }); return value.item;
}
async function missing(id) {
  const tag = 'missing_' + id;
  const value = await action({ type: 'conversation.item.retrieve', item_id: id, event_id: tag }, 'error');
  check(value.error.code === 'realtime_item_not_found' && value.error.event_id === tag,
    'temporary input not cleaned');
  checks.push({ type: 'missing', id, event_id: value.event_id });
}
function judge(label, actual, expected) {
  // Raw answers and exact normalized verdict are retained, including failures.
  const normalize = (s) => s.replace(/[\s。，,.!！?？]/g, '');
  quality.push({ label, actual, expected,
    status: normalize(actual) === normalize(expected) ? 'PASS' : 'FAIL' });
}
async function finish(label, id, expected = 'completed') {
  const at = events.findIndex((e) => e.type === 'response.created' && e.response?.id === id);
  check(at >= 0, 'response creation owner');
  await waitEvent(at, 'response.done', (e) => e.response?.id === id);
  const response = terminal(events, id, expected);
  responses.push({ label, response });
  usage(response);
  if (expected === 'completed' && response.output_modalities[0] === 'text') streamAgrees(events, response);
  return response;
}
async function begin(label, response) {
  check(++creates <= (args.mode === 'manual' ? 6 : 1), 'response attempt bound');
  const value = await action({ type: 'response.create', response: {
    ...response, metadata: { acceptance_case: label },
  } }, 'response.created');
  return value.response.id;
}
async function create(label, response) { return finish(label, await begin(label, response)); }
const inputText = (value) => ({ type: 'input_text', text: value });
const user = (id, content) => ({ type: 'message', role: 'user', id, content });
const audio = (raw, transcript) => ({ type: 'input_audio', audio: raw.toString('base64'),
  ...(transcript === undefined ? {} : { transcript }) });
async function add(item) {
  return action({ type: 'conversation.item.create', item }, 'conversation.item.done', (e) => e.item?.id === item.id);
}
function append(raw) {
  for (let offset = 0; offset < raw.length; offset += 24000)
    send({ type: 'input_audio_buffer.append', audio: raw.subarray(offset, offset + 24000).toString('base64') });
}
const originalInstructions = 'Repeat only the first sentence from the latest user audio verbatim. '
  + 'Do not add explanations. If no speech was detected during the idle interval, reply exactly READY.';
check(args.instruction_policy === undefined || args.instruction_policy === 'original_language_v1',
  'unknown caller instruction policy');
const instructions = args.instruction_policy === undefined ? originalInstructions :
  '逐字抄写本条用户消息中第一段语音的原始转写，仅输出其中第一个完整句子一次。保持原语言，不翻译，不包含前后的文字说明或第二段语音，不增加任何解释。如果收到的是原生空闲未检测到语音的提示，只输出 READY。';
const base = { conversation: 'none', output_modalities: ['text'], tools: [], tool_choice: 'none',
  max_output_tokens: 128, instructions };
let toolRecord = null;
try {
  await waitEvent(0, 'session.created'); await waitEvent(0, 'conversation.created');
  await update({ instructions, output_modalities: ['text'], max_output_tokens: 128,
    audio: { input: { format: { type: 'audio/pcm', rate: 24000 }, turn_detection: null,
      noise_reduction: null, transcription: null },
    output: { voice: 'vivian', format: { type: 'audio/pcm', rate: 24000 } } } });
  if (args.mode === 'mixed') {
    await update({ audio: { input: { transcription: { model: 'qwen3-asr-0.6b' } } } });
    await add(user('mixed_input', [inputText('Repeat the first audio sentence only.'),
      audio(clip, 'REFERENCE_ONLY_NEVER_SPOKEN'), inputText('Second audio follows.'), audio(clip)]));
    const mixed = await create('mixed', { ...base, input: [{ type: 'item_reference', id: 'mixed_input' }] });
    judge('mixed', text(mixed), args.expected);
    await retrieve('mixed_input', 'mixed');
  } else if (args.mode === 'vad_once') {
    await update({ audio: { input: { transcription: { model: 'qwen3-asr-0.6b' },
      turn_detection: { type: 'server_vad', threshold: .5, prefix_padding_ms: 300,
        silence_duration_ms: 300, create_response: true, interrupt_response: true,
        idle_timeout_ms: null } } } });
    const at = events.length;
    append(Buffer.concat([Buffer.alloc(28800), clip, Buffer.alloc(57600)]));
    const auto = await waitEvent(at, 'response.created');
    judge('vad_auto', text(await finish('vad_auto', auto.response.id)), args.expected);
  } else if (args.mode === 'manual') {
    append(clip);
    const committed = await action({ type: 'input_audio_buffer.commit' }, 'input_audio_buffer.committed');
    const manual = await create('manual_private', { ...base, input: [{ type: 'item_reference', id: committed.item_id }] });
    judge('manual_private', text(manual), args.expected);
    const stored = await retrieve(committed.item_id, 'manual_private');
    check(!stored.content[0].transcript && stored.content[0].audio, 'private waveform/history');
    check(!events.some((e) => e.type.startsWith('conversation.item.input_audio_transcription.') &&
      e.item_id === committed.item_id), 'private ASR leaked');
    checks.push({ type: 'private', id: committed.item_id });
    await update({ audio: { input: { transcription: { model: 'qwen3-asr-0.6b' } } } });
    await add(user('mixed_input', [inputText('Repeat the first audio sentence only.'),
      audio(clip, 'REFERENCE_ONLY_NEVER_SPOKEN'), inputText('Second audio follows.'), audio(clip)]));
    const mixed = await create('mixed', { ...base, input: [{ type: 'item_reference', id: 'mixed_input' }] });
    judge('mixed', text(mixed), args.expected);
    await retrieve('mixed_input', 'mixed');
    const spoken = await create('inline_audio', { ...base, output_modalities: ['audio'], max_output_tokens: 3457,
      input: [user('temporary_audio', [inputText('Repeat this sentence.'), audio(clip)])] });
    const item = spoken.output.find((v) => v.type === 'message');
    check(item?.content[0].type === 'output_audio', 'actual native speech');
    judge('inline_audio', item.content[0].transcript, args.expected);
    const waveform = await retrieve(item.id, 'inline_audio');
    await missing('temporary_audio');
    const replay = Buffer.from(waveform.content[0].audio, 'base64');
    check(replay.length > 0 && replay.length <= (2 << 20), 'actual generated speech bound');
    const roundtrip = await create('roundtrip', { ...base,
      input: [user('temporary_roundtrip', [audio(replay)])] });
    judge('roundtrip', text(roundtrip), args.expected);
    await missing('temporary_roundtrip');
    const raw = fs.readFileSync(args.record_file);
    check(raw.length <= (1 << 20) && crypto.createHash('sha256').update(raw).digest('hex') ===
      args.record_sha256, 'actual project source');
    const version = 'rev' + raw.toString().match(/默认\s*rev(\d+)/)?.[1];
    check(version === args.expected_version, 'current release version');
    await add(user('query_release', [inputText('Call lookup_project_record for release.')]));
    const call = functionCall(await create('tool', { output_modalities: ['text'], max_output_tokens: 128,
      instructions: 'Use lookup_project_record to read the release record requested in the latest user message.',
      tools: [{ type: 'function', name: 'lookup_project_record', parameters: { type: 'object',
        properties: { record_key: { type: 'string', enum: ['release'] } },
        required: ['record_key'], additionalProperties: false } }],
      tool_choice: { type: 'function', name: 'lookup_project_record' } }));
    toolRecord = { record_key: 'release', version, source: 'docs/STATUS.md', source_sha256: args.record_sha256 };
    await add({ id: 'release_result', type: 'function_call_output', call_id: call.call_id,
      output: JSON.stringify(toolRecord) });
    await add(user('query_version', [inputText('What version is in the preceding tool result? Reply only the version.')]));
    const answer = await create('tool_answer', { output_modalities: ['text'], max_output_tokens: 128,
      tools: [], tool_choice: 'none', instructions: 'Reply only with the version from the actual tool result.' });
    judge('tool_answer', text(answer), version);
  } else {
    const vad = { type: 'server_vad', threshold: .5, prefix_padding_ms: 300,
      silence_duration_ms: 300, create_response: true, interrupt_response: true,
      idle_timeout_ms: 5000 };
    await update({ audio: { input: { transcription: { model: 'qwen3-asr-0.6b' }, turn_detection: vad } } });
    const padded = Buffer.concat([Buffer.alloc(28800), clip, Buffer.alloc(57600)]);
    const at = events.length; append(padded);
    const auto = await waitEvent(at, 'response.created');
    const answer = await finish('vad_auto', auto.response.id);
    judge('vad_auto', text(answer), args.expected);
    const idle = await waitEvent(at, 'input_audio_buffer.timeout_triggered');
    checks.push({ type: 'idle', item_id: idle.item_id, event_id: idle.event_id });
    const idleAt = events.indexOf(idle);
    const next = await waitEvent(idleAt, 'response.created');
    const empty = await finish('idle', next.response.id);
    judge('idle', text(empty), 'READY');
    await update({ audio: { input: { turn_detection: { ...vad, idle_timeout_ms: null, create_response: false } } } });
    if (args.mode === 'vad') {
      const start = events.length;
      const id = await begin('interrupt', { output_modalities: ['text'], max_output_tokens: 1024,
        tools: [], tool_choice: 'none', instructions: 'Count from 1 to 200, writing every integer on a separate line without skipping any.' });
      await waitEvent(start, 'response.output_text.delta', (e) => e.response_id === id && e.delta.length > 0);
      append(padded);
      const speech = await waitEvent(start, 'input_audio_buffer.speech_started');
      const interrupted = await finish('interrupt', id, 'cancelled');
      check(interrupted.id === id, 'interruption response owner');
      await waitEvent(start, 'conversation.item.input_audio_transcription.completed', (e) => e.item_id === speech.item_id);
      checks.push({ type: 'interrupt', response_id: id, item_id: speech.item_id, event_id: speech.event_id });
      check(events.filter((e) => e.type === 'response.created').length === 3, 'unexpected automatic answer');
    } else {
      check(events.filter((e) => e.type === 'response.created').length === 2, 'unexpected idle answer');
    }
  }
  rt.close(); await until(() => closed, true); clearTimeout(guard);
  check(closeCode === 1000 && connections === 1, 'one clean connection');
  process.stdout.write(JSON.stringify({ status: 'PASS', mode: args.mode, transport: 'official-openai-wss',
    connections, close_code: closeCode, automatic_replay: false, response_creates: creates,
    ...(args.instruction_policy ? { instruction_policy: args.instruction_policy } : {}),
    responses, retrievals, checks, quality, tool_record: toolRecord, events }));
} catch (error) {
  clearTimeout(guard); rt.socket.terminate();
  process.stdout.write(JSON.stringify({ status: 'FAIL', error: error.message, mode: args.mode,
    connections, close_code: closeCode, response_creates: creates, automatic_replay: false,
    responses, retrievals, checks, quality, tool_record: toolRecord, events }));
  process.exitCode = 1;
}
