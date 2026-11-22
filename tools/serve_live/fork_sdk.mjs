// Fork a completed recording with the pinned SDK. The new request contains no answer.
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';
import {check} from '../serve_realtime/conversation_checks.mjs';
import {openJournal} from '../serve_realtime/bounded_journal.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  check(input.length <= 65536, 'bounded arguments');
}
const args = JSON.parse(input);
const sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR, 'node_modules/openai');
check(JSON.parse(fs.readFileSync(path.join(sdk,'package.json'))).version === '7.47.0', 'SDK version');
const url = new URL(args.base_url);
check(url.protocol === 'https:' && url.hostname === '127.0.0.1' && url.pathname === '/v1' &&
      !url.username && !url.password && !url.search && !url.hash, 'local verified TLS');
const {default: OpenAI} = await import(pathToFileURL(path.join(sdk,'index.mjs')));
const {ForksWS} = await import(pathToFileURL(path.join(sdk,'resources/live/forks/ws.mjs')));
const journal = openJournal(args.journal, 8 << 20);
const rt = new ForksWS(new OpenAI({baseURL:url.toString(),apiKey:args.key,maxRetries:0}),
  {session_id:args.source}, {ca:fs.readFileSync(args.ca_file), maxPayload:4<<20,
    perMessageDeflate:false,handshakeTimeout:8000,followRedirects:false,reconnect:null});
let opens = 0, closed = 0, fatal;
const events = [];
rt.socket.on('open',()=>++opens);
rt.on('close',(code,_reason,unsent)=>{closed=code;if(unsent.length) fatal='unsent commands';});
rt.on('error',e=>{fatal=e.error?.error?.message || e.message || 'SDK error';});
rt.on('event',e=>{
  try {
    check(events.length<4096, 'bounded event count');
    journal.append('server',e);
    events.push(e);
    if(e.type==='error') fatal=e.error?.message;
  } catch (e) {fatal=e.message;rt.socket.terminate();}
});
async function wait(test, name, seconds=12) {
  const end=Date.now()+seconds*1000;
  while(Date.now()<end) {
    if(fatal) throw new Error(fatal);
    const result=test();
    if(result) return result;
    check(!closed, 'early close waiting for '+name);
    await delay(5);
  }
  throw new Error('timeout waiting for '+name);
}
const find=type=>events.find(e=>e.type===type);
function send(e) { journal.append('client',e); rt.send(e); }

try {
  await wait(()=>opens===1,'open');
  send({type:'session.start',session:{},event_id:'fork'});
  const started=await wait(()=>find('session.started'),'session.started');
  await delay(350);
  check(!events.some(e=>['session.output_transcript.delta','session.output_audio.delta',
    'session.delegation.created','response.event'].includes(e.type)), 'old work executed');
  send({type:'session.commentary.append', event_id:'recall',delegation_id:null,
    content:'请逐字读出上一轮项目资料中 JSON 的 version 字段的完整值，保留英文字母和所有数字。只回答这个字段。'});
  await wait(()=>find('session.output_transcript.delta'),'spoken recall',70);
  send({type:'session.close',event_id:'end'});
  const ended=await wait(()=>find('session.closed'),'drain',55);
  await wait(()=>closed===1000,'normal close');
  check(ended.session.id===started.session.id && opens===1,'same child');
  console.log(JSON.stringify({opens,close_code:closed,id:started.session.id}));
} finally {
  if(!closed) rt.socket.terminate();
  journal.close();
}
