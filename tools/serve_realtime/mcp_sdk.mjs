// Official SDK actor; model generation and MCP dispatch are both native.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { check, terminal, usage, text, streamAgrees } from './conversation_checks.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part;
  check(Buffer.byteLength(input) <= 65536, 'actor input bound');
}
const args = JSON.parse(input);
check(['direct-deny-import', 'approve-unknown'].includes(args.mode), 'fixed MCP scenario');
const sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR);
for (const [name, version] of [['openai', '7.47.0'], ['ws', '8.21.0']])
  check(JSON.parse(fs.readFileSync(path.join(sdk, 'node_modules', name,
    'package.json'))).version === version, 'SDK identity');
const { default: OpenAI } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/index.mjs')));
const { OpenAIRealtimeWS } = await import(pathToFileURL(path.join(sdk, 'node_modules/openai/realtime/ws.mjs')));
const url = new URL(args.base_url);
check(url.protocol === 'https:' && url.hostname === '127.0.0.1' &&
  url.pathname === '/v1' && !url.search && !url.hash && !url.username && !url.password,
'explicit private verified WSS');
const client = new OpenAI({ baseURL: url.toString(), apiKey: args.api_key, maxRetries: 0 });
const rt = new OpenAIRealtimeWS({ model: 'qwen3.8-flash-next', options: {
  ca: fs.readFileSync(args.ca_file), maxPayload: 4 << 20, perMessageDeflate: false,
  followRedirects: false, handshakeTimeout: 10000,
} }, client);
const events = [], responses = [], tools = [], quality = [];
let fatal = null, closed = false, closeCode = null, connections = 0, sent = 0, bytes = 0;
const journal = fs.openSync(args.journal_file, 'wx', 0o600);
const end = Date.now() + 300000;
const timer = setTimeout(() => { fatal = new Error('actor deadline'); rt.socket.terminate(); }, 300000);
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
  fs.writeSync(journal, raw + '\n');
  events.push(event);
});
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(predicate, closing = false) {
  for (;;) {
    if (fatal) throw fatal;
    const error = events.find((e) => e.type === 'error');
    if (error) throw new Error('native protocol: ' + error.error.code);
    if (predicate()) return;
    check(Date.now() < end && (closing || !closed), 'closed or deadline');
    await pause(2);
  }
}
function send(event) { check(++sent <= 32, 'client event bound'); rt.send(event); }
const definition = (approval, broken = false) => ({ type: 'mcp',
  server_label: broken ? 'broken' : 'project', server_url: args.mcp_url + (broken ? '/unavailable' : ''),
  authorization: args.mcp_token, allowed_tools: ['read_project_record'], require_approval: approval });
const choice = { type: 'mcp', server_label: 'project', name: 'read_project_record' };
const request = 'Call read_project_record with path="docs/MODEL.md", start_line=1, line_count=3. Read the actual project document. Do not answer before receiving the tool result.';
async function create(response) {
  check(responses.length < 4, 'four generation attempts per connection');
  const at = events.length;
  send({ type: 'response.create', response: { max_output_tokens: 256,
    output_modalities: ['text'], ...response } });
  await until(() => events.slice(at).some((e) => e.type === 'response.done'));
  const done = events.slice(at).find((e) => e.type === 'response.done').response;
  // Retain the real terminal even when a later protocol or quality check fails.
  responses.push(done);
  terminal(events, done.id, 'completed'); usage(done); streamAgrees(events, done);
  return done;
}
function generated(response) {
  const calls = response.output.filter((item) => item.type === 'mcp_call');
  check(calls.length === 1, 'one actual generated MCP call');
  const call = calls[0];
  check(call.name === 'read_project_record' && call.server_label === 'project' &&
    isDeepStrictEqual(JSON.parse(call.arguments), args.arguments), 'real MCP arguments');
  check(!call.output && !call.error, 'generation terminal cannot claim tool outcome');
  const delta = events.filter((e) => e.type === 'response.mcp_call_arguments.delta' &&
    e.response_id === response.id && e.item_id === call.id).map((e) => e.delta).join('');
  check(delta === call.arguments, 'MCP argument stream differs');
  return call;
}
async function result(call, failure = false) {
  await until(() => events.some((e) => e.type === 'response.output_item.done' &&
    e.item?.id === call.id && (failure ? !!e.item.error : typeof e.item.output === 'string')));
  const rows = events.filter((e) => e.type === 'response.output_item.done' && e.item?.id === call.id);
  check(rows.length === 1, 'one actual tool terminal');
  const item = rows[0].item;
  check(failure ? !!item.error && !item.output : !item.error && !!item.output, 'tool outcome');
  tools.push(item);
  return item;
}
async function approve(call, accepted) {
  await until(() => events.some((e) => e.type === 'conversation.item.done' &&
    e.item?.type === 'mcp_approval_request' && e.item.arguments === call.arguments));
  const proposal = events.findLast((e) => e.type === 'conversation.item.done' &&
    e.item?.type === 'mcp_approval_request').item;
  check(!events.some((e) => e.type === 'response.mcp_call.in_progress' && e.item_id === call.id),
    'dispatch before approval');
  send({ type: 'conversation.item.create', item: { type: 'mcp_approval_response',
    id: 'decision_' + call.id, approval_request_id: proposal.id, approve: accepted } });
  return result(call, !accepted);
}
async function follow() {
  const answer = await create({ tools: [], tool_choice: 'none',
    instructions: 'Use the actual tool result already in this conversation. Reply with its first-line document title and exact citation only. Do not invent a source.' });
  const value = text(answer).trim();
  quality.push({ case: 'document', answer: value, status:
    value.includes(args.title.replace(/^#\s*/, '')) && value.includes(args.citation) ? 'PASS' : 'FAIL' });
}
let status = 'FAIL', error;
try {
  await until(() => events.some((e) => e.type === 'session.created') &&
    events.some((e) => e.type === 'conversation.created'));
  if (args.mode === 'direct-deny-import') {
    // No prior session tools: this response must import and freeze its own catalog.
    const first = generated(await create({ tools: [definition('never')], tool_choice: choice,
      instructions: request }));
    await result(first); await follow();
    const denied = generated(await create({ tools: [definition('always')], tool_choice: choice,
      instructions: request }));
    await approve(denied, false);
    send({ type: 'session.update', session: { type: 'realtime', tools: [definition('never', true)] } });
    await until(() => events.some((e) => e.type === 'mcp_list_tools.failed'));
  } else {
    send({ type: 'session.update', session: { type: 'realtime', tools: [definition('always')] } });
    await until(() => events.some((e) => e.type === 'mcp_list_tools.completed'));
    const first = generated(await create({ tool_choice: choice, instructions: request }));
    await approve(first, true); await follow();
    const lost = generated(await create({ tools: [definition('never')], tool_choice: choice,
      instructions: request }));
    const failed = await result(lost, true);
    check(JSON.stringify(failed.error).includes('mcp_result_unknown'), 'unknown receipt failure must be preserved');
  }
  const ordinary = await create({ tools: [], tool_choice: 'none', max_output_tokens: 32,
    instructions: 'Compute 6 * 7. Reply only with the decimal number.' });
  quality.push({ case: 'ordinary_after_fault', answer: text(ordinary).trim(),
    status: text(ordinary).trim() === '42' ? 'PASS' : 'FAIL' });
  rt.close(); await until(() => closed, true);
  check(connections === 1 && closeCode === 1000 && responses.length === 4,
    'one clean connection and four generations');
  status = quality.every((row) => row.status === 'PASS') ? 'PASS' : 'QUALITY_FAIL';
} catch (e) { error = e.message; rt.socket.terminate(); }
finally {
  clearTimeout(timer); fs.closeSync(journal);
  process.stdout.write(JSON.stringify({ status, error, mode: args.mode,
    transport: 'official-openai-wss', connections, close_code: closeCode,
    automatic_replay: false, responses, tools, quality, events }));
  if (status !== 'PASS') process.exitCode = 1;
}
