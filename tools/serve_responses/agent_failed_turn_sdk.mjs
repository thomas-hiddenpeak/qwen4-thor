// Reopen the actual failed release lookup and submit a new turn on the same session.
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
const client = apiKey => new OpenAI({apiKey, baseURL:plan.base, timeout:60000, maxRetries:0});
const owner = client(plan.key);
const sessions = owner.beta.agents.sessions;
const id = plan.session;
const missing = e => e instanceof OpenAI.APIError && e.status === 404;
await assert.rejects(client(plan.other).beta.agents.sessions.retrieve(id), missing);
const original = await sessions.retrieve(id);
assert.equal(original.status, 'idle');
assert.equal(original.error, null);
assert.equal(original.prompt_cache_options.ttl, '12h');
const oldTurn = await sessions.turns.retrieve(plan.turn, {session_id:id});
assert.equal(oldTurn.status, 'failed');
assert.equal(oldTurn.error.code, 'server_error');
assert.equal(oldTurn.error.message, 'incomplete refusal frame');
const failed = await owner.responses.retrieve(plan.failed_response);
assert.equal(failed.status, 'failed');
assert.equal(failed.error.code, 'response_parse_failed');
assert.ok(failed.output.some(i => i.content?.some(p => p.type === 'refusal')));
const stream = await sessions.events.stream(id);
let answer = '', next, deltas = 0, idle = false, count = 0;
try {
  const posted = await sessions.events.create(id, {events:[{type:'agent.session.input.message',
    input:[{role:'user',content:[{type:'input_text',text:'What is 6 times 7? Return the integer.'}]}]}]}).asResponse();
  assert.equal(posted.status, 202);
  for await (const event of stream) {
    assert.ok(++count <= 256);
    assert.ok(!['error','agent.session.turn.failed','agent.session.failed'].includes(event.type), event.type);
    if (event.type === 'agent.session.turn.output_text.delta') {
      assert.ok(event.obfuscation);
      answer += event.delta;
      ++deltas;
    }
    if (event.type === 'agent.session.turn.completed') next = event.turn.id;
    if (event.type === 'agent.session.idle') { idle = true; break; }
  }
} finally { stream.controller.abort(); }
assert.ok(idle && deltas > 0 && next && next !== plan.turn);
assert.equal(answer.trim(), '42');
const turns = (await sessions.turns.list(id, {order:'asc'})).data;
assert.deepEqual(turns.map(t => [t.id,t.status]), [[plan.turn,'failed'],[next,'completed']]);
assert.equal((await sessions.retrieve(id)).status, 'idle');
const chat = await client(plan.other).chat.completions.create({model:'qwen3.8-flash-next',
  temperature:0, max_tokens:24, reasoning_effort:'none',
  messages:[{role:'user',content:'Return only the answer to 17 plus 25.'}]});
assert.equal(chat.choices[0].message.content.trim(), '42');
process.stdout.write(JSON.stringify({session:id, old_turn:plan.turn, next_turn:next, original_error:failed.error.code,
  failed_response:failed.id, turn_error:oldTurn.error.code, original_retained:true, initial_state:original.status,
  final_state:'idle', deltas, answer:answer.trim(), peer:404, independent_chat:'42'}));
