// Verify the official translation socket, signed short-lived credential, real
// clause completion before close, and independently transcribed output audio.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {resolve} from 'node:path';
import {readFileSync, writeFileSync} from 'node:fs';
import {setTimeout as sleep} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let body = '';
for await (const chunk of process.stdin) {
  body += chunk;
  assert.ok(body.length < 16384);
}
const plan = JSON.parse(body);
const base = new URL(plan.base);
assert.equal(base.protocol, 'https:');
assert.equal(base.hostname, '127.0.0.1');
const {OpenAI, version} = loadSdk(plan.sdk);
const require = createRequire(resolve(plan.sdk, 'node_modules/openai/package.json'));
const {OpenAIRealtimeTranslationWS} = require('openai/realtime/translations/ws');
const WebSocket = require('ws');
const client = key => new OpenAI({apiKey:key, baseURL:base.href, maxRetries:0, timeout:90000});
const owner = client(plan.key);
const model = 'gpt-realtime-translate';
const source = readFileSync(plan.clip);
assert.equal(createHash('sha256').update(source).digest('hex'), plan.sha256);
const grant = await owner.realtime.translations.clientSecrets.create({
  session:{model, audio:{input:{transcription:{model:'gpt-realtime-whisper'}},
                        output:{language:'en'}}},
  expires_after:{anchor:'created_at',seconds:180},
});
assert.ok(grant.value.startsWith('ek_'));
assert.equal(grant.session.type, 'translation');
assert.equal(grant.session.audio.output.language, 'en');
const ordinary = await owner.realtime.clientSecrets.create({session:{type:'realtime'}});
async function rejected(path, key) {
  const url = new URL(base);
  url.protocol = 'wss:';
  url.pathname += path;
  const socket = new WebSocket(url, {headers:{Authorization:'Bearer '+key}, handshakeTimeout:5000});
  return new Promise((accept, reject) => {
    socket.once('open', () => { socket.terminate(); reject(new Error('Unexpected audience')); });
    socket.once('error', reject);
    socket.once('unexpected-response', (_req, res) => {
      res.resume();
      accept(res.statusCode);
      socket.terminate();
    });
  });
}
assert.equal(await rejected('/realtime', grant.value), 401);
assert.equal(await rejected('/realtime/translations', ordinary.value), 401);
const connection = await OpenAIRealtimeTranslationWS.create(client(grant.value), {model});
const events = [];
let failure;
connection.on('error', error => { failure = error; });
connection.on('event', event => { events.push(structuredClone(event)); });
const waitFor = async predicate => {
  const until = Date.now() + 70000;
  while (!predicate()) {
    if (failure) throw failure;
    if (Date.now() >= until) throw new Error('Timed out waiting for translation events: '+events.map(e=>e.type).slice(-8));
    await sleep(30);
  }
};
const kind = type => events.filter(e => e.type === type);
const transcript = type => kind(type).map(e=>e.delta).join('');
const recognizesMeaning = text =>
  /trad(?:e|es|ing)|transaction/i.test(text) &&
  /stagn|halt|standstill|stop|stall|froze|frozen|suspend|cease|slow|grind|ground/i.test(text);
try {
  await waitFor(() => kind('session.created').length === 1);
  for (let at=0; at<source.length; at+=9600) {
    connection.send({type:'session.input_audio_buffer.append',
                     audio:source.subarray(at,at+9600).toString('base64')});
  }
  connection.send({type:'session.input_audio_buffer.append', audio:Buffer.alloc(48000).toString('base64')});
  await waitFor(() => kind('session.output_audio.delta').length && recognizesMeaning(transcript('session.output_transcript.delta')));
  const beforeClose = events.length;
  connection.send({type:'session.update', session:{audio:{input:{transcription:null}}}});
  await waitFor(() => kind('session.updated').length);
  assert.equal(kind('session.updated')[0].session.audio.input.transcription, null);
  await connection.finish({timeoutMs:75000});
  assert.ok(!failure);
  assert.equal(events.at(-1).type, 'session.closed');
  assert.equal(new Set(events.map(e=>e.event_id)).size, events.length);
  const input = transcript('session.input_transcript.delta');
  const english = transcript('session.output_transcript.delta');
  assert.match(input, /交易/);
  assert.ok(recognizesMeaning(english), english);
  const chunks = kind('session.output_audio.delta');
  const pcm = Buffer.concat(chunks.map(e=>{
    assert.equal(e.format,'pcm16'); assert.equal(e.channels,1); assert.equal(e.sample_rate,24000);
    return Buffer.from(e.delta,'base64');
  }));
  assert.ok(pcm.length>9600 && pcm.length % 2 === 0);
  const header = Buffer.alloc(44);
  header.write('RIFF'); header.writeUInt32LE(36+pcm.length,4); header.write('WAVEfmt ',8);
  header.writeUInt32LE(16,16); header.writeUInt16LE(1,20); header.writeUInt16LE(1,22);
  header.writeUInt32LE(24000,24);header.writeUInt32LE(48000,28);header.writeUInt16LE(2,32);
  header.writeUInt16LE(16,34);header.write('data',36);header.writeUInt32LE(pcm.length,40);
  const wav = Buffer.concat([header,pcm]);
  writeFileSync(plan.wav,wav,{mode:0o600});
  const spoken = await owner.audio.transcriptions.create({
    model:'qwen3-asr-0.6b',language:'en',file:new File([wav],'translated.wav',{type:'audio/wav'}),
  });
  assert.ok(recognizesMeaning(spoken.text), spoken.text);
  // The second tenant has a deliberately small output budget. Earlier audio
  // may already be on the wire; preserve the error before the abnormal close.
  const limited = await OpenAIRealtimeTranslationWS.create(client(plan.other), {model});
  const limitedEvents = [];
  let closeCode;
  limited.on('error', () => {});
  limited.on('event', event => limitedEvents.push(structuredClone(event)));
  limited.socket.on('close', code => { closeCode = code; });
  try {
    await waitFor(() => limitedEvents.some(e => e.type === 'session.created'));
    for (let at = 0; at < source.length; at += 9600)
      limited.send({type:'session.input_audio_buffer.append',
                    audio:source.subarray(at, at+9600).toString('base64')});
    limited.send({type:'session.input_audio_buffer.append',
                  audio:Buffer.alloc(48000).toString('base64')});
    await waitFor(() => closeCode !== undefined);
    assert.equal(limitedEvents.at(-1)?.type, 'error');
    assert.equal(limitedEvents.at(-1).error.code, 'tts_output_limit');
    assert.equal(closeCode, 1011);
    assert.ok(!limitedEvents.some(e => e.type === 'session.closed'));
  } finally {
    writeFileSync(plan.wire+'.limited', JSON.stringify(limitedEvents), {mode:0o600});
    limited.socket.terminate();
  }
  const chat=await client(plan.other).chat.completions.create({model:'qwen3.8-flash-next',
    temperature:0,enable_thinking:false,max_tokens:16,messages:[{role:'user',content:'Return only the number equal to 6 times 7.'}]});
  const independent=chat.choices[0].message.content.trim();
  assert.equal(independent,'42');
  process.stdout.write(JSON.stringify({sdk:version,input,english,spoken:spoken.text,
    wav_sha256:createHash('sha256').update(wav).digest('hex'),pcm_bytes:pcm.length,
    before_close:beforeClose,limited_error:limitedEvents.at(-1).error.code,limited_close:closeCode,grant_audiences:[401,401],independent_chat:independent,
    wire:events})+'\n');
} finally {
  writeFileSync(plan.wire,JSON.stringify(events),{mode:0o600});
  if (connection.socket.readyState!==WebSocket.CLOSED) connection.socket.terminate();
}
