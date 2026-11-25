// Exercise the same lossless source image through stable and beta SDK methods.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const piece of process.stdin) {
  input += piece;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const client = key => new OpenAI({baseURL:plan.base, apiKey:key, timeout:90000, maxRetries:0});
const owner = client(plan.key);
const peer = client(plan.other);
const png = readFileSync(plan.image);
const imageHash = createHash('sha256').update(png).digest('hex');
assert.equal(imageHash, plan.sha256);
const image = {type:'input_image', detail:'auto', image_url:'data:image/png;base64,'+png.toString('base64')};
const message = {role:'user', content:[image, {type:'input_text',text:'What is the single solid color in this image? Answer with one English color word.'}]};
const textOf = response => response.output.filter(i => i.type === 'message')
  .flatMap(i => i.content).filter(p => p.type === 'output_text').map(p => p.text).join('');
const rows = [];
for (const [family, api, policy] of [['stable', owner.responses, 'disabled'], ['beta', owner.beta.responses, 'enabled']]) {
  const format = {context_compression:policy};
  const body = {model:plan.model, input:[message], image:format};
  const answer = await api.create({...body, temperature:0, reasoning:{effort:'none'}, max_output_tokens:16});
  assert.equal(answer.status, 'completed');
  assert.deepEqual(answer.image, format);
  assert.match(textOf(answer).trim(), /^blue[.!]?$/i);
  const stored = await api.retrieve(answer.id);
  assert.equal(textOf(stored), textOf(answer));
  assert.deepEqual(stored.image, format);
  await assert.rejects(peer.responses.retrieve(answer.id), e => e.status === 404);
  const compact = await api.compact(body);
  assert.equal(compact.object, 'response.compaction');
  assert.deepEqual(compact.image, format);
  const source = compact.output.flatMap(i => Array.isArray(i.content) ? i.content : [])
    .find(part => part.type === 'input_image');
  assert.equal(source?.image_url, image.image_url);
  assert.equal(source?.detail, 'auto');
  rows.push({family, policy, id:answer.id, text:textOf(answer), tokens:answer.usage.input_tokens,
    compact:compact.id, source_sha256:imageHash, peer:404});
}
assert.equal(rows[0].tokens - rows[1].tokens, plan.expected_delta);
await assert.rejects(owner.responses.create({model:plan.model,input:'x',image:{context_compression:'invalid'}}),
  e => e.status === 400 && e.param === 'image.context_compression');
process.stdout.write(JSON.stringify({sdk:version,rows,expected_delta:plan.expected_delta}));
