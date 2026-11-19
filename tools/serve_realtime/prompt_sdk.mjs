// Bounded official Realtime client; native registry/file operations use TLS.
import fs from 'node:fs';
import https from 'node:https';
import path from 'node:path';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { check, terminal, usage, text, functionCall, streamAgrees } from './conversation_checks.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part; check(Buffer.byteLength(input) <= 65536, 'argument bound');
}
const args = JSON.parse(input);
const sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR);
for (const [name, version] of [['openai', '7.47.0'], ['ws', '8.21.0']])
  check(JSON.parse(fs.readFileSync(path.join(sdk, 'node_modules', name,
    'package.json'))).version === version, 'fixed SDK identity');
const { default: OpenAI } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/index.mjs')));
const { OpenAIRealtimeWS } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/realtime/ws.mjs')));
const url = new URL(args.base_url);
check(url.protocol === 'https:' && url.hostname === '127.0.0.1' &&
  url.pathname === '/v1' && !url.username && !url.password &&
  !url.search && !url.hash && args.ca_file, 'verified loopback TLS required');
check(['alpha', 'beta', 'remaining'].includes(args.mode), 'bounded client mode');
const ca = fs.readFileSync(args.ca_file);
const client = new OpenAI({ baseURL: url.toString(), apiKey: args.api_key, maxRetries: 0 });
let rt, fatal = null, closed = false, closeCode = null, connections = 0;
let bytes = 0, sent = 0, creates = 0, httpCalls = 0;
const events = [], responses = [], operations = [], checks = [], owned = {};
const deadline = Date.now() + 360000;
const guard = setTimeout(() => {
  fatal = new Error('absolute SDK deadline'); rt?.socket.terminate();
}, 360000);
const digest = (value) => crypto.createHash('sha256').update(value).digest('hex');
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(predicate, allowClosed = false) {
  for (;;) {
    if (fatal) throw fatal;
    if (predicate()) return;
    check(Date.now() < deadline && (allowClosed || !closed), 'deadline or closed');
    await pause(2);
  }
}
async function http(label, method, route, body, status = 200, contentType = 'application/json') {
  check(++httpCalls <= 16 && route.startsWith('/v1/'), 'HTTP operation bound');
  const data = body === undefined ? Buffer.alloc(0) : Buffer.isBuffer(body) ?
    body : Buffer.from(JSON.stringify(body));
  check(data.length <= (1 << 20), 'HTTP body bound');
  const value = await new Promise((resolve, reject) => {
    const request = https.request(new URL(route, url), { method, ca, timeout: 15000,
      headers: { Authorization: `Bearer ${args.api_key}`, 'Content-Type': contentType,
        'Content-Length': data.length } }, (response) => {
      const chunks = []; let length = 0;
      response.on('data', (chunk) => {
        length += chunk.length;
        if (length > (1 << 20)) { response.destroy(new Error('HTTP response bound')); return; }
        chunks.push(chunk);
      });
      response.on('error', reject);
      response.on('end', () => {
        try { resolve({ status: response.statusCode, body: JSON.parse(Buffer.concat(chunks)) }); }
        catch (error) { reject(error); }
      });
    });
    request.on('timeout', () => request.destroy(new Error('HTTP deadline')));
    request.on('error', reject); request.end(data);
  });
  operations.push({ label, method, route, ...value });
  check(value.status === status, `HTTP ${label}: ${value.status}`);
  return value.body;
}
// Shared exact definitions are validated by the actual CPU Prompt registry
// before any model launch; Realtime and Responses have different effort enums.
const definitions = JSON.parse(fs.readFileSync(new URL('./prompt_definitions.json', import.meta.url)));
const user = (id, value) => ({ type: 'message', id, role: 'user',
  content: [{ type: 'input_text', text: value }] });
const register = (label, definition) => http(label, 'POST', '/v1/prompts', { definition });
const fileDefinition = definitions.file;
function send(value) { check(++sent <= 64, 'event count bound'); rt.send(value); }
async function update(session, label, expectedError = null) {
  const at = events.length;
  send({ type: 'session.update', event_id: label, session: { type: 'realtime', ...session } });
  await until(() => events.slice(at).some((e) => e.type === 'session.updated' || e.type === 'error'));
  const rows = events.slice(at);
  const error = rows.find((e) => e.type === 'error');
  if (expectedError) {
    check(error?.error?.event_id === label && error.error.code === expectedError &&
      !rows.some((e) => e.type === 'session.updated'), 'failed update committed');
    checks.push(label); return null;
  }
  check(!error, `update ${label}: ${error?.error?.code}`);
  return rows.find((e) => e.type === 'session.updated').session;
}
async function create(label, response, expected = 'completed', cancel = false) {
  check(++creates <= ({ alpha: 10, beta: 3, remaining: 2 }[args.mode]), 'response bound');
  const at = events.length;
  send({ type: 'response.create', event_id: label,
    response: { ...response, metadata: { acceptance_case: label } } });
  let id;
  await until(() => {
    const error = events.slice(at).find((e) => e.type === 'error');
    if (error) throw new Error(`create ${label}: ${error.error.code}`);
    id = events.slice(at).find((e) => e.type === 'response.created')?.response.id;
    return Boolean(id);
  });
  if (cancel) {
    await until(() => {
      check(!events.slice(at).some((e) => e.type === 'response.done'), 'completed before cancellation');
      return events.slice(at).some((e) => e.type === 'response.output_text.delta' &&
        e.response_id === id && e.delta);
    });
    send({ type: 'response.cancel', response_id: id });
  }
  await until(() => events.slice(at).some((e) => e.type === 'response.done' && e.response?.id === id));
  const value = terminal(events, id, expected);
  responses.push(value);
  if (expected !== 'failed') {
    usage(value);
    if (!cancel) streamAgrees(events, value);
  } else {
    check(value.usage && value.usage.total_tokens === 0 &&
      value.usage.input_tokens === 0 && value.usage.output_tokens === 0,
    'rejected unstarted primary must report zero actual work');
  }
  checks.push(label); return value;
}
async function add(item) {
  const at = events.length;
  send({ type: 'conversation.item.create', item });
  await until(() => {
    const error = events.slice(at).find((e) => e.type === 'error');
    if (error) throw new Error('item rejected: ' + error.error.code);
    return events.slice(at).some((e) => e.type === 'conversation.item.done' && e.item?.id === item.id);
  });
}
const exact = (response, expected, label) => check(text(response).trim() === expected,
  `${label} quality: ${JSON.stringify(text(response))}`);
const independent = (prompt, query) => ({ conversation: 'none', prompt,
  input: query ? [user(undefined, query)] : [] });
let toolRecord = null, sourceRecord = null;
try {
  rt = new OpenAIRealtimeWS({ model: 'qwen3.8-flash-next', options: {
    ca, maxPayload: 4 << 20, perMessageDeflate: false,
    followRedirects: false, handshakeTimeout: 10000,
  } }, client);
  rt.socket.on('open', () => { ++connections; });
  rt.socket.on('close', (code) => { closed = true; closeCode = code; });
  rt.on('error', (error) => {
    if (!error.error) { fatal = new Error('SDK transport: ' + (error.cause?.code || error.name)); rt.socket.terminate(); }
  });
  rt.on('event', (value) => {
    bytes += Buffer.byteLength(JSON.stringify(value));
    if (bytes > (4 << 20) || events.length >= 4096) {
      fatal = new Error('event storage bound'); rt.socket.terminate(); return;
    }
    events.push(value);
  });
  await until(() => events.some((e) => e.type === 'session.created') &&
    events.some((e) => e.type === 'conversation.created'));
  const csv = fs.readFileSync(args.csv_file);
  check(digest(csv) === args.csv_sha256 && csv.length <= 4096, 'actual CSV source');
  const inlineCsv = { type: 'input_file', filename: 'acceptance-counts.csv', file_data: csv.toString('base64') };
  if (args.mode === 'remaining') {
    const pinned = await update({ prompt: { id: args.foreign_prompt, version: '1',
      variables: { left: '13', right: '29' } } }, 'pin-retained-first');
    check(pinned.prompt.id === args.foreign_prompt && pinned.prompt.version === '1',
      'retained owned template missing');
    await create('cancel-after-delta', { ...independent(null), tools: [], tool_choice: 'none', max_output_tokens: 256,
      instructions: 'Write a long numbered list of integers from 1 to 200, one per line.' }, 'cancelled', true);
    exact(await create('new-request-after-cancel', { ...independent(null, 'Compute 8 * 7. Reply only with its integer value.'),
      tools: [], tool_choice: 'none', max_output_tokens: 32 }), '56', 'new request after cancellation');
    const retained = await update({}, 'retained-after-cancel');
    check(retained.prompt.id === args.foreign_prompt && retained.prompt.version === '1',
      'cancellation changed retained template');
  } else if (args.mode === 'beta') {
    await update({ prompt: { id: args.foreign_prompt, variables: { left: '13', right: '29' } } },
      'foreign-prompt-denied', 'object_not_found');
    const files = await register('register-beta-file', fileDefinition);
    const foreign = await create('foreign-file-denied', independent({ id: files.id,
      variables: { source: { type: 'input_file', file_id: args.foreign_file } } },
    'Return the version from the supplied JSON.'), 'failed');
    check(foreign.status_details.error.code === 'object_not_found', 'foreign file isolation');
    const own = await create('beta-csv-one-rpm', independent({ id: files.id,
      variables: { source: inlineCsv } }, 'What is the sum of the tests column? Reply only with the integer.'));
    exact(own, String(args.csv_sum), 'beta spreadsheet');
    const denied = await create('beta-rpm-denied', { ...independent(null, 'Compute 6 * 7. Reply only with the integer.'),
      max_output_tokens: 32 }, 'failed');
    check(denied.status_details.error.code === 'client_rpm_exhausted', 'actual one-RPM refusal');
  } else {
    const record = fs.readFileSync(args.record_file);
    check(digest(record) === args.record_sha256 && record.length <= (1 << 20), 'actual project source');
    const version = record.toString().match(/默认\s*rev(\d+)/)?.[1];
    check(`rev${version}` === args.expected_version, 'actual release extraction');
    sourceRecord = { version: `rev${version}`, source: 'docs/STATUS.md', source_sha256: digest(record) };
    const first = definitions.first;
    const prompt = await register('register-arithmetic', first); owned.prompt = prompt.id;
    const reference = { id: prompt.id, variables: { left: '13', right: '29' } };
    const pinned = await update({ prompt: reference }, 'pin-first');
    check(pinned.prompt.version === '1' && pinned.max_output_tokens === 128, 'template defaults masked');
    const second = definitions.second;
    const appended = await http('append-second', 'POST', `/v1/prompts/${prompt.id}/versions`,
      { definition: second, expected_revision: 1 });
    check(appended.default_version === '2', 'append actual version');
    const unchanged = await update({ output_modalities: ['text'] }, 'ordinary-update');
    check(unchanged.prompt.version === '1' && unchanged.max_output_tokens === 128, 'ordinary update rebound version');
    exact(await create('pinned-first', { conversation: 'none', input: [] }), '42', 'pinned template');
    exact(await create('one-response-second', independent({ ...reference, version: '2' })), '377', 'one response override');
    exact(await create('one-response-null', { ...independent(null, 'Compute 9 * 8. Reply only with the integer.'),
      max_output_tokens: 32 }), '72', 'null clears hidden prefix');
    const after = await update({}, 'session-still-first');
    check(after.prompt.version === '1', 'per-response changed session');
    const boundary = 'q4tPrompt' + crypto.randomBytes(12).toString('hex');
    const multipart = Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nuser_data\r\n` +
      `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="release.json"\r\nContent-Type: application/json\r\n\r\n` +
      JSON.stringify(sourceRecord) + `\r\n--${boundary}--\r\n`);
    const file = await http('upload-project-record', 'POST', '/v1/files', multipart, 200,
      `multipart/form-data; boundary=${boundary}`); owned.file = file.id;
    const files = await register('register-file', fileDefinition);
    exact(await create('actual-project-file', independent({ id: files.id,
      variables: { source: { type: 'input_file', file_id: file.id } } },
    'Return only the version field in the supplied JSON.')), sourceRecord.version, 'actual uploaded project record');
    const picture = fs.readFileSync(args.image_file);
    check(digest(picture) === args.image_sha256, 'fixed actual image');
    const imagePrompt = await register('register-image', definitions.image);
    const imageReply = await create('actual-image', independent({ id: imagePrompt.id,
      variables: { picture: { type: 'input_image', detail: 'low',
        image_url: 'data:image/png;base64,' + picture.toString('base64') } } },
    'What color is the left half of the image? Reply with one lowercase English color.'));
    exact(imageReply, 'red', 'actual image');
    exact(await create('actual-csv', independent({ id: files.id, variables: { source: inlineCsv } },
      'What is the sum of the tests column? Reply only with the integer.')), String(args.csv_sum), 'actual spreadsheet');
    const tool = await register('register-tool', definitions.tool);
    await update({ prompt: { id: tool.id } }, 'select-tool-template');
    await add(user('lookup_user', 'Call lookup_project_record with record_key release. After receiving its result, return only its version.'));
    const call = functionCall(await create('actual-tool-call', { tool_choice: { type: 'function', name: 'lookup_project_record' } }));
    // The source is re-read only after the actual call, never prefilled as an answer.
    check(digest(fs.readFileSync(args.record_file)) === args.record_sha256, 'tool source changed');
    toolRecord = { record_key: 'release', ...sourceRecord };
    await add({ type: 'function_call_output', id: 'actual_tool_output', call_id: call.call_id, output: JSON.stringify(toolRecord) });
    await http('delete-pinned-tool-template', 'DELETE', `/v1/prompts/${tool.id}`);
    exact(await create('pinned-tool-after-delete', { tools: [], tool_choice: 'none',
      instructions: 'Return only the exact version value from the actual tool result.' }), sourceRecord.version, 'actual tool round trip');
    await create('cancel-after-delta', { ...independent(null), tools: [], tool_choice: 'none', max_output_tokens: 256,
      instructions: 'Write a long numbered list of integers from 1 to 200, one per line.' }, 'cancelled', true);
    exact(await create('new-request-after-cancel', { ...independent(null, 'Compute 8 * 7. Reply only with its integer value.'),
      tools: [], tool_choice: 'none', max_output_tokens: 32 }), '56', 'new request after cancellation');
  }
  rt.close(); await until(() => closed, true);
  check(connections === 1 && closeCode === 1000, 'one clean connection');
  clearTimeout(guard);
  process.stdout.write(JSON.stringify({ status: 'PASS', mode: args.mode, transport: 'official-openai-wss',
    automatic_replay: false, connections, close_code: closeCode, response_creates: creates,
    http_calls: httpCalls, owned, source_record: sourceRecord, tool_record: toolRecord,
    checks, responses, events, operations }));
} catch (error) {
  clearTimeout(guard); rt?.socket.terminate();
  process.stdout.write(JSON.stringify({ status: 'FAIL', error: error.message, mode: args.mode,
    automatic_replay: false, connections, close_code: closeCode, response_creates: creates,
    http_calls: httpCalls, owned, source_record: sourceRecord, tool_record: toolRecord,
    checks, responses, events, operations }));
  process.exitCode = 1;
}
