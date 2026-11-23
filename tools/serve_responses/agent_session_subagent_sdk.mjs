// Exercise all six child reads against the official SDK and a delegated task.
import assert from 'node:assert/strict';
import { setTimeout as delay } from 'node:timers/promises';
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
const id = (await api.create({
  agent: { model: 'qwen3.8-flash-next', multi_agent: { enabled: true, max_concurrent_subagents: 1 },
    instructions: 'Use the multi-agent tools. For a new task delegate it to one child named reader with spawn_agent, then call wait_agent on root/reader and report its answer. For the second user message use followup_task on the existing reader and wait for its answer. Never spawn a second child.',
    reasoning: { effort: 'none' } },
  environment: { type: 'none' },
  input: 'Delegate this exact task to reader: Read the release record {"publication_revision":' + plan.revision +
    '}. Report only its publication_revision number. Wait for reader, then answer with the number.',
})).id;
const owned = { session_id: id };
const waitIdle = async () => {
  const deadline = Date.now() + 85000;
  do {
    const s = await api.retrieve(id);
    if (s.status === 'idle') return s;
    assert.equal(s.status, 'in_progress', JSON.stringify(s.error));
    await delay(250);
  } while (Date.now() < deadline);
  throw Error('Managed delegation exceeded its turn deadline');
};
const firstSession = await waitIdle();
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
const stream = await api.events.stream(id);
const streamTypes = [];
try {
  await api.events.create(id, { events: [{ type: 'agent.session.input.message', input: [
    { role: 'user', content: [{ type: 'input_text', text: 'Give the existing reader a follow-up task: recall the publication revision you read, add one, and reply with the resulting integer. Wait and give its answer.' }] }] }] });
  for await (const e of stream) {
    streamTypes.push(e.type);
    if (e.type === 'agent.session.turn.failed') throw Error(JSON.stringify(e.turn.error));
    if (e.type === 'agent.session.idle') break;
  }
} finally { stream.controller.abort(); }
const nextPage = await childTurns.list(child.id, { ...owned, order: 'asc', after: one.id, limit: 1 });
assert.equal(nextPage.data.length, 1);
const two = nextPage.data[0];
assert.notEqual(two.id, one.id);
const secondAnswer = answer((await childTurns.items.list(two.id, { ...binding, order: 'asc' })).data);
assert.ok(secondAnswer.match(/\d+/g)?.includes(String(plan.revision + 1)), secondAnswer);
assert.deepEqual((await api.subagents.list(id)).data.map(s => s.id), [child.id]);
await assert.rejects(api.subagents.turns.retrieve('turn_unknown', binding), missing);
process.stdout.write(JSON.stringify({session: id, child: child.id, turns: [one.id,two.id],
  firstAnswer, secondAnswer, revision: plan.revision, parentAnswer: answer(parent),
  streamTypes, sdk: version, foreign: 404, queryMethods: 6})+'\n');
