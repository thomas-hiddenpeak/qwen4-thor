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
function open(account, target, Socket = OpenAIRealtimeWS, socketOptions = options) {
  const events = [];
  let failure, status, bytes = 0;
  const socket = new Socket({...target, options:socketOptions}, account);
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
async function legacyTranscription() {
  const sessions = parent.beta.realtime.transcriptionSessions;
  check(await status(sessions.create({client_secret:{expires_at:{seconds:9}}})) === 400, 'expiry minimum');
  check(await status(sessions.create({input_audio_format:'mp3'})) === 400, 'invalid wire encoding');
  const before = Math.floor(Date.now()/1000);
  const {data:session,response} = await sessions.create({input_audio_format:'pcm16',
    input_audio_transcription:{model:'qwen3-asr-0.6b',language:'zh'}, turn_detection:null,
    client_secret:{expires_at:{anchor:'created_at',seconds:15}}}).withResponse();
  const {value,expires_at:expires} = session.client_secret;
  check(response.status===200 && session.object==='realtime.transcription_session' &&
    session.input_audio_format==='pcm16' && session.input_audio_transcription.language==='zh' &&
    value.startsWith('ek_') && expires>=before+15 && expires<=Math.floor(Date.now()/1000)+15,
    'typed issuance and expiry');
  const delegated = client(value);
  check(await status(delegated.beta.realtime.transcriptionSessions.create({}))===401, 'recursive issuance');
  check(await status(delegated.models.list())===401, 'credential scope');
  const beta = !!args.beta_transcription_wire;
  const connectOptions = beta ? {...options, headers:{'OpenAI-Beta':'realtime=v1'}} : options;
  const connection = open(delegated,{intent:'transcription'},OpenAIRealtimeWS,connectOptions);
  try {
    const created = (await connection.wait('session.created')).session;
    if (beta) {
      check(created.input_audio_transcription.language==='zh' && created.turn_detection===null &&
        created.input_audio_format==='pcm16' && !('audio' in created), 'beta transcription startup');
      connection.send({type:'transcription_session.update',session:{input_audio_format:'pcm16',
        input_audio_transcription:{model:'qwen3-asr-0.6b',language:'zh'},turn_detection:null}});
      const updated = (await connection.wait('transcription_session.updated')).session;
      check(updated.id===created.id && updated.input_audio_transcription.language==='zh' &&
        updated.input_audio_format==='pcm16', 'beta transcription update');
    } else {
      check(created.audio.input.transcription.language==='zh' && created.audio.input.turn_detection===null &&
        created.audio.input.format.type==='audio/pcm', 'inherited transcription settings');
    }
    connection.send({type:'input_audio_buffer.append',audio:fs.readFileSync(args.pcm).toString('base64')});
    connection.send({type:'input_audio_buffer.commit'});
    const event = await connection.wait('conversation.item.input_audio_transcription.completed');
    const modelReply = await parent.chat.completions.create({model,
      messages:[{role:'user',content:'What is 6 times 7? Reply with only the number.'}],
      temperature:0,reasoning_effort:'none',max_tokens:20});
    const reply = modelReply.choices[0].message.content.trim();
    check(reply==='42','fresh Chat');
    return {sdk:'7.47.0',issued:200,invalid_ttl:400,invalid_format:400,recursive_issue:401,
      models:401,requested_ttl:15,language:'zh',transcript:event.transcript,independent_chat:reply,
      ...(beta ? {beta_start:'session.created',beta_update:'transcription_session.updated'} : {})};
  } finally {connection.close();}
}

async function legacyWire() {
  const {OpenAIRealtimeWS:BetaSocket} = await import(pathToFileURL(path.join(root, 'beta/realtime/ws.mjs')));
  const grant = await parent.beta.realtime.sessions.create({model, modalities:['text'],
    temperature:.75, turn_detection:null, max_response_output_tokens:32,
    client_secret:{expires_after:{seconds:30}}});
  const socket = open(client(grant.client_secret.value),{model},BetaSocket);
  try {
    const created = (await socket.wait('session.created')).session;
    check(created.temperature===.75 && created.modalities[0]==='text' &&
      !('output_modalities' in created), 'beta session.created');
    socket.send({type:'session.update',session:{temperature:.9,modalities:['text'],
      instructions:'Answer arithmetic with only the resulting integer.'}});
    const updated = (await socket.wait('session.updated')).session;
    check(updated.temperature===.9 && updated.max_response_output_tokens===32, 'beta update');
    socket.send({type:'conversation.item.create',item:{type:'message',role:'user',
      content:[{type:'input_text',text:'What is 6 times 7? Reply with only the number.'}]}});
    const item = (await socket.wait('conversation.item.created')).item;
    check(item.role==='user' && item.content[0].text.includes('6 times 7'), 'beta item receipt');
    socket.send({type:'response.create',response:{temperature:.6,
      max_response_output_tokens:32,modalities:['text']}});
    const result = (await socket.wait('response.done')).response;
    const answer = result.output.flatMap(i=>i.content||[]).filter(p=>p.type==='text')
      .map(p=>p.text).join('').trim();
    check(result.status==='completed' && answer==='42' && usage(result)>0, 'beta response resource');
    const start = (await socket.wait('response.created')).response;
    check(start.id===result.id && start.temperature===.6 && result.temperature===.6 &&
      start.max_output_tokens===32 && result.max_output_tokens===32, 'per-response execution settings');
    const completed = await socket.wait('response.text.done');
    const delta = await socket.wait('response.text.delta');
    check(completed.text.trim()==='42' && delta.response_id===result.id &&
      delta.item_id===completed.item_id, 'beta delta and completion');
    return {sdk:'7.47.0',beta_session:true,issued_temperature:.75,updated_temperature:.9,
      requested_temperature:.6,reported_temperature:result.temperature,max_output_tokens:result.max_output_tokens,
      delta:'response.text.delta',completed:'response.text.done',
      answer,tokens:usage(result)};
  } finally {socket.close();}
}

async function legacyConversation() {
  const issue = body => parent.beta.realtime.sessions.create(body);
  check(await status(issue({temperature:1.21}))===400, 'temperature upper bound');
  check(await status(issue({client_secret:{expires_at:{seconds:15}}}))===400, 'expiry spelling');
  const instructions = 'Answer arithmetic with only the resulting integer.';
  const {data, response} = await issue({model, instructions, modalities:['text'],
    max_response_output_tokens:32, temperature:.75, turn_detection:null,
    client_secret:{expires_after:{anchor:'created_at',seconds:15}}}).withResponse();
  check(response.status===200 && response.headers.get('cache-control')?.includes('no-store'),
    'private legacy credential');
  check(data.object==='realtime.session' && data.instructions===instructions &&
    data.temperature===.75 && data.max_response_output_tokens===32 &&
    data.modalities.length===1 && data.modalities[0]==='text', 'legacy effective settings');
  const {value, expires_at:expires} = data.client_secret;
  check(value.startsWith('ek_') && expires > Date.now()/1000 &&
    expires <= Math.floor(Date.now()/1000)+15, 'owner credential lifetime');
  const delegated = client(value);
  check(await status(delegated.beta.realtime.sessions.create({}))===401, 'recursive issuance');
  check(await status(delegated.models.list())===401, 'credential scope');
  const socket = open(delegated,{model});
  try {
    const created = (await socket.wait('session.created')).session;
    check(created.instructions===instructions && created.max_output_tokens===32 &&
      created.output_modalities[0]==='text' && !('temperature' in created), 'native startup');
    socket.send({type:'conversation.item.create',item:{type:'message',role:'user',
      content:[{type:'input_text',text:'What is 6 times 7? Reply with only the number.'}]}});
    socket.send({type:'response.create'});
    const result = (await socket.wait('response.done')).response;
    check(result.status==='completed' && text(result).trim()==='42' && usage(result)>0,
      'legacy credential conversation');
    const modelReply = await parent.chat.completions.create({model,
      messages:[{role:'user',content:'What is 6 times 7? Reply with only the number.'}],
      temperature:0,reasoning_effort:'none',max_tokens:20});
    const reply = modelReply.choices[0].message.content.trim();
    check(reply==='42','separate Chat');
    return {sdk:'7.47.0',issued:200,requested_ttl:15,recursive_issue:401,models:401,
      invalid_temperature:400,invalid_expiry:400,temperature:.75,max_output_tokens:32,
      socket_answer:'42', tokens:usage(result), independent_chat:reply};
  } finally { socket.close(); }
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
  console.log(JSON.stringify(args.legacy_transcription ? await legacyTranscription() :
    args.legacy_conversation ? await legacyConversation() :
    args.legacy_wire ? await legacyWire() : await run()));
} catch (e) {
  // SDK error objects may hold request metadata; never print them wholesale.
  console.error('SDK check failed', e.status || '', e.code || '', String(e.message).slice(0,320));
  process.exitCode=1;
}
