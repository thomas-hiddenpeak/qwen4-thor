// Official SDK for Realtime and native trace HTTP; no model/tool retries.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { check, terminal, usage, text, streamAgrees } from './conversation_checks.mjs';
import { recoverTrace } from './tracing_poll.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part; check(Buffer.byteLength(input) <= 65536, 'actor input bound');
}
const args = JSON.parse(input);
check(['primary', 'light'].includes(args.mode), 'fixed tracing scenario');
const sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR);
for (const [name, version] of [['openai', '7.47.0'], ['ws', '8.21.0']])
  check(JSON.parse(fs.readFileSync(path.join(sdk, 'node_modules', name,
    'package.json'))).version === version, 'SDK identity');
const { default: OpenAI } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/index.mjs')));
const { OpenAIRealtimeWS } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/realtime/ws.mjs')));
const url = new URL(args.base_url);
check(url.protocol === 'https:' && url.hostname === '127.0.0.1' &&
  url.pathname === '/v1' && !url.search && !url.hash && !url.username &&
  !url.password && process.env.NODE_EXTRA_CA_CERTS === args.ca_file,
'verified TLS for SDK HTTP and WebSocket');
const clients = [args.api_key, args.other_api_key].map((key) =>
  new OpenAI({ baseURL: url.toString(), apiKey: key, maxRetries: 0, timeout: 10000 }));
const rt = new OpenAIRealtimeWS({ model: 'qwen3.8-flash-next', options: {
  ca: fs.readFileSync(args.ca_file), maxPayload: 4 << 20, perMessageDeflate: false,
  followRedirects: false, handshakeTimeout: 10000,
} }, clients[0]);
const events = [], responses = [], operations = [], tools = [], quality = [], signals = [];
let fatal = null, closed = false, closeCode = null, connections = 0;
let bytes = 0, sent = 0, httpCalls = 0, sessionId, traceId;
const journal = fs.openSync(args.journal_file, 'wx', 0o600);
const end = Date.now() + 300000;
const timer = setTimeout(() => { fatal = new Error('actor deadline'); rt.socket.terminate(); }, 300000);
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
rt.socket.on('open', () => { ++connections; });
rt.socket.on('close', (code) => { closed = true; closeCode = code; });
rt.on('error', (error) => {
  if (!error.error) { fatal = new Error('SDK transport: ' + (error.cause?.code || error.name)); rt.socket.terminate(); }
});
rt.on('event', (event) => {
  const raw = JSON.stringify(event);
  bytes += Buffer.byteLength(raw);
  if (bytes > (8 << 20) || events.length >= 8192 || raw.includes(args.mcp_token)) {
    fatal = new Error('event bound or credential leak'); rt.socket.terminate(); return;
  }
  fs.writeSync(journal, raw + '\n'); events.push(event);
});
async function until(predicate, allowClosed = false, limit = end) {
  for (;;) {
    if (fatal) throw fatal;
    if (predicate()) return;
    check(Date.now() < Math.min(end, limit) && (allowClosed || !closed), 'closed or deadline');
    await pause(5);
  }
}
function send(event) { check(++sent <= 32, 'event bound'); rt.send(event); }
async function http(label, route, method = 'get', expected = 200, other = false) {
  check(++httpCalls <= 80 && route.startsWith('/realtime/traces'), 'trace HTTP bound');
  let value;
  try {
    const { data, response } = await clients[Number(other)][method](route).withResponse();
    value = { status: response.status, body: data };
  } catch (error) {
    if (!Number.isInteger(error.status)) throw error;
    value = { status: error.status, body: { error: error.error } };
  }
  operations.push({ label, route, method, scope: other ? 'other' : 'owner', ...value });
  if (value.status !== expected)
    throw Object.assign(new Error(`HTTP ${label}: ${value.status}`), { status: value.status });
  return value.body;
}
async function signal(name) {
  check(['repair-storage', 'kill-trace-worker'].includes(name) && !signals.includes(name),
    'fixed one-shot owned fault');
  fs.writeFileSync(path.join(args.control_dir, name + '.request'), name, { flag: 'wx', mode: 0o600 });
  await until(() => fs.existsSync(path.join(args.control_dir, name + '.ack')), false, Date.now() + 15000);
  signals.push(name);
}
async function query(label, predicate) {
  // Storage recovery has a real 60-second rolling budget. Poll only reads,
  // retaining each 503; never replay a model request or tool execution.
  return recoverTrace((i) => http(`${label}-${i}`,
    '/realtime/traces?session_id=' + sessionId), predicate);
}
async function create(label, response) {
  check(responses.length < 3, 'three actual jobs per profile');
  const at = events.length;
  send({ type: 'response.create', event_id: label, response: {
    max_output_tokens: 256, output_modalities: ['text'], ...response } });
  await until(() => events.slice(at).some((e) => e.type === 'response.done' || e.type === 'error'));
  check(!events.slice(at).some((e) => e.type === 'error'), 'generation rejected');
  const done = events.slice(at).find((e) => e.type === 'response.done').response;
  responses.push(done); terminal(events, done.id, 'completed'); usage(done); streamAgrees(events, done);
  return done;
}
async function ordinary() {
  const id = 'user_trace_ordinary';
  send({ type: 'conversation.item.create', item: { type: 'message', id, role: 'user',
    content: [{ type: 'input_text', text: 'Compute 6 * 7. Reply only with the decimal number.' }] } });
  await until(() => events.some((e) => e.type === 'conversation.item.done' && e.item?.id === id));
  const response = await create('ordinary', { tools: [], tool_choice: 'none' });
  quality.push({ case: 'ordinary', answer: text(response).trim(),
    status: text(response).trim() === '42' ? 'PASS' : 'FAIL' });
}
async function project() {
  const at = events.length;
  send({ type: 'session.update', session: { type: 'realtime', tools: [{ type: 'mcp',
    server_label: 'project', server_url: args.mcp_url, authorization: args.mcp_token,
    allowed_tools: ['read_project_record'], require_approval: 'always' }] } });
  await until(() => events.slice(at).some((e) => e.type === 'mcp_list_tools.completed' || e.type === 'error'));
  check(!events.slice(at).some((e) => e.type === 'error'), 'MCP catalog update');
  const generated = await create('project-tool', {
    tool_choice: { type: 'mcp', server_label: 'project', name: 'read_project_record' },
    instructions: 'Call read_project_record with path="docs/MODEL.md", start_line=1, line_count=3. Read the actual project document. Do not answer before receiving the tool result.' });
  const calls = generated.output.filter((i) => i.type === 'mcp_call');
  check(calls.length === 1 && isDeepStrictEqual(JSON.parse(calls[0].arguments), args.arguments) &&
    !calls[0].output && !calls[0].error, 'actual generated project call');
  const call = calls[0];
  await until(() => events.some((e) => e.type === 'conversation.item.done' &&
    e.item?.type === 'mcp_approval_request'));
  const proposal = events.findLast((e) => e.type === 'conversation.item.done' &&
    e.item?.type === 'mcp_approval_request').item;
  check(!events.some((e) => e.type === 'response.mcp_call.in_progress'), 'dispatch before approval');
  send({ type: 'conversation.item.create', item: { type: 'mcp_approval_response',
    id: 'decision_' + call.id, approval_request_id: proposal.id, approve: true } });
  await until(() => events.some((e) => e.type === 'response.output_item.done' && e.item?.id === call.id));
  const actual = events.find((e) => e.type === 'response.output_item.done' && e.item?.id === call.id).item;
  check(actual.output && !actual.error, 'actual tool receipt'); tools.push(actual);
  const response = await create('project-answer', { tools: [], tool_choice: 'none',
    instructions: 'Use the actual tool result already in this conversation. Reply with its first-line document title and exact citation only. Do not invent a source.' });
  const answer = text(response).trim();
  quality.push({ case: 'document', answer, status:
    answer.includes(args.title.replace(/^#\s*/, '')) && answer.includes(args.citation) ? 'PASS' : 'FAIL' });
}
let status = 'FAIL', error, snapshot, finalSnapshot, retained;
try {
  await until(() => events.some((e) => e.type === 'session.created') &&
    events.some((e) => e.type === 'conversation.created'));
  sessionId = events.find((e) => e.type === 'session.created').session.id;
  check(typeof sessionId === 'string' && /^sess_[A-Za-z0-9_-]+$/.test(sessionId),
    'session.created must expose its native session ID');
  traceId = 'trace_' + crypto.createHash('sha256').update(sessionId).digest('hex');
  if (args.mode === 'light') {
    retained = await http('prior-engine-record', '/realtime/traces/' + args.retained_trace_id);
    check(retained.status === 'closed', 'closed trace survived actual engine restart');
  }
  send({ type: 'session.update', session: { type: 'realtime', tracing: {
    workflow_name: 'q4t-project-query', group_id: args.mode,
    metadata: { case: args.mode, opaque: 'caller-owned-private-metadata' } } } });
  await until(() => events.some((e) => e.type === 'session.updated' && e.session.tracing));
  if (args.mode === 'primary') {
    await ordinary();
    await http('storage-unavailable', '/realtime/traces?session_id=' + sessionId, 'get', 503);
    await signal('repair-storage');
  }
  await project();
  snapshot = await query('complete-lifecycles', (r) =>
    r.spans.filter((s) => s.kind === 'response' && s.status === 'completed').length === responses.length &&
    r.spans.some((s) => s.kind === 'mcp_call' && s.status === 'completed'));
  check(snapshot.id === traceId && snapshot.workflow_name === 'q4t-project-query', 'trace identity');
  const full = await http('retrieve', '/realtime/traces/' + traceId);
  check(full.session_id === sessionId && full.metadata.opaque === 'caller-owned-private-metadata', 'full private record');
  const page = await http('summary', '/realtime/traces?limit=10');
  check(page.data.some((r) => r.id === traceId) && page.data.every((r) =>
    r.summary === true && !('spans' in r) && !('metadata' in r)), 'bounded summary');
  await http('cross-scope-get', '/realtime/traces/' + traceId, 'get', 404, true);
  await http('cross-scope-delete', '/realtime/traces/' + traceId, 'delete', 404, true);
  const other = await http('cross-scope-list', '/realtime/traces?session_id=' + sessionId, 'get', 200, true);
  check(other.data.length === 0, 'foreign scope exposed trace');
  if (args.mode === 'light') {
    await signal('kill-trace-worker');
    // Only read recovery is polled; no model/tool or mutation is retried.
    for (let i = 0; i < 20; ++i) {
      try {
        await http('worker-recovered-' + i, '/realtime/traces/' + traceId);
        break;
      } catch (e) {
        if (operations.at(-1)?.status !== 503 || i === 19) throw e;
        await pause(100);
      }
    }
    const erased = await http('delete-active', '/realtime/traces/' + traceId, 'delete');
    check(erased.deleted === true && erased.id === traceId, 'native trace deletion');
    await ordinary();
    await http('no-resurrection', '/realtime/traces/' + traceId, 'get', 404);
  }
  rt.close(); await until(() => closed, true);
  check(connections === 1 && closeCode === 1000 && responses.length === 3, 'bounded clean session');
  if (args.mode === 'primary')
    finalSnapshot = await query('closed', (r) => r.status === 'closed');
  else await http('closed-still-deleted', '/realtime/traces/' + traceId, 'get', 404);
  status = quality.every((q) => q.status === 'PASS') ? 'PASS' : 'QUALITY_FAIL';
} catch (e) { error = e.message; rt.socket.terminate(); }
finally {
  clearTimeout(timer); fs.closeSync(journal);
  process.stdout.write(JSON.stringify({ status, error, mode: args.mode,
    transport: 'official-openai-wss-and-http', connections, close_code: closeCode,
    automatic_replay: false, session_id: sessionId, trace_id: traceId, signals,
    responses, tools, quality, operations, snapshot, final_snapshot: finalSnapshot, retained, events }));
  if (status !== 'PASS') process.exitCode = 1;
}
