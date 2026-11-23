// Real SDK application: accept the call, look up the spoken request, answer the caller.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {pathToFileURL} from 'node:url';
import {functionCall} from '../serve_realtime/conversation_checks.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part;
  assert.ok(input.length <= 32768);
}
const args = JSON.parse(input);
const sdk = path.resolve(args.sdk, 'node_modules/openai');
assert.equal(JSON.parse(fs.readFileSync(path.join(sdk, 'package.json'))).version, '7.47.0');
const {default: OpenAI} = await import(pathToFileURL(path.join(sdk, 'index.mjs')));
const {OpenAIRealtimeWS} = await import(pathToFileURL(path.join(sdk, 'realtime/ws.mjs')));
const baseURL = 'https://127.0.0.1:' + args.port + '/v1';
const client = apiKey => new OpenAI({apiKey, baseURL, maxRetries:0, timeout:12000});
const owner = client(args.key), other = client(args.other), id = args.realtime_id;
const marker = name => path.join(args.out, name);
const save = (name, value) => fs.writeFileSync(marker(name), JSON.stringify(value), {flag:'wx'});
assert.match(id, /^rtc_[A-Za-z0-9_-]+$/);
const options = {ca:fs.readFileSync(args.ca), followRedirects:false, perMessageDeflate:false,
                 handshakeTimeout:6000, maxPayload:8<<20};
const attach = client => new OpenAIRealtimeWS({callID:id, options}, client);
const config = {type:'realtime', model:'qwen3.8-flash-next',
  instructions:'根据用户的录音，调用工具查询实际项目记录。', output_modalities:['audio'],
  audio:{input:{format:{type:'audio/pcm',rate:24000},turn_detection:null,
                transcription:{model:'qwen3-asr-0.6b'}}, output:{voice:'vivian'}}};
let rt, fatal, closeCode, transcript = '', answer = '', version;
const events = [];
async function until(probe, label, seconds=20) {
  const end = Date.now() + seconds*1000;
  while (Date.now() < end) {
    if (fatal) throw fatal;
    const value = probe();
    if (value) return value;
    if (closeCode !== undefined) throw new Error('premature close ' + closeCode + ' during ' + label);
    await delay(10);
  }
  throw new Error('deadline for ' + label);
}
const waitEvent = (type, from=0) => until(()=>events.slice(from).find(e=>e.type===type), type, 55);
const denied = (request, status) => assert.rejects(request, e => e.status === status);
try {
  await denied(other.realtime.calls.accept(id, config), 404);
  await denied(owner.realtime.calls.accept(id, {session:{type:'live'}}), 400);
  const accept = await owner.realtime.calls.accept(id, config).withResponse();
  assert.equal(accept.response.status, 200);
  assert.equal(accept.data, undefined);
  save('sdk-accepted', 200);
  await denied(other.realtime.calls.hangup(id), 404);
  const foreign = attach(other);
  foreign.on('error', ()=>{});
  const foreignStatus = await new Promise((resolve, reject) => {
    const timer = setTimeout(()=>reject(new Error('foreign attach deadline')),7000);
    foreign.socket.on('unexpected-response', (_, res) => {
      clearTimeout(timer);
      res.resume();
      resolve(res.statusCode);
    });
    foreign.socket.once('open', ()=>{clearTimeout(timer); reject(new Error('foreign call attached'));});
  }).finally(()=>foreign.socket.terminate());
  assert.equal(foreignStatus,404);
  await delay(3200); // Attach beyond the Live replay window.
  rt = attach(owner);
  rt.socket.on('close', code=>{closeCode=code;});
  rt.on('error', error=>{fatal=error;});
  rt.on('event', e=>{
    if (events.length >= 4096) {fatal=new Error('event bound'); return;}
    events.push(e);
  });
  const {session} = await waitEvent('session.created');
  assert.equal(session.type,'realtime');
  assert.match(session.id,/^sess_/);
  assert.equal(session.instructions,config.instructions);
  save('sdk-attached',true);
  const digit = await waitEvent('input_audio_buffer.dtmf_event_received');
  assert.equal(digit.event,'#');
  assert.ok(Math.abs(Date.now()/1000-digit.received_at)<10);
  await until(()=>fs.existsSync(marker('carrier-input-ended')), 'carrier input');
  await delay(200);
  rt.send({type:'input_audio_buffer.commit',event_id:'commit_phone'});
  transcript = (await waitEvent('conversation.item.input_audio_transcription.completed')).transcript;
  save('sdk-transcript.json',transcript);
  await until(()=>fs.existsSync(marker('verified-input.json')),'verified spoken query');
  rt.send({type:'response.create', response:{output_modalities:['text'],max_output_tokens:128,
    instructions:'Use lookup_project_record with record_key release for the spoken project version question.',
    tools:[{type:'function',name:'lookup_project_record',parameters:{type:'object',
      properties:{record_key:{type:'string',enum:['release']}},required:['record_key'],additionalProperties:false}}],
    tool_choice:{type:'function',name:'lookup_project_record'}}});
  const first = (await waitEvent('response.done')).response;
  const call = functionCall(first);
  const raw = fs.readFileSync(args.record.path);
  assert.equal(createHash('sha256').update(raw).digest('hex'),args.record.sha256);
  version = /默认\s*(rev\d+)/.exec(raw.toString())[1];
  rt.send({type:'conversation.item.create',item:{type:'function_call_output',id:'release_result',
    call_id:call.call_id,output:JSON.stringify({record_key:'release',version,source:'docs/STATUS.md',sha256:args.record.sha256})}});
  await until(()=>events.find(e=>e.type==='conversation.item.done' && e.item?.id==='release_result'),'stored real result');
  const next = events.length;
  rt.send({type:'response.create',response:{output_modalities:['audio'],tools:[],tool_choice:'none',
    // Bounded duplex profile includes 16 codec tokens per generated speech frame.
    max_output_tokens:3457,instructions:'只朗读刚收到的真实工具结果中的版本号一次，保留 rev 前缀，不要添加其他内容。'}});
  const response = (await waitEvent('response.done',next)).response;
  save('sdk-speech-terminal.json',{status:response.status,details:response.status_details,
    transcript:response.output.flatMap(i=>(i.content || []).map(p=>p.transcript || '')).join(''),
    usage:response.usage});
  assert.equal(response.status,'completed');
  const start = await waitEvent('output_audio_buffer.started',next);
  const stop = await waitEvent('output_audio_buffer.stopped',next);
  assert.equal(start.response_id,response.id);
  assert.equal(stop.response_id,response.id);
  answer = (await waitEvent('response.output_audio_transcript.done',next)).transcript;
  assert.equal(answer.replace(/[^a-z0-9]/gi,'').toLowerCase(),version);
  await until(()=>fs.existsSync(marker('carrier-heard')),'carrier playout');
  const hung = await owner.realtime.calls.hangup(id).withResponse();
  assert.equal(hung.response.status,200);
  assert.equal(hung.data,undefined);
  await until(()=>closeCode===1000,'sideband close');
  await denied(owner.realtime.calls.hangup(id),404);
  save('sdk-receipts.json',{foreign_accept:404,invalid:400,accepted:200,foreign_hangup:404,
    foreign_attach:foreignStatus,late_attach_ms:3200,hangup:200,retired:404,sideband_close:closeCode,
    actual_tool:call.name,tool_result_version:version,playout_response_matched:true});
} finally {
  save('voice.json',{version,input:transcript,output:answer,kinds:events.map(e=>e.type)});
  if (rt && closeCode===undefined) rt.socket.terminate();
}
