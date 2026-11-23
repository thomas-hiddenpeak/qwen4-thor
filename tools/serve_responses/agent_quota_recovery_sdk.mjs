// Resume a retained failed turn without dispatching the child's work again.
import assert from 'node:assert/strict';
import { loadSdk } from '../serve_protocol/sdk.mjs';

let raw = '';
for await (const part of process.stdin) {
  raw += part;
  assert.ok(Buffer.byteLength(raw) <= 32768);
}
const p = JSON.parse(raw);
const endpoint = new URL(p.base);
assert.equal(endpoint.protocol, 'https:');
assert.equal(endpoint.hostname, '127.0.0.1');
const { OpenAI, version } = loadSdk(p.sdk);
assert.equal(version, '7.47.0');
const client = key => new OpenAI({baseURL:p.base, apiKey:key, timeout:60000, maxRetries:0});
const owner = client(p.key);
const api = owner.beta.agents.sessions;
const peer = client(p.other).beta.agents.sessions;
const id = p.session;
const params = {session_id:id, order:'asc'};
const childParams = {...params, subagent_id:p.child};
const text = items => items.filter(i => i.role === 'assistant')
  .flatMap(i => i.content).map(part => part.text || '').join('').trim();

async function verify() {
  const session = await api.retrieve(id);
  assert.equal(session.status, 'idle');
  assert.equal(session.error, null);
  const previous = (await api.turns.list(id, {order:'asc'})).data;
  assert.equal(previous.length, 4);
  const failed = previous.at(-1);
  assert.equal(failed.status, 'failed');
  assert.equal(failed.error.code, 'invalid_request');
  assert.match(failed.error.message, /cumulative client input limit/);
  await assert.rejects(peer.subagents.retrieve(p.child, {session_id:id}), e => e.status === 404);
  const children = (await api.subagents.turns.list(p.child, params)).data;
  assert.equal(children.length, 2);
  const retained = (await api.subagents.turns.items.list(children[1].id, childParams)).data;
  const expected = text(retained);
  assert.equal(expected, String(p.revision + 1));

  const stream = await api.events.stream(id);
  let terminal, idle = 0;
  try {
    await api.events.create(id, {events:[{type:'agent.session.input.message', input:[
      {role:'user', content:[{type:'input_text', text:'Your last wait_agent already returned the completed answer from root/reader. Read that existing tool result in the conversation and reply with its integer alone. No further tool calls are needed.'}]}]}]});
    for await (const event of stream) {
      if (event.type === 'agent.session.turn.failed') throw Error(JSON.stringify(event.turn.error));
      if (event.type === 'agent.session.turn.completed') terminal = event.turn;
      if (event.type === 'agent.session.idle' && terminal) { ++idle; break; }
    }
  } finally { stream.controller.abort(); }
  assert.equal(idle, 1);
  const latest = (await api.turns.list(id, {order:'asc'})).data;
  assert.deepEqual(latest.slice(0,-1), previous);
  assert.equal(latest.length, 5);
  assert.equal(terminal.id, latest.at(-1).id);
  const output = (await api.turns.items.list(terminal.id, params)).data;
  assert.equal(text(output), expected);
  assert.ok(!output.some(i => i.type.endsWith('_call')));
  assert.ok(terminal.usage.input_tokens > 0 && terminal.usage.input_tokens <= p.limit);
  assert.deepEqual((await api.subagents.turns.list(p.child, params)).data, children);
  assert.deepEqual((await api.subagents.turns.items.list(children[1].id, childParams)).data, retained);
  const chat = await owner.chat.completions.create({model:'qwen3.8-flash-next',temperature:0,
    messages:[{role:'user',content:'What is 6 times 7? Reply only with the decimal number.'}],
    reasoning_effort:'none', max_tokens:24});
  assert.equal(chat.choices[0].message.content.trim(),'42');
  console.log(JSON.stringify({sdk:version, original_turns:previous.length, original_error:failed.error,
    original_input:failed.usage.input_tokens, parent_answer:text(output), child_answer:expected,
    child_turns:children.length, new_tool_calls:0, actual_input:terminal.usage.input_tokens, limit:p.limit,
    idle, foreign:404, independent_chat:'42'}));
}
try {
  await verify();
} catch (error) {
  console.error("Agent quota recovery failed", error.status || "", error.code || "", String(error.message).slice(0,350));
  process.exitCode = 1;
}
