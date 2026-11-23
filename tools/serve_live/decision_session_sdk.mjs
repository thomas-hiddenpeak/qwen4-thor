// Official SDK, one connection, one real audio query, no automatic reconnect.
import fs from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {check} from '../serve_realtime/conversation_checks.mjs';
import {openJournal} from '../serve_realtime/bounded_journal.mjs';

let input='';
for await (const part of process.stdin) {
  input+=part;
  check(Buffer.byteLength(input)<=65536,'argument bound');
}
const args=JSON.parse(input);
const sdk=path.resolve(process.env.Q4T_NODE_SDK_DIR,'node_modules/openai');
check(JSON.parse(fs.readFileSync(path.join(sdk,'package.json'))).version==='7.47.0','SDK identity');
const {default:OpenAI}=await import(pathToFileURL(path.join(sdk,'index.mjs')));
const {LiveWS}=await import(pathToFileURL(path.join(sdk,'resources/live/ws.mjs')));
const origin=new URL(args.base_url);
check(origin.origin==='https://127.0.0.1:'+origin.port && origin.pathname==='/v1' &&
      !origin.username && !origin.password && !origin.search && !origin.hash,'local verified TLS');
const read = (file, hash) => {
  const bytes=fs.readFileSync(file);
  check(bytes.length<1<<20 && crypto.createHash('sha256').update(bytes).digest('hex')===hash,'source identity');
  return bytes;
};
const clip=read(args.clip,args.clip_sha256);
const record=JSON.parse(read(args.record_file,args.record_sha256)).observation;
const model='qwen3.8-flash-next';
const question={name:'version',type:'choice',instructions:'Select the default version in the project acceptance record.',
                choices:[{value:'rev000'},{value:record.version},{value:'unknown'}]};
const trigger=(source,name,instructions)=>({type:'decisions',event_type:'session.'+source+'_transcript.delta',
  decisions:{questions:[{name,type:'predicate',instructions}]}});
const journal=openJournal(args.journal_file,8<<20);
const socket=new LiveWS(new OpenAI({baseURL:origin.toString(),apiKey:args.api_key,maxRetries:0}),{
  ca:fs.readFileSync(args.ca_file),maxPayload:4<<20,perMessageDeflate:false,followRedirects:false,
  handshakeTimeout:10000,reconnect:null});
let failure,opens=0,closed=false,closeCode,sent=0,bytes=0;
const events=[];
socket.socket.on('open',()=>{++opens;});
socket.on('close',(code,reason,pending)=>{closed=true;closeCode=code;if(pending.length)failure=new Error('unsent commands');});
socket.on('error',e=>{failure=new Error(e.error?.error?.code||e.message||'Live error');});
socket.on('event',event=>{
  try {
    bytes+=Buffer.byteLength(JSON.stringify(event));
    check(events.length<4096 && bytes<6<<20,'bounded wire events');
    journal.append('server',event);events.push(event);
  } catch(e) {failure=e;socket.socket.terminate();}
});
const timer=setTimeout(()=>{failure=new Error('bounded SDK deadline');socket.socket.terminate();},180000);
async function until(predicate) {
  for (;;) {
    if(failure)throw failure;
    const value=predicate();if(value)return value;
    check(!closed,'premature close');
    await new Promise(resolve=>setTimeout(resolve,3));
  }
}
function send(event) {
  check(++sent<=32,'bounded client events');
  journal.append('client',event);socket.send(event);
}
const find=(type,predicate=()=>true)=>until(()=>events.find(e=>e.type===type && predicate(e)));
const result={status:'FAIL'};
try {
  await until(()=>opens===1);
  send({type:'session.start',event_id:'start',session:{model,store:false,
    instructions:'你负责项目资料。用户询问项目版本时，必须调用 q4t_live_delegate 委派查询。得到应用确认的结果后只说版本号一次。',
    delegation:{type:'decisions',acknowledgement_delay_ms:600000,decisions:{model,questions:[question]}},
    triggers:[trigger('input','asked','The caller is asking for the current project version.'),
              trigger('output','spoken','The assistant has just spoken the default project version.')] }});
  await find('session.started');
  send({type:'decision.item.create',event_id:'add',item:{id:'release_record',role:'user',
    content:[{type:'input_json',data:{source:'project acceptance record',default_version:record.version,
                                     document_sha256:args.record_sha256}}]}});
  await find('decision.item.created');
  send({type:'decision.create',event_id:'manual'});
  const manual=await find('session.delegation.created',e=>e.client_event_id==='manual');
  await find('decision.event',e=>e.delegation_id===manual.delegation.id);
  for (const raw of [clip,Buffer.alloc(48000)]) {
    for(let offset=0;offset<raw.length;offset+=24000)
      send({type:'session.input_audio.append',audio:raw.subarray(offset,offset+24000).toString('base64')});
  }
  const auto=await find('session.delegation.created',e=>e.delegation.id!==manual.delegation.id);
  const evaluated=await find('decision.event',e=>e.delegation_id===auto.delegation.id);
  await find('decision.event',e=>e.source_event_type==='session.input_transcript.delta');
  const answer=evaluated.event.decision.answers[0];
  check(answer.type==='choice','voice decision returned no choice');
  send({type:'session.commentary.append',event_id:'tell',delegation_id:auto.delegation.id,
        content:'请只说这个已核实的项目版本号一次：'+answer.choice});
  await find('session.commentary.appended');
  const spoken=await find('session.output_transcript.delta',e=>e.client_event_id==='tell');
  await find('decision.event',e=>e.source_event_id===spoken.event_id);
  send({type:'session.close',event_id:'finish'});
  await find('session.closed');await until(()=>closed);
  check(closeCode===1000 && opens===1,'normal close');
  result.status='PASS';
} catch(e) {result.error=String(e.message);process.exitCode=2;}
finally {
  clearTimeout(timer);if(!closed)socket.socket.terminate();
  journal.close();Object.assign(result,{opens,sent,close_code:closeCode});
  process.stdout.write(JSON.stringify(result)+'\n');
}
