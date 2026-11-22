// Official SDK application for one inbound phone call and a real project lookup.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {pathToFileURL} from 'node:url';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(input.length <= 32768);
}
const args = JSON.parse(input);
const sdk = path.resolve(args.sdk, 'node_modules/openai');
assert.equal(JSON.parse(fs.readFileSync(path.join(sdk, 'package.json'))).version, '7.47.0');
const {default: OpenAI} = await import(pathToFileURL(path.join(sdk, 'index.mjs')));
const {SidebandWS} = await import(pathToFileURL(path.join(sdk, 'resources/live/sideband/ws.mjs')));
const baseURL = 'https://127.0.0.1:' + args.port + '/v1';
const client = apiKey => new OpenAI({apiKey, baseURL, maxRetries:0, timeout:12000});
const owner = client(args.key), other = client(args.other);
const marker = name => path.join(args.out, name);
const save = (name, data) => fs.writeFileSync(marker(name), JSON.stringify(data), {flag:'wx'});
const calls = owner.live.sessions;
const session = {
  type:'live', model:'qwen3.8-flash-next', instructions:
    '这是项目资料查询服务。用户询问项目资料时，先使用 q4t_live_delegate 委派客户端读取真实项目记录。收到客户端结果前不要回答或编造版本号。收到应用提供的真实结果后，只说版本号一次，不要解释，不要再次委派。',
  input:[{role:'developer',content:[{text:'保持项目查询任务和真实结果的关联。'}]},
    {role:'user',content:[{text:'接下来的录音包含我的问题，请先使用客户端查询。'}]},
    {role:'assistant',content:[{text:'明白。'}]}],
  audio:{output:{voice:'vivian'}}, delegation:{type:'client',acknowledgement_delay_ms:600000}
};
async function denied(request, status) {
  await assert.rejects(request, e => e.status === status);
}
async function wait(predicate, label, seconds = 10) {
  const end = Date.now() + seconds * 1000;
  while (Date.now() < end) {
    if (failure) throw new Error(failure);
    const value = predicate();
    if (value) return value;
    await delay(10);
  }
  throw new Error('Timed out waiting for ' + label);
}
if (args.blocked_media) {
  await assert.rejects(calls.accept(args.id, {session}), e =>
    e.status === 503 && e.code === 'live_media_unavailable');
  save('unready-media.json', {http_status:503, code:'live_media_unavailable'});
  process.exit(0);
}
let failure, ws, closed, version;
const events = [];
try {
  await denied(other.live.sessions.accept(args.id, {session}), 404);
  await denied(calls.accept(args.id, {session:{...session, type:'realtime'}}), 400);
  const accepted = await calls.accept(args.id, {session}).withResponse();
  assert.equal(accepted.response.status, 200);
  assert.equal(accepted.data, undefined);
  save('sdk-accepted', 200);
  await denied(other.live.sessions.hangup(args.id), 404);
  ws = new SidebandWS(owner, {session_id:args.id, graceful_close:true},
    {ca:fs.readFileSync(args.ca), reconnect:null, followRedirects:false,
     handshakeTimeout:6000, perMessageDeflate:false});
  ws.on('event', e => {
    if (e.type === 'session.input_audio.append' || e.type === 'session.output_audio.delta') return;
    if (events.length >= 2000) { failure = 'event limit'; return; }
    events.push(e);
    if (e.type === 'error') failure = e.error?.code ?? 'protocol error';
  });
  ws.on('error', e => { failure = e.message; });
  ws.on('close', code => { closed = code; });
  const event = (type, seconds) => wait(() => events.find(e => e.type === type), type, seconds);
  const started = await event('session.started');
  assert.equal(started.session.id, args.id);
  save('sdk-attached', true);
  if (args.realtime_id) {
    const phone = {target_uri:'tel:+12025550100'};
    const rtc = args.realtime_id;
    await denied(other.live.sessions.refer(args.id, phone), 404);
    await denied(other.realtime.calls.refer(rtc, phone), 404);
    await denied(calls.refer(args.id, {target_uri:'sip:bad\r\nHeader: value'}), 400);
    await assert.rejects(calls.refer(args.id, phone),
      e => e.status === 502 && e.code === 'sip_refer_rejected' && e.message.includes('486'));
    const sent = await owner.realtime.calls.refer(rtc,
      {target_uri:'sips:help@desk.invalid;transport=tls?Subject=A%20B'}).withResponse();
    assert.equal(sent.response.status, 200);
    assert.equal(sent.data, undefined);
    await denied(calls.refer(args.id, phone), 409);
    save('transfer-overlap', true);
    await wait(() => fs.existsSync(marker('retry-allowed')), 'first subscription final');
    const next = await calls.refer(args.id, {target_uri:'tel:+12025550101'}).withResponse();
    assert.equal(next.response.status, 200);
    assert.equal(next.data, undefined);
    save('transfer-sdk.json', {foreign_live:404, foreign_realtime:404, invalid:400,
      carrier_refusal:502, realtime:200, overlap:409, live:200});
  }
  const {delegation} = await event('session.delegation.created', 55);
  assert.equal(delegation.target, 'client');
  const record = fs.readFileSync(args.record.path);
  assert.equal(createHash('sha256').update(record).digest('hex'), args.record.sha256);
  version = /默认\s*(rev\d+)/.exec(record.toString())[1];
  ws.send({type:'session.thinking.append',event_id:'lookup',delegation_id:delegation.id,
           content:JSON.stringify({...args.record,version})});
  await event('session.thinking.appended');
  ws.send({type:'session.commentary.append',event_id:'answer',delegation_id:delegation.id,
           content:'项目资料已读取。只说刚才真实记录中的版本号一次。'});
  await event('session.commentary.appended');
  await event('session.output_transcript.delta', 60);
  await wait(() => fs.existsSync(marker('carrier-heard')), 'carrier playback', 30);
  const hung = await calls.hangup(args.id).withResponse();
  assert.equal(hung.response.status, 200);
  assert.equal(hung.data, undefined);
  assert.equal((await event('session.closed', 20)).session.id, args.id);
  await wait(() => closed === 1000, 'normal sideband close');
  await denied(calls.hangup(args.id), 404);
  save('sdk-receipts.json', {foreign_accept:404, invalid:400, accepted:200,
    foreign_hangup:404, hangup:200, retired:404, sideband_close:closed});
} finally {
  const text = type => events.filter(e => e.type === type).map(e => e.delta).join('');
  save('voice.json', {version, input:text('session.input_transcript.delta'),
    output:text('session.output_transcript.delta'), kinds:events.map(e=>e.type)});
  if (ws && !closed) ws.close({code:1000,reason:'fixture cleanup'});
}
