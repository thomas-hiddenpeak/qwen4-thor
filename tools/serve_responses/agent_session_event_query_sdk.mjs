// Follow one real turn through two independent subscriptions on native TLS.
import assert from 'node:assert/strict';
import { loadSdk } from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const url = new URL(plan.base);
assert.equal(url.protocol, 'https:');
assert.equal(url.hostname, '127.0.0.1');
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const client = key => new OpenAI({ baseURL: url.href, apiKey: key,
  maxRetries: 0, timeout: 60000 });
const owner = client(plan.key);
const api = owner.beta.agents.sessions;
const peer = client(plan.other).beta.agents.sessions;
const model = 'qwen3.8-flash-next';

async function collect(stream, padding) {
  const events = [];
  try {
    for await (const event of stream) {
      assert.ok(events.length < 256);
      assert.ok(!['error', 'agent.session.turn.failed'].includes(event.type), event.type);
      assert.equal(Object.hasOwn(event, 'obfuscation'), padding && event.type.endsWith('.delta'));
      if (Object.hasOwn(event, 'obfuscation')) {
        assert.equal(typeof event.obfuscation, 'string');
        assert.ok(event.obfuscation.length > 0);
      }
      const { obfuscation, ...payload } = event;
      events.push(payload);
      if (event.type === 'agent.session.idle') return events;
    }
    throw Error('Session subscription ended before idle');
  } finally {
    stream.controller.abort();
  }
}

const initial = await api.create({ agent: { model, reasoning: { effort: 'none' },
    instructions: 'Answer arithmetic questions with only the decimal integer.' },
  environment: { type: 'none' }, stream: true, input: 'What is 1 + 1?' });
const opened = await collect(initial, true);
const id = opened.find(e => e.type === 'agent.session.created').session.id;
const text = events => events.filter(e => e.type === 'agent.session.turn.output_text.delta')
  .map(e => e.delta).join('').trim();
assert.equal(text(opened), '2');
const expected = status => error => error instanceof OpenAI.APIError && error.status === status;
await assert.rejects(peer.events.stream(id, { query: { include_obfuscation: false } }), expected(404));
for (const query of ['include_obfuscation=null',
                     'include_obfuscation=true&include_obfuscation=false', 'limit=1']) {
  const denied = await fetch(url.href+'/agents/sessions/'+id+'/events?'+query, {
    headers: { Authorization: 'Bearer '+plan.key, 'OpenAI-Beta':'agents=v1' },
    signal: AbortSignal.timeout(5000) });
  assert.equal(denied.status, 400);
  assert.ok((await denied.json()).error);
}
const streams = [];
let masked, plain;
try {
  streams.push(await api.events.stream(id));
  streams.push(await api.events.stream(id, { query: { include_obfuscation: false } }));
  const accepted = await api.events.create(id, { events: [{ type:'agent.session.input.message',
    input:[{ role:'user', content:[{type:'input_text', text:'What is 6 times 7?'}] }] }] }).asResponse();
  assert.equal(accepted.status, 202);
  [masked, plain] = await Promise.all([collect(streams[0], true), collect(streams[1], false)]);
} finally {
  for (const stream of streams) stream.controller.abort();
}
assert.deepEqual(masked, plain);
assert.equal(new Set(masked.map(e => e.event_id)).size, masked.length);
assert.equal(text(plain), '42');
const items = (await api.items.list(id, {order:'asc'})).data;
assert.ok(items.some(item => item.role === 'assistant' &&
  item.content?.some(part => part.type === 'output_text' && part.text.trim() === '42')));

const response = await owner.responses.create({ model, input:'Return only the number 42.',
  reasoning:{effort:'none'}, store:true, top_logprobs:1, include:['message.output_text.logprobs'], max_output_tokens:32 });
assert.equal(response.output_text.trim(), '42');
const field = 'message.output_text.logprobs';
const bracket = await owner.responses.retrieve(response.id, {include:[field]});
const canonical = await owner.get('/responses/'+response.id+'?include='+encodeURIComponent(field));
assert.deepEqual(bracket.output, canonical.output);
assert.deepEqual(bracket.usage, canonical.usage);
assert.ok(bracket.output.some(item => item.content?.some(part => Array.isArray(part.logprobs))));
await api.delete(id);
const chat = await owner.chat.completions.create({model, temperature:0, reasoning_effort:'none', max_tokens:24,
  messages:[{role:'user', content:'Reply with just the number: 6 times 7.'}]});
const answer = chat.choices[0].message.content.trim();
assert.equal(answer, '42');
process.stdout.write(JSON.stringify({ sdk: version, events: masked.length,
  deltas: masked.filter(e => e.type.endsWith('.delta')).length, text: text(plain),
  initial: text(opened), identical_event_ids_and_payloads: true, foreign:404,
  rejected_query_statuses:[400,400,400], include_projections_equal:true, independent_chat:answer })+'\n');
