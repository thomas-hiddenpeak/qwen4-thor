// Official WSS client against the actual native Chat executor, never a mock LLM.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { check, terminal, usage, text, functionCall, streamAgrees } from './conversation_checks.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part;
  check(Buffer.byteLength(input) <= 65536, 'input bound');
}
const args = JSON.parse(input);
const sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR);
for (const [name, version] of [['openai', '7.47.0'], ['ws', '8.21.0']])
  check(JSON.parse(fs.readFileSync(path.join(sdk, 'node_modules', name,
    'package.json'))).version === version, 'SDK identity');
const { default: OpenAI } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/index.mjs')));
const { OpenAIRealtimeWS } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/realtime/ws.mjs')));
const url = new URL(args.base_url);
check(url.protocol === 'https:' && url.hostname === '127.0.0.1' &&
  url.pathname === '/v1' && !url.search && !url.hash &&
  !url.username && !url.password && args.ca_file, 'explicit verified TLS loopback');
check(['main', 'quota'].includes(args.mode), 'bounded scenario required');
const client = new OpenAI({ baseURL: url.toString(), apiKey: args.api_key, maxRetries: 0 });
const rt = new OpenAIRealtimeWS({ model: 'qwen3.8-flash-next', options: {
  ca: fs.readFileSync(args.ca_file), maxPayload: 4 << 20,
  perMessageDeflate: false, followRedirects: false, handshakeTimeout: 10000,
} }, client);
let fatal = null, closed = false, closeCode = null, connections = 0;
let bytes = 0, sent = 0, creates = 0;
const events = [], results = [];
const deadline = Date.now() + 240000;
const guard = setTimeout(() => {
  fatal = new Error('SDK absolute deadline'); rt.socket.terminate();
}, 240000);
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
  if (bytes > (4 << 20) || events.length >= 4096) {
    fatal = new Error('event storage bound'); rt.socket.terminate(); return;
  }
  events.push(value);
});
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(predicate, allowClosed = false) {
  for (;;) {
    if (fatal) throw fatal;
    if (predicate()) return;
    check(Date.now() < deadline && (allowClosed || !closed), 'deadline or closed');
    await pause(2);
  }
}
function send(value) {
  check(++sent <= 64, 'client event bound'); rt.send(value);
}
async function create(response, expected = 'completed', cancel = false) {
  check(++creates <= (args.mode === 'main' ? 8 : 2), 'response attempt bound');
  const at = events.length;
  const tag = `case_${creates}`;
  send({ type: 'response.create', event_id: tag,
    response: { ...response, metadata: { acceptance_case: tag } } });
  let id;
  await until(() => {
    const error = events.slice(at).find((event) => event.type === 'error');
    if (error) throw new Error('create rejected: ' + error.error.code);
    const created = events.slice(at).find((event) => event.type === 'response.created');
    id = created?.response.id;
    return Boolean(id);
  });
  if (cancel) {
    await until(() => {
      check(!events.slice(at).some((event) => event.type === 'response.done'),
        'response completed before actual delta cancellation');
      return events.slice(at).some((event) => event.type === 'response.output_text.delta' &&
        event.response_id === id && event.delta.length > 0);
    });
    send({ type: 'response.cancel', response_id: id });
  }
  await until(() => events.slice(at).some((event) => event.type === 'response.done' &&
    event.response?.id === id));
  const value = terminal(events, id, expected);
  if (expected !== 'failed') {
    usage(value);
    if (expected !== 'cancelled') streamAgrees(events, value);
  }
  results.push(value);
  return value;
}
async function add(item) {
  const at = events.length;
  send({ type: 'conversation.item.create', item });
  await until(() => {
    const error = events.slice(at).find((event) => event.type === 'error');
    if (error) throw new Error('item rejected: ' + error.error.code);
    return events.slice(at).some((event) => event.type === 'conversation.item.done' &&
      event.item?.id === item.id);
  });
}
const user = (id, value) => ({ type: 'message', id, role: 'user',
  content: [{ type: 'input_text', text: value }] });
let toolRecord = null;
try {
  await until(() => events.some((event) => event.type === 'session.created') &&
    events.some((event) => event.type === 'conversation.created'));
  const initial = events.find((event) => event.type === 'session.created').session;
  check(initial.type === 'realtime' && initial.model === 'qwen3.8-flash-next', 'session identity');
  if (args.mode === 'quota') {
    const first = await create({ conversation: 'none', input: [],
      instructions: 'Compute 6 * 7. Reply only 42.', output_modalities: ['text'],
      max_output_tokens: 32 });
    check(text(first).trim() === '42', 'quota first actual quality');
    const refused = await create({ conversation: 'none', input: [],
      instructions: 'Compute 6 * 7. Reply only 42.', max_output_tokens: 32 }, 'failed');
    check(refused.status_details?.error?.code === 'client_rpm_exhausted', 'actual RPM refusal');
  } else {
    check(fs.statSync(args.record_file).size <= (1 << 20), 'bounded project record');
    const record = fs.readFileSync(args.record_file);
    const digest = crypto.createHash('sha256').update(record).digest('hex');
    check(digest === args.record_sha256, 'project record changed');
    const version = record.toString().match(/默认\s*rev(\d+)/)?.[1];
    check(version && `rev${version}` === args.expected_version, 'actual default record');
    send({ type: 'session.update', session: { type: 'realtime',
      instructions: 'Use project records exactly. Only execute the declared lookup tool when requested.',
      output_modalities: ['text'], max_output_tokens: 128,
      tools: [{ type: 'function', name: 'lookup_project_record',
        description: 'Read the actual local project release record.', parameters: {
          type: 'object', properties: { record_key: { type: 'string', enum: ['release'] } },
          required: ['record_key'], additionalProperties: false } }],
    } });
    await until(() => events.some((event) => event.type === 'session.updated'));
    await add(user('user_lookup', 'Call lookup_project_record with record_key release. After receiving the result reply only with its version value.'));
    const call = functionCall(await create({
      tool_choice: { type: 'function', name: 'lookup_project_record' } }));
    // The allowlisted, read-only tool returns current project data only after
    // the actual model call has completed and its arguments have been checked.
    toolRecord = { record_key: 'release', version: `rev${version}`,
      source: 'docs/STATUS.md', source_sha256: digest };
    await add({ type: 'function_call_output', id: 'tool_result',
      call_id: call.call_id, output: JSON.stringify(toolRecord) });
    const answer = await create({ tools: [], tool_choice: 'none',
      instructions: 'Reply only with the exact version field from the actual tool result.' });
    check(text(answer).trim() === toolRecord.version, 'real project tool round trip quality');
    const before = events.filter((event) => event.type === 'response.created').length;
    send({ type: 'response.create', event_id: 'invalid_metadata',
      response: { metadata: { bad: 'x'.repeat(513) } } });
    await until(() => events.some((event) => event.type === 'error' &&
      event.error?.event_id === 'invalid_metadata'));
    check(events.filter((event) => event.type === 'response.created').length === before,
      'invalid metadata published response');
    const independent = await create({ conversation: 'none', input: [],
      instructions: 'Compute 6 * 7. Reply only 42.', tools: [],
      tool_choice: 'none', max_output_tokens: 32 });
    check(text(independent).trim() === '42', 'out-of-band actual quality');
    for (const [instructions, expected] of [
      ['Compute 9 * 8. Reply only 72.', '72'],
      ['Reply only with the word blue.', 'blue'],
    ]) {
      const counter = await create({ conversation: 'none', input: [],
        instructions, tools: [], tool_choice: 'none', max_output_tokens: 32 });
      check(text(counter).trim() === expected, 'empty-context counterexample');
    }
    const limited = await create({ conversation: 'none', input: [], tools: [],
      tool_choice: 'none', max_output_tokens: 1,
      instructions: 'Write the numbers from one through twenty in English.' }, 'incomplete');
    check(limited.status_details?.reason === 'max_output_tokens', 'explicit token exhaustion');
    await create({ conversation: 'none', input: [], tools: [], tool_choice: 'none',
      max_output_tokens: 256,
      instructions: 'Write a long numbered list of the integers from 1 to 200, one per line.' },
    'cancelled', true);
    await add(user('user_followup', 'What was the version in the project tool result? Reply only with the version.'));
    const followup = await create({ tools: [], tool_choice: 'none', max_output_tokens: 32 });
    check(text(followup).trim() === toolRecord.version, 'default history after independent cancellation');
    check(events.filter((event) => event.type === 'error').length === 1,
      'unexpected connection error');
  }
  rt.close();
  await until(() => closed, true);
  check(closeCode === 1000 && connections === 1, 'clean one-connection boundary');
  clearTimeout(guard);
  process.stdout.write(JSON.stringify({ status: 'PASS', mode: args.mode,
    transport: 'official-openai-wss', connections, close_code: closeCode,
    response_creates: creates, automatic_replay: false, tool_record: toolRecord,
    responses: results, events }));
} catch (error) {
  clearTimeout(guard); rt.socket.terminate();
  process.stdout.write(JSON.stringify({ status: 'FAIL', error: error.message,
    connections, close_code: closeCode, response_creates: creates,
    automatic_replay: false, responses: results, events }));
  process.exitCode = 1;
}
