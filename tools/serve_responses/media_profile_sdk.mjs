// Exercise a stored image decision and its spoken result across both capacities.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const piece of process.stdin) {
  input += piece;
  assert.ok(Buffer.byteLength(input) <= 32768);
}
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const client = key => new OpenAI({baseURL:plan.base,apiKey:key,timeout:90000,maxRetries:0});
const owner = client(plan.key);
const peer = client(plan.other);
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const text = response => response.output.filter(x => x.type === 'message')
  .flatMap(x => x.content).filter(x => x.type === 'output_text').map(x => x.text).join('');
const image = readFileSync(plan.image);
assert.equal(sha(image), plan.sha256);
const primary = plan.phase === 'primary';
assert.ok(primary || plan.phase === 'light');
const policy = primary ? 'disabled' : 'enabled';
const imageUrl = 'data:image/png;base64,'+image.toString('base64');
const api = primary ? owner.responses : owner.beta.responses;
const format = {type:'json_schema',name:'observed_color',strict:true,schema:{type:'object',
  properties:{color:{type:'string',enum:['red','green','blue','yellow','black','white']}},
  required:['color'],additionalProperties:false}};
async function readSaved(source) {
  const stored = await owner.responses.retrieve(source.id);
  assert.equal(stored.status, 'completed');
  assert.equal(stored.image.context_compression, 'disabled');
  assert.deepEqual(JSON.parse(text(stored)), {color:'blue'});
  assert.equal(stored.usage.input_tokens, source.tokens);
  const bytes = Buffer.from(await (await owner.files.content(source.file)).arrayBuffer());
  assert.equal(sha(bytes), source.audio_sha256);
  await assert.rejects(peer.responses.retrieve(source.id), e => e.status === 404);
  await assert.rejects(peer.files.content(source.file), e => e.status === 404);
}
if (!primary) await readSaved(plan.previous.source);
const message = {role:'user',content:[{type:'input_image',detail:'auto',image_url:imageUrl},
  {type:'input_text',text:'What is the single solid color in this image? Answer with one English color word.'}]};
const body = {model:plan.model,input:[message], image:{context_compression:policy}};
const answer = await api.create({...body,text:{format},temperature:0,
  reasoning:{effort:'none'},max_output_tokens:96});
assert.equal(answer.status, 'completed');
assert.deepEqual(JSON.parse(text(answer)), {color:'blue'});
assert.deepEqual(answer.image, body.image);
const compact = await api.compact(body);
const original = compact.output.flatMap(x => Array.isArray(x.content) ? x.content : [])
  .find(x => x.type === 'input_image');
assert.equal(original?.image_url, imageUrl);
assert.equal(original.detail, 'auto');
const speech = await owner.audio.speech.create({model:plan.voice_model,voice:'vivian',
  input:JSON.parse(text(answer)).color,response_format:'opus'},primary ? {headers:{Accept:'audio/opus'}} : {});
const mime = speech.headers.get('content-type').split(';')[0];
assert.equal(mime, primary ? 'audio/opus' : 'application/octet-stream');
const audio = Buffer.from(await speech.arrayBuffer());
assert.ok(audio.length > 64 && audio.length <= 2 * 1024 * 1024);
assert.equal(audio.subarray(0,4).toString(),'OggS');
assert.ok(audio.includes(Buffer.from('OpusHead')));
const current = {id:answer.id,policy,tokens:answer.usage.input_tokens,
  color:JSON.parse(text(answer)).color,source_sha256:sha(image),mime,
  audio_bytes:audio.length,audio_sha256:sha(audio)};
let source = plan.previous?.source;
if (primary) {
  const file = await owner.files.create({purpose:'user_data',file:new File([audio],'spoken-color.opus')});
  source = {...current,file:file.id};
  await readSaved(source);
} else {
  assert.equal(source.tokens-current.tokens, 528);
  assert.notEqual(source.id,current.id);
  await assert.rejects(peer.beta.responses.retrieve(current.id), e => e.status === 404);
}
process.stdout.write(JSON.stringify({sdk:version,phase:plan.phase,source,current,peer:404}));
