// Real TLS issuance and WSS redemption. Only public observations go to stdout.
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {check, text, usage} from '../serve_realtime/conversation_checks.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  check(Buffer.byteLength(input) < 8192, 'bounded SDK input');
}
const args = JSON.parse(input);
const root = path.resolve(process.env.Q4T_NODE_SDK_DIR, 'node_modules/openai');
check(JSON.parse(fs.readFileSync(path.join(root, 'package.json'))).version === '7.47.0', 'SDK identity');
const endpoint = new URL(args.base_url);
check(endpoint.protocol === 'https:' && endpoint.hostname === '127.0.0.1' &&
      endpoint.pathname === '/v1' && !endpoint.username && !endpoint.password &&
      !endpoint.search && !endpoint.hash, 'local TLS fixture');
const {default: OpenAI} = await import(pathToFileURL(path.join(root, 'index.mjs')));
const {OpenAIRealtimeWS} = await import(pathToFileURL(path.join(root, 'realtime/ws.mjs')));
const client = (apiKey) => new OpenAI({baseURL:args.base_url, apiKey, maxRetries:0, timeout:10000});
const parent = client(args.key);
const model = 'qwen3.8-flash-next';
const options = {perMessageDeflate:false, followRedirects:false, handshakeTimeout:5000, maxPayload:4<<20};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function open(account, target) {
  const events = [];
  let failure, status, bytes = 0;
  const socket = new OpenAIRealtimeWS({...target, options}, account);
  socket.socket.on('unexpected-response', (_,res) => {status = res.statusCode; res.resume(); socket.socket.terminate();});
  socket.on('error', (err) => { failure = err.error?.code || 'transport_error'; });
  socket.on('event', (e) => {
    bytes += Buffer.byteLength(JSON.stringify(e));
    if (events.length >= 256 || bytes > (4<<20)) {failure = 'event_limit'; socket.socket.terminate(); return;}
    events.push(e);
  });
  async function wait(type) {
    const limit = Date.now() + 30000;
    for (;;) {
      if (status) return {status};
      const found = events.find((e) => e.type === type);
      if (found) return found;
      if (failure) throw new Error('WSS '+type+': '+failure);
      check(Date.now() < limit, 'deadline: '+type);
      await sleep(8);
    }
  }
  return {wait, send:v=>socket.send(v), close:()=>socket.close()};
}
async function status(pending) {
  try {return (await pending.asResponse()).status;} catch (e) {return e.status;}
}
async function run() {
  const instructions = 'Reply with the exact requested number.';
  const {data:grant,response} = await parent.realtime.clientSecrets.create({
    expires_after:{anchor:'created_at',seconds:10},
    session:{type:'realtime',model,output_modalities:['text'],instructions}
  }).withResponse();
  check(response.status===200 && response.headers.get('cache-control')?.includes('no-store') &&
        grant.session.instructions===instructions && typeof grant.value==='string', 'credential issue');
  const child = client(grant.value);
  const connection = open(child, {model});
  let held, total;
  try {
    const created = (await connection.wait('session.created')).session;
    check(created.instructions===instructions && created.output_modalities[0]==='text', 'startup inheritance');
    held = await open(parent,{model}).wait('session.created');
    check(held.status===429, 'same client concurrency');
    check(await status(child.realtime.clientSecrets.create({}))===401, 'credential cannot mint');
    check(await status(child.models.list())===401, 'credential cannot list models');
    const waitMs = grant.expires_at * 1000 - Date.now() + 150;
    check(waitMs <= 10500, 'expiry bound');
    await sleep(Math.max(0,waitMs));
    check((await open(child,{model}).wait('session.created')).status===401, 'new use after expiry');
    connection.send({type:'conversation.item.create',item:{type:'message',role:'user',
      content:[{type:'input_text',text:'What is 6 times 7? Reply with only the number.'}]}});
    connection.send({type:'response.create',response:{output_modalities:['text'],max_output_tokens:32}});
    const result = (await connection.wait('response.done')).response;
    check(result.status==='completed' && text(result).trim()==='42','existing session math');
    total = usage(result);
  } finally {connection.close();}
  const speech = await parent.realtime.clientSecrets.create({session:{type:'transcription',
    audio:{input:{transcription:{model:'qwen3-asr-0.6b',language:'zh'},turn_detection:null}}}});
  check(speech.session.type==='transcription','transcription issuance');
  const asr = open(client(speech.value), {intent:'transcription'});
  let transcript;
  try {
    const e = await asr.wait('session.created');
    check(e.session?.audio?.input?.transcription?.language==='zh', 'ASR inherited language');
    asr.send({type:'input_audio_buffer.append',audio:fs.readFileSync(args.pcm).toString('base64')});
    asr.send({type:'input_audio_buffer.commit'});
    transcript = (await asr.wait('conversation.item.input_audio_transcription.completed')).transcript;
  } finally {asr.close();}
  return {issued:2, scoped_capacity:held.status, after_expiry:401, recursive_issue:401,
          models:401, existing_session_reply:'42', actual_tokens:total, transcript};
}
try {
  console.log(JSON.stringify(await run()));
} catch (e) {
  // SDK error objects may hold request metadata; never print them wholesale.
  console.error('SDK check failed', e.status || '', e.code || '', String(e.message).slice(0,320));
  process.exitCode=1;
}
