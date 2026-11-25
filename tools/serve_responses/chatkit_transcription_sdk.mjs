// Exercise the ChatKit composer with browser audio over an actual HTTPS session.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const client = apiKey => new OpenAI({baseURL: plan.base, apiKey, maxRetries: 0});
const owner = client(plan.key);
const peer = client(plan.other);
const endpoint = new URL(plan.base + '/chatkit');
assert.equal(endpoint.protocol, 'https:');
const workflow = await owner.beta.agents.create({model: plan.model, instructions: 'Answer briefly.'});
const session = await owner.beta.chatkit.sessions.create({user: 'dictation-reader',
  workflow: {id: workflow.id}, rate_limits: {max_requests_per_1_minute: 40}});
const observations = [];
async function dictate(token, audio, mime, status) {
  const response = await fetch(endpoint, {method: 'POST', redirect: 'error',
    headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
    body: JSON.stringify({type: 'input.transcribe', params: {audio_base64: audio, mime_type: mime}})});
  assert.equal(response.url, endpoint.href);
  assert.equal(response.status, status);
  assert.match(response.headers.get('content-type'), /^application\/json\b/i);
  const body = await response.json();
  if (status === 200) {
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.deepEqual(Object.keys(body), ['text']);
    assert.equal(typeof body.text, 'string');
    assert.ok(body.text.trim());
  } else {
    assert.ok(body.error.code);
  }
  observations.push({method: 'POST', path: endpoint.pathname, status, mime,
    body_sha256: createHash('sha256').update(JSON.stringify(body)).digest('hex')});
  return body;
}
const clips = Object.fromEntries(['wav', 'webm'].map(format => {
  const bytes = readFileSync(plan[format]);
  return [format, {data: bytes.toString('base64'), sha256: createHash('sha256').update(bytes).digest('hex')}];
}));
await dictate(session.client_secret, 'a===', 'audio/wav', 400);
await dictate(session.client_secret, clips.wav.data, 'audio/webm', 415);
await dictate(plan.other, clips.wav.data, 'audio/wav', 401);
const wav = await dictate(session.client_secret, clips.wav.data, 'audio/wav', 200);
const webm = await dictate(session.client_secret, clips.webm.data, 'audio/webm;codecs=opus', 200);
await owner.beta.chatkit.sessions.cancel(session.id);
await dictate(session.client_secret, clips.wav.data, 'audio/wav', 401);
const chat = await peer.chat.completions.create({model: plan.model, temperature: 0, max_tokens: 32,
  reasoning_effort: 'none', messages: [{role: 'user', content: 'Return just the integer product of 6 and 7.'}]});
const independent = chat.choices[0].message.content.trim();
assert.equal(independent, '42');
// A recovered profile must read the original cancelled Eval, including its owner
// and terminal result counts. Only resource IDs and a digest cross the handoff.
async function canceledRun(previous) {
  let evalId = previous?.eval_id;
  let runId = previous?.run_id;
  if (!previous) {
    const evaluation = await owner.evals.create({name: 'Dictation service cancellation',
      data_source_config: {type: 'custom', item_schema: {type: 'object',
        properties: {question: {type: 'string'}}, required: ['question']}},
      testing_criteria: [{name: 'Arithmetic', type: 'string_check',
        input: '{{sample.output_text}}', operation: 'eq', reference: '42'}]});
    evalId = evaluation.id;
    const item = {question: 'Return just the integer product of 6 and 7.'};
    const run = await owner.evals.runs.create(evalId, {data_source: {type: 'responses',
      model: plan.model, input_messages: {type: 'template',
        template: [{role: 'user', content: '{{item.question}}'}]},
      source: {type: 'file_content', content: Array.from({length: 12}, () => ({item}))}}});
    runId = run.id;
    const terminal = await owner.evals.runs.cancel(runId, {eval_id: evalId});
    assert.equal(terminal.status, 'canceled');
  }
  const stored = await owner.evals.runs.retrieve(runId, {eval_id: evalId});
  assert.equal(stored.status, 'canceled');
  assert.equal(stored.id, runId);
  assert.equal(stored.eval_id, evalId);
  await assert.rejects(peer.evals.runs.retrieve(runId, {eval_id: evalId}), err => err.status === 404);
  const record = {eval_id: evalId, run_id: runId, status: stored.status, foreign_status: 404,
    sha256: createHash('sha256').update(JSON.stringify(stored)).digest('hex')};
  if (previous) assert.deepEqual(record, previous);
  return record;
}
const handoff = plan.handoff ? {canceled_run: await canceledRun(plan.previous)} : {};
process.stdout.write(JSON.stringify({...handoff, sdk: version, transcripts: {wav: wav.text, webm: webm.text},
  inputs: {wav: clips.wav.sha256, webm: clips.webm.sha256}, observations, independent}));
