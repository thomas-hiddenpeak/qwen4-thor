// Exercise all six child reads against the official SDK and a delegated task.
import assert from 'node:assert/strict';
import { loadSdk } from '../serve_protocol/sdk.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part;
  assert.ok(Buffer.byteLength(input) <= 8192);
}
const plan = JSON.parse(input);
const url = new URL(plan.base);
assert.equal(url.hostname, '127.0.0.1');
assert.equal(url.protocol, 'https:');
assert.ok(Number.isInteger(plan.revision));
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const sessions = key => new OpenAI({ baseURL: url.href, apiKey: key,
  maxRetries: 0, timeout: 90000 }).beta.agents.sessions;
const api = sessions(plan.key), peer = sessions(plan.other);
const missing = e => e.status === 404;
await assert.rejects(api.create({ environment: { type: 'openai_hosted' },
  agent: { model: 'qwen3.8-flash-next' }, stream: true }), e => e.status === 400 && e.param === 'input');
const task = 'Delegate this exact task to reader: Read the release record {"publication_revision":' +
  plan.revision + '}. Report only its publication_revision number. Wait for reader, then answer with the number.';
const initial = await api.create({
  agent: { model: 'qwen3.8-flash-next', multi_agent: { enabled: true, max_concurrent_subagents: 1 },
    instructions: 'Use the multi-agent functions for the explicit action requested. First delegate the release lookup to one child named reader using spawn_agent and wait_agent on root/reader. When asked to close or resume, call the corresponding close_subagent or resume_subagent function on root/reader and confirm. When asked to recall and calculate, use followup_task on the same reader and wait for its answer. Never spawn another child.',
    reasoning: { effort: 'none' } },
  environment: { type: 'none' }, stream: true, input: task,
});
const collect = async stream => {
  const events = [];
  try {
    for await (const event of stream) {
      events.push(event);
      if (event.type === 'agent.session.turn.failed') throw Error(JSON.stringify(event.turn.error));
      if (event.type === 'agent.session.idle') return events;
    }
    throw Error('Session stream ended before idle');
  } finally { stream.controller.abort(); }
};
const firstEvents = await collect(initial);
const id = firstEvents.find(e => e.type === 'agent.session.created').session.id;
const owned = { session_id: id };
const runTurn = async text => {
  const stream = await api.events.stream(id);
  try {
    await api.events.create(id, { events: [{ type: 'agent.session.input.message', input: [
      { role: 'user', content: [{ type: 'input_text', text }] }] }] });
    return await collect(stream);
  } finally { stream.controller.abort(); }
};
const firstSession = await api.retrieve(id);
await assert.rejects(peer.subagents.list(id), missing);
const listed = (await api.subagents.list(id, { order: 'asc', limit: 1 }));
assert.equal(listed.data.length, 1);
assert.equal(listed.has_more, false);
const child = listed.data[0];
assert.equal(child.parent_agent_id, firstSession.agent.id);
assert.equal(child.name, 'reader');
assert.equal(child.status, 'active');
assert.equal(child.closed_at, null);
assert.deepEqual(await api.subagents.retrieve(child.id, owned), child);
await assert.rejects(peer.subagents.retrieve(child.id, owned), missing);
const childTurns = api.subagents.turns;
const turns = (await childTurns.list(child.id, { ...owned, order: 'asc' })).data;
assert.equal(turns.length, 1);
const one = turns[0];
const binding = { ...owned, subagent_id: child.id };
assert.deepEqual(await childTurns.retrieve(one.id, binding), one);
assert.equal(one.agent_id, child.id);
assert.equal(one.subagent_id, child.id);
assert.equal(one.status, 'completed');
assert.ok(one.created_at <= one.started_at && one.started_at <= one.completed_at);
const items = (await childTurns.items.list(one.id, { ...binding, order: 'asc' })).data;
assert.deepEqual((await api.subagents.items.list(child.id, { ...owned, order: 'asc' })).data, items);
const answer = rows => rows.filter(i => i.role === 'assistant').flatMap(i => i.content)
  .map(part => part.text || '').join('').trim();
const firstAnswer = answer(items);
assert.ok(firstAnswer.match(/\d+/g)?.includes(String(plan.revision)), firstAnswer);
const parent = (await api.items.list(id, { order: 'asc' })).data;
const spawn = parent.find(x => x.type === 'create_subagent_call');
assert.equal(spawn?.agent_id, firstSession.agent.id);
assert.ok(parent.some(x => x.type === 'wait_for_subagents_call' && x.recipient_agent_ids.includes(child.id)));
assert.ok(answer(parent).match(/\d+/g)?.includes(String(plan.revision)));
await assert.rejects(peer.subagents.turns.items.list(one.id, binding), missing);
const transitions = [];
for (const [action, state] of [['close', 'closed'], ['resume', 'active']]) {
  const turn = await runTurn('Please ' + action + ' the existing subagent root/reader now using its ' +
    action + '_subagent function. Confirm when done.');
  const changed = await api.subagents.retrieve(child.id, owned);
  assert.equal(changed.opened_at, child.opened_at);
  assert.equal(changed.status, state);
  assert.equal(changed.parent_agent_id, child.parent_agent_id);
  assert.deepEqual(changed.instructions, child.instructions);
  if (state === 'closed') assert.ok(changed.closed_at >= changed.opened_at);
  else assert.equal(changed.closed_at, null);
  assert.deepEqual(turn.filter(e => e.type === 'agent.session.subagent.' + state).map(e => e.subagent), [changed]);
  const ops = turn.filter(e => e.type === 'agent.session.turn.item.done' && e.item.type === action + '_subagent_call');
  assert.equal(ops.length, 1);
  assert.equal(ops[0].item.sender_agent_id, changed.parent_agent_id);
  assert.equal(ops[0].item.recipient_agent_id, child.id);
  assert.equal(ops[0].item.status, 'completed');
  assert.deepEqual((await childTurns.list(child.id, owned)).data, [one]);
  assert.deepEqual((await childTurns.items.list(one.id, { ...binding, order: 'asc' })).data, items);
  await assert.rejects(peer.subagents.retrieve(child.id, owned), missing);
  transitions.push({state, item: ops[0].item.id, opened_at: changed.opened_at, closed_at: changed.closed_at});
}
const secondEvents = await runTurn('Give the existing reader a follow-up task: recall the publication revision you read, add one, and reply with the resulting integer. Wait and give its answer.');
const nextPage = await childTurns.list(child.id, { ...owned, order: 'asc', after: one.id, limit: 1 });
assert.equal(nextPage.data.length, 1);
const two = nextPage.data[0];
assert.notEqual(two.id, one.id);
const secondAnswer = answer((await childTurns.items.list(two.id, { ...binding, order: 'asc' })).data);
assert.ok(secondAnswer.match(/\d+/g)?.includes(String(plan.revision + 1)), secondAnswer);
assert.deepEqual((await api.subagents.list(id)).data.map(s => s.id), [child.id]);
await assert.rejects(api.subagents.turns.retrieve('turn_unknown', binding), missing);
const lifecycle = (events, turn, expected) => {
  assert.equal(new Set(events.map(e => e.event_id)).size, events.length);
  const own = events.filter(e => e.turn_id === turn.id);
  const edges = own.filter(e => e.turn).map(e => e.turn.status);
  assert.deepEqual(edges, ['queued', 'in_progress', 'completed']);
  assert.deepEqual(own.find(e => e.type === 'agent.session.turn.completed').turn, turn);
  const deltas = own.filter(e => e.type === 'agent.session.turn.output_text.delta');
  assert.ok(deltas.length && deltas.every(e => e.output_index === 0));
  assert.equal(deltas.map(e => e.delta).join('').trim(), expected);
  const item = own.find(e => e.type === 'agent.session.turn.item.done').item;
  assert.equal(item.turn_id, turn.id);
  return { edges, deltas: deltas.length, item: item.id, outputIndex: 0 };
};
assert.deepEqual(firstEvents.filter(e => e.type === 'agent.session.subagent.created').map(e => e.subagent), [child]);
assert.equal(secondEvents.filter(e => e.type === 'agent.session.subagent.created').length, 0);
const childEvents = [lifecycle(firstEvents, one, firstAnswer), lifecycle(secondEvents, two, secondAnswer)];
process.stdout.write(JSON.stringify({session: id, child: child.id, turns: [one.id,two.id],
  firstAnswer, secondAnswer, revision: plan.revision, parentAnswer: answer(parent),
  transitions, childEvents, invalidEmptyHostedStream: 400, sdk: version, foreign: 404, queryMethods: 6})+'\n');
