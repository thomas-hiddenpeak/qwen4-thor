// Two real SDK attachments to the same ongoing media call, without implicit reconnect.
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';
import {check} from '../serve_realtime/conversation_checks.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  check(input.length < 65536, 'bounded arguments');
}
const args = JSON.parse(input);
const sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR, 'node_modules/openai');
check(JSON.parse(fs.readFileSync(path.join(sdk,'package.json'))).version === '7.47.0', 'SDK identity');
const url = new URL(args.base_url);
check(url.protocol === 'https:' && url.hostname === '127.0.0.1' && url.pathname === '/v1', 'local test endpoint');
const {default: OpenAI} = await import(pathToFileURL(path.join(sdk,'index.mjs')));
const {SidebandWS} = await import(pathToFileURL(path.join(sdk,'resources/live/sideband/ws.mjs')));
const client = new OpenAI({baseURL:url.toString(),apiKey:args.api_key,maxRetries:0});
const out = args.directory;
const file = (name)=>path.join(out,name);
const fixture = Buffer.alloc(9600);
for (let i=0;i<fixture.length/2;i++)
  fixture.writeInt16LE(Math.round(9000*Math.sin(2*Math.PI*850*i/24000)),2*i);

async function wait(test, name, seconds=6) {
  const until = Date.now()+seconds*1000;
  while (Date.now()<until) {
    const result=test();
    if (result) return result;
    await delay(5);
  }
  throw new Error('Timeout waiting for '+name);
}

function connect() {
  const ws = new SidebandWS(client,{session_id:args.session,graceful_close:true},{
    ca:fs.readFileSync(args.ca_file), reconnect:null, followRedirects:false,
    handshakeTimeout:6000,perMessageDeflate:false});
  const peer={ws,opened:0,events:[],error:'',closed:0};
  ws.socket.on('open',()=>peer.opened++);
  ws.on('event',e=>{check(peer.events.length<2000,'bounded event count');peer.events.push(e);});
  ws.on('error',e=>{peer.error=e.message||'Sideband error';});
  ws.on('close',(code,reason,unsent)=>{peer.closed=code;check(!unsent.length,'unsent commands');});
  return peer;
}
const event = (peer,type,id)=>wait(()=>peer.events.find(e=>e.type===type &&
  (!id || e.client_event_id===id)), type);
let a, b;
try {
  a=connect();
  await wait(()=>a.opened,'first open');
  a.ws.send({type:'session.input_audio.mute',event_id:'trusted_first'});
  const first=await event(a,'session.input_audio.muted','trusted_first');
  a.ws.close({code:1000,reason:'detach'});
  await wait(()=>a.closed===1000,'first detach');
  fs.writeFileSync(file('detached'), '1',{flag:'wx'});
  await wait(()=>fs.existsSync(file('primary-continued')),'primary control during detach');
  b=connect();
  const replay=await event(b,'session.input_audio.muted','trusted_first');
  check(replay.event_id===first.event_id,'original replay ID');
  await event(b,'session.input_audio.unmuted','primary_gap');
  b.ws.send({type:'session.input_audio.mute',event_id:'second_mute'});
  await event(b,'session.input_audio.muted','second_mute');
  fs.writeFileSync(file('muted'), '1',{flag:'wx'});
  await wait(()=>fs.existsSync(file('input-sent')),'received input sent by primary');
  const mutedInput=b.events.slice(b.events.findIndex(e=>e.client_event_id==='second_mute'))
    .filter(e=>e.type==='session.input_audio.append');
  check(mutedInput.length>0 && mutedInput.every(e=>!e.event_id && !('start_ms' in e) &&
    !('end_ms' in e)), 'reflected input shape');
  const inputPcm=Buffer.concat(mutedInput.map(e=>Buffer.from(e.audio,'base64')));
  fs.writeFileSync(file('input.pcm'),inputPcm,{flag:'wx'});
  b.ws.send({type:'session.output_audio.fixture.play',event_id:'real_fixture',audio:fixture.toString('base64')});
  await event(b,'session.output_audio.fixture.played');
  const frames=b.events.filter(e=>e.type==='session.output_audio.delta');
  let end;
  for (const f of frames) {
    const count=Buffer.from(f.delta,'base64').length;
    check(!('event_id' in f) && Number.isFinite(f.start_ms) && Number.isFinite(f.end_ms) &&
      f.start_ms>=0 && (end===undefined || Math.abs(f.start_ms-end)<.0001) &&
      Math.abs((f.end_ms-f.start_ms)*48-count)<.001,'contiguous PCM session clock');
    end=f.end_ms;
  }
  check(Buffer.concat(frames.map(f=>Buffer.from(f.delta,'base64'))).equals(fixture),'reflected fixture bytes');
  await wait(()=>fs.existsSync(file('rtp-received')),'actual RTP output');
  b.ws.send({type:'session.close',event_id:'finish'});
  const closed=await event(b,'session.closed','finish');
  await wait(()=>b.closed===1000,'normal drain');
  check(a.opened===1 && b.opened===1 && !a.error && !b.error && closed.session.id===args.session,'same owner completed');
  console.log(JSON.stringify({opens:2,codes:[a.closed,b.closed],replayed_event_id:replay.event_id,
    replay_occurrences:b.events.filter(e=>e.event_id===replay.event_id).length,input_bytes:inputPcm.length,
    output_bytes:fixture.length,output_frames:frames.length,output_begin_ms:frames[0].start_ms,
    output_end_ms:end,ended_session:closed.session.id}));
} finally {
  for (const peer of [a,b]) if(peer && !peer.closed) peer.ws.close({code:1000,reason:'cleanup'});
}
