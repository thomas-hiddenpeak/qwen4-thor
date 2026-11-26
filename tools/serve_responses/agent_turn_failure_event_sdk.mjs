// The bounded fixture assigns this client a single model request per minute.
import assert from 'node:assert/strict';
import {setTimeout as delay} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.equal(base.protocol, 'https:');
assert.equal(base.hostname, '127.0.0.1');
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const sessions = key => new OpenAI({apiKey:key, baseURL:base.href, maxRetries:0,
  timeout:60000}).beta.agents.sessions;
const api = sessions(plan.key);
const model = 'qwen3.8-flash-next';

async function terminal(stream) {
  const events = [];
  try {
    for await (const event of stream) {
      assert.notEqual(event.type, 'error');
      assert.ok(events.length < 256);
      events.push(event);
      if (event.type === 'agent.session.idle' || event.type === 'agent.session.failed')
        return events;
    }
    throw Error('subscription ended before session terminal');
  } finally {
    stream.controller.abort();
  }
}

const initial = await terminal(await api.create({agent:{model, reasoning:{effort:'none'},
  instructions:'Answer arithmetic questions with the decimal integer only.'},
  environment:{type:'none'}, input:'What is 1 plus 1?', stream:true}));
const session = initial.find(e => e.type === 'agent.session.created').session.id;
const answer = events => events.filter(e => e.type === 'agent.session.turn.output_text.delta')
  .map(e => e.delta).join('').trim();
assert.equal(answer(initial),'2');
const unavailable = error => error instanceof OpenAI.APIError && error.status === 404;
await assert.rejects(sessions(plan.other).retrieve(session), unavailable);

const stream = await api.events.stream(session);
let live;
try {
  const post = await api.events.create(session, {events:[{type:'agent.session.input.message',
    input:[{role:'user',content:[{type:'input_text',text:'What is 2 plus 2?'}]}]}]}).asResponse();
  assert.equal(post.status,202);
  live = await terminal(stream);
} finally {
  stream.controller.abort();
}
const [failure] = live.filter(e => e.type === 'agent.session.turn.failed');
assert.ok(failure && live.at(-1).type === 'agent.session.idle');
const rid = failure.turn.id;
const retained = await api.turns.retrieve(rid,{session_id:session});
assert.equal(retained.status,'failed');
assert.equal(retained.error.code,'rate_limit_exceeded');
assert.deepEqual(failure.turn.error,retained.error);
assert.equal((await api.retrieve(session)).status,'idle');
await assert.rejects(sessions(plan.other).turns.retrieve(rid,{session_id:session}), unavailable);

await delay(60050);
const later = await api.stream(session,{input:'What is 6 times 7? Return the number only.'}).finalResult();
assert.equal(later.output_text.trim(),'42');
const turns = (await api.turns.list(session,{order:'asc'})).data;
assert.deepEqual(turns.map(t=>t.status),['completed','failed','completed']);
assert.equal(turns[1].id,rid);
assert.deepEqual(turns[1].error,retained.error);
console.log(JSON.stringify({sdk:version,session,failed_turn:rid,first_answer:'2',
  submitted:202,event_error:failure.turn.error,read_error:retained.error,
  events:live.map(e=>({type:e.type,id:e.event_id})),session_status:'idle',
  final_answer:'42',turn_statuses:turns.map(t=>t.status),foreign:404}));
