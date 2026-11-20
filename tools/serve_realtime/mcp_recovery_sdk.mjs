// Real MCP work across native owned faults; never reconnect or replay.
import fs from 'node:fs';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { pathToFileURL } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { openJournal } from './bounded_journal.mjs';
import { publicEvent } from './mcp_recovery_journal.mjs';
import { check, terminal, usage, text, streamAgrees } from './conversation_checks.mjs';
let input = '';
for await (const part of process.stdin) {
  input += part;
  check(Buffer.byteLength(input) <= 65536, 'input bound');
}
const args = JSON.parse(input);
check(['complete', 'hold_approval', 'hold_complete', 'policy', 'hold_policy'].includes(args.mode),
  'fixed recovery mode');
const policy = ['policy', 'hold_policy'].includes(args.mode);
const jobs = policy || args.mode === 'hold_approval' ? 1 : 2;
const journal = openJournal(args.journal_file, 8 << 20);
process.once('exit', () => journal.close());
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
let bytes = 0, sent = 0, creates = 0, approval = null;
const events = [], responses = [], tools = [], restored = [], transportErrors = [];
const deadline = Date.now() + 180000;
const timer = setTimeout(() => { fatal = new Error('absolute deadline'); rt.socket.terminate(); }, 180000);
const secrets = [args.api_key, args.mcp_token];
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
  try {
    check(bytes <= (4 << 20) && events.length < 4096, 'event bound');
    const clean = publicEvent(event, 'server', secrets);
    journal.append('server', clean); events.push(clean);
    if (event.type === 'error') throw new Error('native protocol: ' + event.error?.code);
  } catch (error) { fatal = error; rt.socket.terminate(); }
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
  check(++sent <= 32, 'client event bound');
  journal.append('client', publicEvent(event, 'client', secrets)); rt.send(event);
}
async function waitEvent(at, type, predicate = () => true) {
  let found;
  await until(() => { found = events.slice(at).find((e) => e.type === type && predicate(e)); return found; });
  return found;
}
async function action(event, type, predicate = () => true) {
  const at = events.length; send(event); return waitEvent(at, type, predicate);
}
async function user(id, prompt) {
  const item = { type: 'message', id, role: 'user', content: [{ type: 'input_text', text: prompt }] };
  await action({ type: 'conversation.item.create', item }, 'conversation.item.done', (e) => e.item?.id === id);
}
async function create(response) {
  check(++creates <= jobs, 'generation bound');
  const at = events.length;
  send({ type: 'response.create', response: { max_output_tokens: 256,
    output_modalities: ['text'], conversation: 'auto', ...response } });
  const event = await waitEvent(at, 'response.done');
  const row = event.response; responses.push(row);
  terminal(events, row.id, 'completed'); usage(row); streamAgrees(events, row);
  return row;
}
const readPrompt = 'Call read_project_record with path="docs/MODEL.md", start_line=1, line_count=3. Read the actual project document. Do not answer before receiving the tool result.';
const followPrompt = 'Use the actual tool result already in this conversation. Reply with its first-line document title and exact citation only. Do not invent a source.';
function result(status, error) {
  return { phase: 'result', status, ...(error ? { error } : {}), mode: args.mode,
    transport, connections, close_code: closeCode, automatic_replay: false,
    response_creates: creates, approval, responses, tools, restored,
    sent_events: sent, event_bytes: bytes, events, transport_errors: transportErrors,
    client_journal_authorization: 'redacted' };
}
try {
  await waitEvent(0, 'session.created'); await waitEvent(0, 'conversation.created');
  check(Array.isArray(args.history) && args.history.length <= 1 &&
    (!policy || args.history.length === 0), 'bounded prior successful history');
  for (const item of args.history) {
    check(item.type === 'mcp_call' && item.output && !item.error, 'only completed owned history');
    const done = await action({ type: 'conversation.item.create', item },
      'conversation.item.done', (e) => e.item?.id === item.id);
    check(isDeepStrictEqual(done.item, item), 'authenticated history changed');
    restored.push(done.item);
  }
  const definition = { type: 'mcp', server_label: 'project', server_url: args.mcp_url,
    authorization: args.mcp_token, allowed_tools: ['read_project_record'], require_approval: 'always' };
  const at = events.length;
  await action({ type: 'session.update', session: { type: 'realtime', tools: [definition] } },
    'session.updated');
  await waitEvent(at, policy ? 'mcp_list_tools.failed' : 'mcp_list_tools.completed');
  if (policy) {
    await user('user_mcp_after_fault', 'Compute 6 * 7. Reply only with the decimal number.');
    await create({ tools: [], tool_choice: 'none' });
  } else {
    await user('user_mcp_read', readPrompt);
    const row = await create({ tool_choice: { type: 'mcp', server_label: 'project', name: 'read_project_record' } });
    const calls = row.output.filter((i) => i.type === 'mcp_call');
    check(calls.length === 1, 'one actual generated MCP call');
    const call = calls[0];
    check(!call.output && !call.error && call.name === 'read_project_record' &&
      call.server_label === 'project' && isDeepStrictEqual(JSON.parse(call.arguments), args.arguments),
    'actual generated arguments');
    const proposal = await waitEvent(at, 'conversation.item.done',
      (e) => e.item?.type === 'mcp_approval_request' && e.item.arguments === call.arguments);
    approval = proposal.item;
    check(!events.some((e) => e.type === 'response.mcp_call.in_progress'), 'dispatch before approval');
    if (args.mode !== 'hold_approval') {
      await action({ type: 'conversation.item.create', item: { type: 'mcp_approval_response',
        id: 'decision_' + call.id, approval_request_id: approval.id, approve: true } },
      'conversation.item.done', (e) => e.item?.type === 'mcp_approval_response');
      const done = await waitEvent(at, 'response.output_item.done', (e) => e.item?.id === call.id && e.item.output);
      tools.push(done.item);
      check(!done.item.error, 'actual tool outcome failed');
      await waitEvent(at, 'conversation.item.done', (e) => e.item?.id === call.id && e.item.output);
      await user('user_mcp_follow', followPrompt);
      await create({ tools: [], tool_choice: 'none' });
    }
  }
  if (['complete', 'policy'].includes(args.mode)) {
    rt.close(); await until(() => closed, true);
    check(closeCode === 1000, 'normal close missing');
  } else {
    armed = true;
    process.stdout.write(JSON.stringify({ phase: 'ready', mode: args.mode, transport,
      connections, response_creates: creates, approval, completed_ids: responses.map((r) => r.id),
      tool_ids: tools.map((t) => t.id) }) + '\n');
    await until(() => closed, true);
    check([1001, 1006, 1011, 1012].includes(closeCode), 'fault did not close owned connection');
  }
  check(connections === 1 && creates === jobs && responses.length === jobs, 'missing work or automatic reconnect');
  if (args.mode === 'hold_approval')
    check(!events.some((e) => e.type === 'response.mcp_call.in_progress'), 'unapproved call dispatched');
  process.stdout.write(JSON.stringify(result('PASS')) + '\n');
} catch (error) {
  rt.socket.terminate(); process.exitCode = 1;
  process.stdout.write(JSON.stringify(result('FAIL', error.message)) + '\n');
} finally { clearTimeout(timer); }
