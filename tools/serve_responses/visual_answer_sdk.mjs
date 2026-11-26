// Check the public content part selected by the model on ordinary visual input.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
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
const common = {model:plan.model, temperature:0, reasoning:{effort:'none'}, max_output_tokens:96};
const parts = response => response.output.filter(item => item.type === 'message').flatMap(item => item.content);
function message(fixture, question) {
  const bytes = readFileSync(fixture.path);
  assert.equal(createHash('sha256').update(bytes).digest('hex'), fixture.sha256);
  return {role:'user', content:[{type:'input_image', detail:'auto',
    image_url:'data:image/png;base64,'+bytes.toString('base64')}, {type:'input_text', text:question}]};
}
const restored = [];
if (plan.previous) {
  for (const prior of [...plan.previous.observations, plan.previous.refusal]) {
    const saved = await owner.responses.retrieve(prior.id);
    assert.equal(saved.id, prior.id);
    assert.deepEqual(parts(saved), prior.content);
    await assert.rejects(peer.responses.retrieve(prior.id), error => error.status === 404);
    restored.push({id:saved.id, content:parts(saved), peer:404});
  }
}
const observations = [];
const question = 'What is the single solid color in this image? Answer with one English color word.';
for (const [index, fixture] of plan.images.entries()) {
  const body = {...common, user:null, safety_identifier:'bounded-wire-client', input:[message(fixture, question)],
    image:{context_compression:index === 0 ? 'disabled' : 'enabled'}};
  let response;
  let streamedText = '';
  let refusalDelta = false;
  if (index === 0) {
    response = await owner.responses.create(body);
  } else {
    const stream = await owner.beta.responses.create({...body, stream:true});
    for await (const event of stream) {
      if (event.type === 'response.output_text.delta') streamedText += event.delta;
      if (event.type === 'response.refusal.delta') refusalDelta = true;
      if (event.type === 'response.completed' || event.type === 'response.incomplete') response = event.response;
    }
  }
  assert.ok(response);
  assert.ok(response.user == null);
  assert.equal(response.safety_identifier, 'bounded-wire-client');
  const types = parts(response);
  const stored = await owner.responses.retrieve(response.id);
  assert.deepEqual(parts(stored), types);
  assert.ok(stored.user == null);
  assert.equal(stored.safety_identifier, response.safety_identifier);
  await assert.rejects(peer.responses.retrieve(response.id), error => error.status === 404);
  const text = types.filter(p => p.type === 'output_text').map(p => p.text).join('');
  const row = {id:response.id, expected:fixture.color, image_sha256:fixture.sha256,
    content:types, status:response.status, tokens:response.usage, peer:404,
    streamed:index !== 0, streamed_text:streamedText, refusal_delta:refusalDelta,
    passed:response.status === 'completed' && types.length === 1 && types[0].type === 'output_text' &&
      text.trim().toLowerCase().replace(/[.!]$/, '') === fixture.color &&
      (index === 0 || (streamedText === text && !refusalDelta))};
  observations.push(row);
  process.stderr.write(JSON.stringify(row)+'\n');
}
const invalid = [];
for (const resource of [owner.responses, owner.beta.responses]) {
  await assert.rejects(resource.create({...common,input:'Hello',user:42,prompt:{id:'prompt_missing'}}),
    error => {
      invalid.push({status:error.status,param:error.error?.param});
      return error.status === 400 && error.error?.param === 'user';
    });
}
const declined = await owner.responses.create({...common, max_output_tokens:192, instructions:plan.policy,
  input:[message(plan.images[0], plan.private_question)]});
const declineParts = parts(declined);
const refusal = {id:declined.id, status:declined.status, content:declineParts,
  passed:declined.status === 'completed' && declineParts.length === 1 && declineParts[0].type === 'refusal' &&
    declineParts[0].refusal.trim().length > 0 && !declineParts[0].refusal.includes(plan.marker)};
process.stderr.write(JSON.stringify(refusal)+'\n');
const chat = await owner.chat.completions.create({model:plan.model, temperature:0, enable_thinking:false,
  max_completion_tokens:24, messages:[{role:'user',content:'Compute 17 plus 25. Reply only with the number.'}]});
const answer = chat.choices[0].message;
process.stdout.write(JSON.stringify({sdk:version, restored, observations, refusal, invalid, null_user_identity:true, chat:answer.content,
  passed:observations.every(row => row.passed) && refusal.passed && answer.content.trim() === '42' && !answer.refusal}));
