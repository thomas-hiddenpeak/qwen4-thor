// Exercise connection-local event padding and durable per-session cache defaults.
import assert from 'node:assert/strict';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const url = new URL(plan.base);
assert.ok(url.protocol === 'https:' && url.hostname === '127.0.0.1');
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const client = apiKey => new OpenAI({baseURL:url.href, apiKey, timeout:60000, maxRetries:0});
const owner = client(plan.key);
const api = owner.beta.agents.sessions;
const peer = client(plan.other).beta.agents.sessions;
const model = 'qwen3.8-flash-next';
const create = {environment:{type:'none'}, agent:{model, reasoning:{effort:'none'},
  instructions:'Answer arithmetic questions with only the decimal integer.'},
  input:'What is 6 times 7?'};
const status = n => error => error instanceof OpenAI.APIError && error.status === n;
for (const patch of [{stream_options:{}}, {stream:false, stream_options:{}},
  {stream:true, stream_options:{include_obfuscation:null}}, {prompt_cache_options:{ttl:'24h'}}])
  await assert.rejects(api.create({...create, ...patch}), status(400));

async function complete(stream, padded) {
  let id, ttl, events = 0, deltas = 0, text = '';
  try {
    for await (const event of stream) {
      assert.ok(++events <= 256);
      assert.ok(!['error','agent.session.turn.failed'].includes(event.type), event.type);
      const delta = event.type.endsWith('.delta');
      assert.equal(Object.hasOwn(event, 'obfuscation'), padded && delta);
      if (delta) ++deltas;
      if (padded && delta) assert.ok(typeof event.obfuscation === 'string' && event.obfuscation.length);
      if (event.type === 'agent.session.created') {
        id = event.session.id;
        ttl = event.session.prompt_cache_options?.ttl;
        assert.ok(!Object.hasOwn(event.session, 'stream_options'));
      }
      if (event.type === 'agent.session.turn.output_text.delta') text += event.delta;
      if (event.type === 'agent.session.idle') {
        assert.ok(deltas > 0);
        assert.equal(text.trim(), '42');
        return {id, ttl, events, deltas};
      }
    }
    throw Error('Stream ended before idle');
  } finally { stream.controller.abort(); }
}

const plain = await complete(await api.create({...create, stream:true,
  stream_options:{include_obfuscation:false}, prompt_cache_options:{ttl:'12h'}}), false);
assert.equal(plain.ttl, '12h');
const id = plain.id;
assert.equal((await api.retrieve(id)).prompt_cache_options.ttl, '12h');
assert.equal((await api.update(id, {metadata:{purpose:'records'}})).prompt_cache_options.ttl, '12h');
await assert.rejects(peer.update(id, {prompt_cache_options:null}), status(404));
await assert.rejects(api.update(id, {prompt_cache_options:{ttl:3600}}), status(400));
assert.equal((await api.update(id, {prompt_cache_options:{}})).prompt_cache_options.ttl, '30m');
async function followingTurn(question) {
  const stream = await api.events.stream(id);
  try {
    await api.events.create(id, {events:[{type:'agent.session.input.message',
      input:[{role:'user', content:[{type:'input_text', text:question}]}]}]});
    return await complete(stream, true);
  } finally { stream.controller.abort(); }
}
await followingTurn('What is 21 plus 21?');
assert.equal((await api.update(id, {prompt_cache_options:null})).prompt_cache_options ?? null, null);
assert.equal((await api.retrieve(id)).prompt_cache_options ?? null, null);

await followingTurn('What is 40 plus 2?');
const masked = await complete(await api.create({...create, stream:true}), true);
assert.equal(masked.ttl, undefined);
await api.delete(masked.id);
// Retain the three linked Responses in the isolated store for the disk oracle.
const chat = await owner.chat.completions.create({model, temperature:0, reasoning_effort:'none',
  max_tokens:24, messages:[{role:'user',content:'What is six times seven? Reply only with the integer.'}]});
assert.equal(chat.choices[0].message.content.trim(), '42');
console.log(JSON.stringify({independent_chat:'42', plain_create_deltas:plain.deltas,
  masked_create_deltas:masked.deltas, fresh_subscription_default_padding:true,
  session_id:id, cached_initial_ttl:plain.ttl, updated_ttl:'30m', cleared:true,
  rejected_invalid:true, cross_client_status:404}));
