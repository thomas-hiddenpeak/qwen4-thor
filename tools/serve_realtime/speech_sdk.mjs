// Bounded official SDK actor; every waveform comes from the owned real server.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { check, terminal, usage, text, functionCall, streamAgrees } from './conversation_checks.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part; check(Buffer.byteLength(input) <= 65536, 'input bound');
}
const args = JSON.parse(input), sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR);
for (const [name, version] of [['openai', '7.47.0'], ['ws', '8.21.0']])
  check(JSON.parse(fs.readFileSync(path.join(sdk, 'node_modules', name,
    'package.json'))).version === version, 'SDK identity');
const { default: OpenAI } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/index.mjs')));
const { OpenAIRealtimeWS } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/realtime/ws.mjs')));
const url = new URL(args.base_url);
check(url.protocol === 'https:' && url.hostname === '127.0.0.1' &&
  url.pathname === '/v1' && !url.search && !url.hash && !url.username &&
  !url.password && args.ca_file && ['main', 'quota', 'content'].includes(args.mode), 'TLS scope');
const client = new OpenAI({ baseURL: url.toString(), apiKey: args.api_key, maxRetries: 0 });
const rt = new OpenAIRealtimeWS({ model: 'qwen3.8-flash-next', options: {
  ca: fs.readFileSync(args.ca_file), maxPayload: 4 << 20, perMessageDeflate: false,
  followRedirects: false, handshakeTimeout: 10000,
} }, client);
let fatal = null, closed = false, closeCode = null, connections = 0;
let bytes = 0, sent = 0, creates = 0;
const events = [], results = [], retrievals = [], checks = [], quality = [];
const deadline = Date.now() + 360000;
const guard = setTimeout(() => {
  fatal = new Error('absolute SDK deadline'); rt.socket.terminate();
}, 360000);
rt.socket.on('open', () => { ++connections; });
rt.socket.on('close', (code) => { closed = true; closeCode = code; });
rt.on('error', (error) => {
  if (!error.error) { fatal = new Error('SDK transport: ' +
    (error.cause?.code || error.name)); rt.socket.terminate(); }
});
rt.on('event', (event) => {
  bytes += Buffer.byteLength(JSON.stringify(event));
  if (bytes > (12 << 20) || events.length >= 8192) {
    fatal = new Error('bounded event storage'); rt.socket.terminate(); return;
  }
  events.push(event);
});
async function until(predicate, allowClosed = false) {
  for (;;) {
    if (fatal) throw fatal;
    if (predicate()) return;
    check(Date.now() < deadline && (allowClosed || !closed), 'deadline or closed');
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
}
function send(value) { check(++sent <= 96, 'client event bound'); rt.send(value); }
async function action(value, type, predicate = () => true) {
  const at = events.length; send(value);
  let found;
  await until(() => {
    const fresh = events.slice(at);
    const error = fresh.find((e) => e.type === 'error');
    if (error && type !== 'error') throw new Error('event rejected: ' + error.error.code);
    found = fresh.find((e) => e.type === type && predicate(e)); return Boolean(found);
  });
  return found;
}
async function retrieve(id, label) {
  const value = await action({ type: 'conversation.item.retrieve', item_id: id },
    'conversation.item.retrieved', (e) => e.item?.id === id);
  retrievals.push({ label, item: value.item }); return value.item;
}
function judge(label, actual, expected) {
  quality.push({ label, actual, expected, status: actual === expected ? 'PASS' : 'FAIL' });
}
async function create(label, response, expected = 'completed', truncate = false) {
  check(++creates <= ({ main: 7, quota: 2, content: 5 }[args.mode]), 'response attempt bound');
  const at = events.length;
  const created = await action({ type: 'response.create', response: {
    ...response, metadata: { acceptance_case: label } } }, 'response.created');
  const id = created.response.id;
  if (truncate) {
    let first;
    await until(() => {
      check(!events.slice(at).some((e) => e.type === 'response.done'),
        'response finished before actual playback interruption');
      first = events.slice(at).find((e) => e.type === 'response.output_audio.delta' &&
        e.response_id === id && Buffer.from(e.delta, 'base64').length >= 960);
      return Boolean(first);
    });
    const ack = await action({ type: 'conversation.item.truncate',
      item_id: first.item_id, content_index: 0, audio_end_ms: 20 },
    'conversation.item.truncated', (e) => e.item_id === first.item_id);
    checks.push({ type: 'active_truncate', response_id: id,
      item_id: first.item_id, event_id: ack.event_id, milliseconds: 20 });
  }
  await until(() => events.slice(at).some((e) => e.type === 'response.done' &&
    e.response?.id === id));
  const value = terminal(events, id, expected);
  results.push({ label, response: value });
  if (expected !== 'failed') usage(value);
  if (response.output_modalities?.[0] === 'text' && expected === 'completed')
    streamAgrees(events, value);
  const item = value.output.find((v) => v.type === 'message' &&
    v.content.some((p) => p.type === 'output_audio'));
  if (item && expected !== 'failed') await retrieve(item.id, label);
  return value;
}
const phrase = 'Hello, this is a native speech test.';
const independent = { conversation: 'none', input: [], tools: [], tool_choice: 'none' };
const audioBase = { output_modalities: ['audio'], max_output_tokens: 3457 };
async function userTurn(label, task) {
  const id = 'user_' + label;
  await action({ type: 'conversation.item.create', item: { id,
    type: 'message', role: 'user', content: [{ type: 'input_text', text: task }] } },
  'conversation.item.done', (e) => e.item?.id === id);
}
let toolRecord = null;
try {
  await until(() => events.some((e) => e.type === 'session.created') &&
    events.some((e) => e.type === 'conversation.created'));
  await action({ type: 'session.update', session: { type: 'realtime',
    audio: { output: { voice: 'vivian', speed: 1,
      format: { type: 'audio/pcm', rate: 24000 } } },
    output_modalities: ['text'], max_output_tokens: 3457 } }, 'session.updated');
  if (args.mode === 'quota') {
    const spoken = await create('quota_audio', { ...audioBase,
      instructions: 'Repeat exactly without adding words: ' + phrase });
    judge('quota_audio', spoken.output[0].content[0].transcript.trim(), phrase);
    await create('quota_refused', { ...independent, output_modalities: ['text'],
      max_output_tokens: 32, instructions: 'Compute 6 * 7. Reply only 42.' }, 'failed');
  } else {
    const raw = fs.readFileSync(args.record_file);
    check(raw.length <= (1 << 20) && crypto.createHash('sha256').update(raw)
      .digest('hex') === args.record_sha256, 'actual project record identity');
    const version = 'rev' + raw.toString().match(/默认\s*rev(\d+)/)?.[1];
    check(version === args.expected_version, 'actual release version');
    await action({ type: 'session.update', session: { type: 'realtime',
      tools: [{ type: 'function', name: 'lookup_project_record',
        description: 'Read the actual local project release record.', parameters: {
          type: 'object', properties: { record_key: { type: 'string', enum: ['release'] } },
          required: ['record_key'], additionalProperties: false } }] } }, 'session.updated');
    await action({ type: 'conversation.item.create', item: { id: 'user_lookup',
      type: 'message', role: 'user', content: [{ type: 'input_text', text:
        'Call lookup_project_record for release. After its result reply only with the version.' }] } },
    'conversation.item.done', (e) => e.item?.id === 'user_lookup');
    const call = functionCall(await create('tool', { output_modalities: ['text'],
      max_output_tokens: 128, tool_choice: { type: 'function', name: 'lookup_project_record' } }));
    toolRecord = { record_key: 'release', version, source: 'docs/STATUS.md',
      source_sha256: args.record_sha256 };
    await action({ type: 'conversation.item.create', item: { id: 'tool_result',
      type: 'function_call_output', call_id: call.call_id,
      output: JSON.stringify(toolRecord) } }, 'conversation.item.done',
    (e) => e.item?.id === 'tool_result');
    if (args.mode === 'content') await userTurn('tool_answer',
      'What is the version in the preceding tool result? Reply only with the version.');
    const answer = await create('tool_audio', { ...audioBase, tools: [], tool_choice: 'none',
      instructions: 'Reply only with the exact version field in the actual tool result.' });
    judge('tool_audio', answer.output[0].content[0].transcript.trim(), version);
    if (args.mode === 'main') {
      const denied = await action({ type: 'session.update', event_id: 'locked_voice',
        session: { type: 'realtime', audio: { output: { voice: 'ryan' } } } }, 'error');
      check(denied.error.event_id === 'locked_voice', 'voice error ownership');
      checks.push({ type: 'locked_voice', error: denied.error });
    }
    for (const [label, format, speed] of [['pcm', 'audio/pcm', 1],
      ['mulaw', 'audio/pcmu', 1], ['alaw', 'audio/pcma', .75]]) {
      const updated = await action({ type: 'session.update', session: {
        type: 'realtime', audio: { output: { speed } } } }, 'session.updated');
      check(updated.session.audio.output.speed === speed, 'effective fractional speed');
      checks.push({ type: 'speed', label, speed });
      if (args.mode === 'content') await userTurn(label,
        'Repeat exactly without adding words: ' + phrase);
      const value = await create(label, { ...audioBase, tools: [], tool_choice: 'none',
        audio: { output: { format: format === 'audio/pcm' ?
          { type: format, rate: 24000 } : { type: format } } },
        instructions: 'Ignore earlier questions. Repeat exactly without adding words: ' + phrase });
      judge(label, value.output[0].content[0].transcript.trim(), phrase);
    }
    if (args.mode === 'main') {
    const item = results.find((v) => v.label === 'pcm').response.output[0].id;
    await action({ type: 'conversation.item.truncate', item_id: item,
      content_index: 0, audio_end_ms: 100 }, 'conversation.item.truncated');
    await retrieve(item, 'pcm_truncated');
    await action({ type: 'conversation.item.delete', item_id: item }, 'conversation.item.deleted');
    const missing = await action({ type: 'conversation.item.retrieve', item_id: item,
      event_id: 'deleted_audio' }, 'error');
    check(missing.error.event_id === 'deleted_audio', 'deleted history error owner');
    checks.push({ type: 'deleted', item_id: item, error: missing.error });
    await action({ type: 'session.update', session: { type: 'realtime',
      audio: { output: { speed: 1 } } } }, 'session.updated');
    await create('cancel', { ...audioBase, tools: [], tool_choice: 'none', instructions:
      'Say exactly: The service is ready. We will now read the project release record and explain the current status.' },
    'cancelled', true);
    const last = await create('after_cancel', { ...independent,
      output_modalities: ['text'], max_output_tokens: 32,
      instructions: 'Compute 6 * 7. Reply only 42.' });
    judge('after_cancel', text(last).trim(), '42');
    }
  }
  rt.close(); await until(() => closed, true); clearTimeout(guard);
  check(closeCode === 1000 && connections === 1, 'one clean SDK connection');
  process.stdout.write(JSON.stringify({ status: 'PASS', mode: args.mode,
    transport: 'official-openai-wss', connections, close_code: closeCode,
    response_creates: creates, automatic_replay: false, tool_record: toolRecord,
    responses: results, retrievals, checks, quality, events }));
} catch (error) {
  clearTimeout(guard); rt.socket.terminate();
  process.stdout.write(JSON.stringify({ status: 'FAIL', error: error.message,
    mode: args.mode, connections, close_code: closeCode, response_creates: creates,
    automatic_replay: false, tool_record: toolRecord,
    responses: results, retrievals, checks, quality, events }));
  process.exitCode = 1;
}
