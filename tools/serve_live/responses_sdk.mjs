// A typed project query exercises backend calls, real tool results and voice.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {check} from '../serve_realtime/conversation_checks.mjs';
import {openJournal} from '../serve_realtime/bounded_journal.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  check(Buffer.byteLength(input) <= 65536, 'bounded arguments');
}
const args = JSON.parse(input);
const sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR, 'node_modules/openai');
check(JSON.parse(fs.readFileSync(path.join(sdk, 'package.json'))).version === '7.47.0', 'SDK identity');
const {default: OpenAI} = await import(pathToFileURL(path.join(sdk, 'index.mjs')));
const {LiveWS} = await import(pathToFileURL(path.join(sdk, 'resources/live/ws.mjs')));
const addr = new URL(args.base_url);
check(addr.protocol === 'https:' && addr.hostname === '127.0.0.1' && addr.pathname === '/v1' &&
  !addr.username && !addr.password && !addr.search && !addr.hash, 'local TLS test endpoint');
const journal = openJournal(args.journal_file, 8 << 20);
const rt = new LiveWS(new OpenAI({baseURL: addr.toString(), apiKey: args.api_key, maxRetries: 0}), {
  ca: fs.readFileSync(args.ca_file), maxPayload: 4 << 20, perMessageDeflate: false,
  followRedirects: false, handshakeTimeout: 10000, reconnect: null,
});
let opens = 0, closed = false, closeCode, failure, sent = 0, bytes = 0;
const events = [], result = {status: 'FAIL'};
rt.socket.on('open', () => {opens++;});
rt.on('close', (code, reason, unsent) => {
  closed = true; closeCode = code;
  if (unsent.length) failure = new Error('unsent commands');
});
rt.on('error', e => {failure = new Error(e.error?.error?.code || e.message || 'Live SDK error');});
rt.on('event', event => {
  try {
    bytes += Buffer.byteLength(JSON.stringify(event));
    check(bytes <= 6 << 20 && events.length < 4096, 'finite SDK event stream');
    journal.append('server', event);
    events.push(event);
  } catch (e) {failure = e; rt.socket.terminate();}
});
const deadline = Date.now() + 210000;
const timer = setTimeout(() => {failure = new Error('SDK deadline');rt.socket.terminate();}, 210000);
async function until(predicate) {
  for (;;) {
    if (failure) throw failure;
    const found = predicate();
    if (found) return found;
    check(!closed && Date.now() < deadline, 'premature close or deadline');
    await new Promise(resolve => setTimeout(resolve, 3));
  }
}
function send(event) {
  check(++sent <= 10, 'bounded commands');
  journal.append('client', event); rt.send(event);
}
const eventOf = type => until(() => events.find(e => e.type === type));
const model = 'qwen3.8-flash-next';
try {
  await until(() => opens === 1);
  send({type:'session.start', event_id:'start', session:{model,
    instructions:'这是项目资料查询服务。收到后端给出的版本号后，请只说这个版本号一次，不要解释或再次委派。',
    delegation:{type:'responses',acknowledgement_delay_ms:600000,responses:{model,
      instructions:'查阅项目版本时调用 read_project_version。拿到工具结果后，只回答结果中的版本号，例如 rev123，不要解释。',
      tools:[{type:'function',name:'read_project_version',description:'读取项目状态文档中的当前默认版本',
        parameters:{type:'object',properties:{},required:[],additionalProperties:false},strict:true}],
      tool_choice:{type:'function',name:'read_project_version'},reasoning:{effort:'none'},max_output_tokens:96}}}});
  await eventOf('session.started');
  send({type:'response.item.create',event_id:'question',item:{role:'user',content:'请查阅项目资料并返回当前默认版本号。'}});
  send({type:'response.create',event_id:'lookup'});
  const outer = await until(() => events.find(e => e.type==='response.event' && e.event.type==='response.output_item.done' &&
    e.event.item.type==='function_call'));
  const call=outer.event.item;
  check(call.name==='read_project_version' && call.call_id && call.status==='completed' &&
    JSON.stringify(JSON.parse(call.arguments))==='{}', 'owned finite lookup');
  await until(() => events.find(e => e.delegation_id===outer.delegation_id && e.event?.type==='response.completed'));
  const record = fs.readFileSync(args.record_file);
  const digest = crypto.createHash('sha256').update(record).digest('hex');
  const version = record.toString().match(/默认\s*rev(\d+)/);
  check(record.length < 1<<20 && digest===args.record_sha256 && version, 'actual project record');
  result.record = {path:'docs/STATUS.md', sha256:digest, version:'rev'+version[1]};
  send({type:'session.update',event_id:'answer_only',session:{delegation:{type:'responses',responses:{tool_choice:'none'}}}});
  await eventOf('session.updated');
  send({type:'response.item.create',event_id:'result',item:{type:'function_call_output',call_id:call.call_id,
    output:JSON.stringify(result.record)}});
  send({type:'response.create',event_id:'answer'});
  await until(() => events.find(e => e.client_event_id==='answer' && e.event?.type==='response.completed'));
  await eventOf('session.output_transcript.delta');
  send({type:'session.close',event_id:'finish'});
  await eventOf('session.closed');
  await until(() => closed);
  check(closeCode===1000 && opens===1, 'normal drain without reconnect');
  result.status='PASS';
} catch (e) {result.error = String(e.message);process.exitCode=2;}
finally {
  clearTimeout(timer);
  if (!closed) rt.socket.terminate();
  journal.close(); Object.assign(result,{opens,close_code:closeCode,sent});
  process.stdout.write(JSON.stringify(result)+'\n');
}
