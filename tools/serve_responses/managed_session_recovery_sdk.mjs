// The primary leaves a release lookup behind. A new process reads its actual turn and resumes it.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const release = JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-chatkit-widget-stream-20261126/delivery.json', import.meta.url)));
const revision = String(release.publication.publication_revision);
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const client = apiKey => new OpenAI({baseURL:plan.base, apiKey, timeout:60000, maxRetries:0});
const sessions = client(plan.key).beta.agents.sessions;
const peer = client(plan.other);
const missing = e => e instanceof OpenAI.APIError && e.status === 404;
const primary = plan.phase === 'primary';
assert.ok(primary || plan.phase === 'light');

const schema = {type:'object', properties:{value:{type:'integer'}}, required:['value'], additionalProperties:false};
const fmt = {type:'json_schema', schema};
function checkValue(wire, expected) {
  const value = JSON.parse(wire);
  assert.deepEqual(value, {value:Number(expected)});
  return String(value.value);
}
const historySession = await sessions.retrieve(plan.failed.session);
assert.equal(historySession.status, 'idle');
const historicalTurn = await sessions.turns.retrieve(plan.failed.turn, {session_id:historySession.id});
assert.equal(historicalTurn.status, 'failed');
assert.equal(historicalTurn.error.message, 'incomplete refusal frame');
const failed = await client(plan.key).responses.retrieve(plan.failed.response);
assert.equal(failed.status, 'failed');
assert.equal(failed.error.code, 'response_parse_failed');
await assert.rejects(peer.responses.retrieve(failed.id), missing);

async function receive(stream, padded, expected) {
  let session, text = '', deltas = 0, count = 0;
  try {
    for await (const event of stream) {
      assert.ok(++count <= 256);
      assert.ok(!['error', 'agent.session.turn.failed'].includes(event.type), event.type);
      const delta = event.type.endsWith('.delta');
      assert.equal(Object.hasOwn(event, 'obfuscation'), padded && delta);
      if (delta) {
        ++deltas;
        if (padded) assert.ok(typeof event.obfuscation === 'string' && event.obfuscation.length);
      }
      if (event.type === 'agent.session.created') session = event.session;
      if (event.type === 'agent.session.turn.output_text.delta') text += event.delta;
      if (event.type === 'agent.session.idle') {
        assert.ok(deltas > 0);
        checkValue(text, expected);
        return {session, deltas};
      }
    }
    throw Error('Session stream ended before idle');
  } finally { stream.controller.abort(); }
}

let id, deltas;
if (primary) {
  const stream = await sessions.create({environment:{type:'none'}, agent:{model:plan.model,
    reasoning:{effort:'none'}, instructions:'Use the value field for the requested integer.',text:{format:fmt}},
    input:'The issued release record gives revision '+revision+'. Return the revision number.',
    stream:true, stream_options:{include_obfuscation:false}, prompt_cache_options:{ttl:'12h'}});
  const result = await receive(stream, false, revision);
  assert.equal(result.session.prompt_cache_options.ttl, '12h');
  id = result.session.id;
  deltas = result.deltas;
} else {
  id = plan.previous.source.session;
}
const saved = await sessions.retrieve(id);
assert.equal(saved.prompt_cache_options.ttl, '12h');
assert.ok(!Object.hasOwn(saved, 'stream_options'));
await assert.rejects(peer.beta.agents.sessions.retrieve(id), missing);
const turns = (await sessions.turns.list(id, {order:'asc'})).data;
const answers = async () => (await sessions.items.list(id, {order:'asc'})).data
  .filter(i => i.role === 'assistant').map(i => ({id:i.id,
    text:i.content.filter(c => c.type === 'output_text').map(c => c.text).join('').trim()}));
const original = await answers();
assert.equal(turns.length, 1);
assert.equal(turns[0].status, 'completed');
assert.equal(original.length, 1);
checkValue(original[0].text, revision);
const source = {session:id, turn:turns[0].id, item:original[0].id};
let resumed;
if (!primary) {
  assert.deepEqual(source, plan.previous.source);
  assert.equal((await sessions.update(id, {prompt_cache_options:{}})).prompt_cache_options.ttl, '30m');
  const stream = await sessions.events.stream(id);
  try {
    await sessions.events.create(id, {events:[{type:'agent.session.input.message',input:[
      {role:'user', content:[{type:'input_text',text:'What is 6 times 7?'}]}]}]});
    deltas = (await receive(stream, true, '42')).deltas;
  } finally { stream.controller.abort(); }
  const updated = (await sessions.turns.list(id, {order:'asc'})).data;
  assert.equal(updated.length, 2);
  assert.equal(updated[0].id, source.turn);
  assert.equal(updated[1].status, 'completed');
  const history = await answers();
  assert.equal(history.length, 2);
  assert.deepEqual(history[0], original[0]);
  checkValue(history[1].text, '42');
  assert.equal((await sessions.update(id, {prompt_cache_options:null})).prompt_cache_options ?? null, null);
  assert.equal((await sessions.retrieve(id)).prompt_cache_options ?? null, null);
  resumed = {turn:updated[1].id, item:history[1].id, answer:checkValue(history[1].text, '42'), ttl:'30m', cleared:true};
}
const chat = await peer.chat.completions.create({model:plan.model, temperature:0, max_tokens:24,
  reasoning_effort:'none', messages:[{role:'user',content:'Compute 17 + 25. Return only the integer.'}]});
process.stdout.write(JSON.stringify({phase:plan.phase, sdk:version, revision, source, historical:plan.failed, format:fmt, restored_ttl:'12h',
  peer:404, padded:!primary, deltas, chat:chat.choices[0].message.content.trim(), ...(resumed && {resumed})}));
