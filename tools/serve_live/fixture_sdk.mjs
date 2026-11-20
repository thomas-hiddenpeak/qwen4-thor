// Bounded fixture/playback/resume SDK actor. Native model/tool events only; no replay/reconnect.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { check } from '../serve_realtime/conversation_checks.mjs';
import { clientHandoff } from './client_wait.mjs';
import { openJournal } from '../serve_realtime/bounded_journal.mjs';
let input = '';
for await (const part of process.stdin) {
  input += part; check(Buffer.byteLength(input) <= 65536, 'argument bound');
}
const args = JSON.parse(input), sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR);
for (const [name, version] of [['openai','7.47.0'], ['ws','8.21.0']])
  check(JSON.parse(fs.readFileSync(path.join(sdk,'node_modules',name,'package.json'))).version === version,
    'fixed actual SDK version');
const {default: OpenAI} = await import(pathToFileURL(path.join(sdk,'node_modules/openai/index.mjs')));
const {LiveWS} = await import(pathToFileURL(path.join(sdk,'node_modules/openai/resources/live/ws.mjs')));
const url = new URL(args.base_url);
check(url.protocol === 'https:' && url.hostname === '127.0.0.1' && url.pathname === '/v1'
  && !url.search && !url.hash && !url.username && !url.password, 'local verified TLS');
const digest = (raw) => crypto.createHash('sha256').update(raw).digest('hex');
const clip = fs.readFileSync(args.clip);
check(clip.length > 0 && clip.length <= (1 << 20) && clip.length % 2 === 0 &&
  digest(clip) === args.clip_sha256, 'actual authorized PCM identity');
const journal = openJournal(args.journal_file, 8 << 20);
const rt = new LiveWS(new OpenAI({baseURL: url.toString(), apiKey: args.api_key, maxRetries: 0}), {
  ca: fs.readFileSync(args.ca_file), maxPayload: 4 << 20, perMessageDeflate: false,
  followRedirects: false, handshakeTimeout: 10000, reconnect: null,
});
let connections = 0, closed = false, closeCode = null, fatal = null, bytes = 0, sent = 0;
const events = [], deadline = Date.now()+300000;
const guard = setTimeout(() => {fatal = new Error('bounded SDK deadline');rt.socket.terminate();},300000);
rt.socket.on('open', () => {++connections;});
rt.on('close', (code, _reason, unsent) => {closed = true;closeCode = code;
  if (unsent.length) fatal = new Error('unsent client events');});
rt.on('error', (error) => {fatal = new Error('Live SDK: '+(error.error?.error?.code || error.name));});
rt.on('event', (event) => {
  try {
    bytes += Buffer.byteLength(JSON.stringify(event));
    check(bytes <= (4 << 20) && events.length < 4096, 'bounded wire events');
    journal.append('server',event); events.push(event);
  } catch (error) {fatal = error;rt.socket.terminate();}
});
async function until(predicate, allowClosed=false) {
  for (;;) {
    if (fatal) throw fatal;
    if (predicate()) return;
    check(Date.now()<deadline && (allowClosed || !closed),'deadline or premature close');
    await new Promise((resolve)=>setTimeout(resolve,2));
  }
}
function send(event) {
  check(++sent <= 40,'client event budget');journal.append('client',event);rt.send(event);
}
async function eventSince(at,type) {
  let result;await until(()=>{result=events.slice(at).find((e)=>e.type===type);return !!result;});return result;
}
async function action(event,type) {const at=events.length;send(event);return eventSince(at,type);}
const result = {status:'FAIL',events,automatic_replay:false,expected_input:args.expected_input};
try {
  await until(()=>connections===1);
  await action({type:'session.start',event_id:'start',session:{model:'qwen3.8-flash-next',
    instructions:'这是项目资料查询服务。用户询问项目资料时，先使用 q4t_live_delegate 委派客户端读取真实项目记录。收到客户端结果前不要回答或编造版本号。收到应用提供的真实结果后，只说版本号一次，不要解释，不要再次委派。',
    input:[{role:'developer',content:[{text:'保持项目查询任务和真实结果的关联。'}]},
      {role:'user',content:[{text:'接下来的录音包含我的问题，请先使用客户端查询。'}]},
      {role:'assistant',content:[{text:'明白。'}]}],
    audio:{format:{type:'audio/pcm',rate:24000},output:{voice:'vivian'}},
    delegation:{type:'client',acknowledgement_delay_ms:600000},store:false,
  }},'session.started');
  const fixtureAt = events.length;
  await action({type:'session.output_audio.fixture.play',event_id:'fixture_audio',
    audio:clip.toString('base64')},'session.output_audio.fixture.playing');
  send({type:'session.input_audio.append',event_id:'fixture_drop',
    audio:Buffer.concat([clip,Buffer.alloc(48000)]).toString('base64')});
  await action({type:'session.input_audio.mute',event_id:'mute'},'session.input_audio.muted');
  await eventSince(fixtureAt,'session.output_audio.fixture.played');
  check(!events.slice(fixtureAt).some((e)=>e.type==='session.input_transcript.delta'
    || e.type==='session.delegation.created'),'no inference from dropped fixture input');
  result.fixture={sha256:digest(clip),bytes:clip.length,played:true,listener_heard:false};
  send({type:'session.input_audio.append',event_id:'fixture_muted',
    audio:Buffer.concat([clip,Buffer.alloc(48000)]).toString('base64')});
  // The server accepts this voice clip under the preserved explicit mute.
  // Native VAD must not turn it into an ASR/LLM task after fixture playback.
  await new Promise((resolve)=>setTimeout(resolve,250));
  check(!events.slice(fixtureAt).some((e)=>e.type==='session.input_transcript.delta'
    || e.type==='session.delegation.created'),'fixture preserves selected input mute');
  await action({type:'session.input_audio.unmute',event_id:'unmute'},'session.input_audio.unmuted');
  await action({type:'session.update',event_id:'delay',session:{delegation:{type:'client',
    acknowledgement_delay_ms:600000}}},'session.updated');
  const at = events.length;
  for (const raw of [clip,Buffer.alloc(48000)])
    for (let offset=0;offset<raw.length;offset+=24000)
      send({type:'session.input_audio.append',audio:raw.subarray(offset,offset+24000).toString('base64')});
  await eventSince(at,'session.input_transcript.delta');
  const handoffDeadline = Date.now()+30000;
  let delegated;
  await until(()=>{delegated=clientHandoff(events.slice(at),Date.now(),handoffDeadline);return !!delegated;});
  result.delegation_id = delegated.delegation.id;
  // The application now performs a real read; no task text/result fabricated by the server.
  const raw = fs.readFileSync(args.record_file);
  check(raw.length <= (1 << 20) && digest(raw)===args.record_sha256,'project record identity');
  const match = raw.toString().match(/默认\s*rev(\d+)/);
  check(match && 'rev'+match[1]===args.expected_version,'actual project version');
  result.record = {path:'docs/STATUS.md',sha256:digest(raw),version:'rev'+match[1]};
  await action({type:'session.thinking.append',event_id:'project_result',
    delegation_id:result.delegation_id,content:JSON.stringify(result.record)},'session.thinking.appended');
  const speechAt = events.length;
  await action({type:'session.commentary.append',event_id:'project_answer',
    delegation_id:result.delegation_id,content:'项目资料已读取。只说刚才真实记录中的版本号一次。'},
    'session.commentary.appended');
  await eventSince(speechAt,'session.output_transcript.delta');
  await action({type:'session.close',event_id:'finish'},'session.closed');
  await until(()=>closed,true);check(closeCode===1000,'normal drained close');
  result.status='PASS';
} catch (error) {result.error=String(error.message);process.exitCode=2;}
finally {
  clearTimeout(guard);if(!closed)rt.socket.terminate();journal.close();
  Object.assign(result,{connections,close_code:closeCode,client_events:sent});
  process.stdout.write(JSON.stringify(result)+'\n');
}
