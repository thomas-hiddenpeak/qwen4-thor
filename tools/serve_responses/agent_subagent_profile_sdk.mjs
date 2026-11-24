// Follow one owned child across two separately started managed models.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { loadSdk } from '../serve_protocol/sdk.mjs';

let raw = '';
for await (const piece of process.stdin) {
  raw += piece;
  assert.ok(Buffer.byteLength(raw) <= 32768);
}
const p = JSON.parse(raw);
assert.ok(['primary', 'light'].includes(p.profile));
const base = new URL(p.base);
assert.equal(base.hostname, '127.0.0.1');
assert.equal(base.protocol, 'https:');
const { OpenAI, version } = loadSdk(p.sdk);
assert.equal(version, '7.47.0');
const sessions = key => new OpenAI({ baseURL: p.base, apiKey: key,
  maxRetries: 0, timeout: 90000 }).beta.agents.sessions;
const api = sessions(p.key);
const other = sessions(p.other);
const digest = rows => createHash('sha256').update(JSON.stringify(rows)).digest('hex');
const answer = rows => rows.filter(i => i.role === 'assistant').flatMap(i => i.content)
  .map(part => part.text || '').join('').trim();
const number = (text, value) => assert.ok(text.match(/\d+/g)?.includes(String(value)), text);
async function collect(stream) {
  const events = [];
  try {
    for await (const e of stream) {
      if (e.type === 'agent.session.turn.failed') throw Error(JSON.stringify(e.turn.error));
      events.push(e);
      if (e.type === 'agent.session.idle') return events;
    }
    throw Error('Session stream ended before idle');
  } finally { stream.controller.abort(); }
}
async function submit(session, text) {
  const stream = await api.events.stream(session);
  try {
    await api.events.create(session, { events: [{ type: 'agent.session.input.message',
      input: [{ role: 'user', content: [{ type: 'input_text', text }] }] }] });
    return await collect(stream);
  } finally { stream.controller.abort(); }
}
async function history(session, child) {
  const owner = { session_id: session, order: 'asc' };
  const result = [];
  for (const turn of (await api.subagents.turns.list(child, owner)).data) {
    const items = [];
    for await (const item of api.subagents.turns.items.list(turn.id, { ...owner, subagent_id: child })) {
      items.push(item);
    }
    const outputs = items.filter(i => i.role !== 'user').map(i => i.id);
    result.push({ id: turn.id, status: turn.status, items_sha256: digest(items), outputs, answer: answer(items) });
  }
  return result;
}
function checkOutput(events, turn) {
  const own = events.filter(e => e.turn_id === turn.id);
  assert.deepEqual(own.filter(e => e.turn).map(e => e.turn.status), ['queued','in_progress','completed']);
  const deltas = own.filter(e => e.type === 'agent.session.turn.output_text.delta');
  const completed = own.filter(e => e.type === 'agent.session.turn.item.done');
  assert.deepEqual(completed.map(e => e.item.id), turn.outputs);
  assert.deepEqual(completed.map(e => e.output_index), turn.outputs.map((_, i) => i));
  assert.ok(deltas.length > 0);
  for (const delta of deltas) {
    const output = completed[delta.output_index];
    assert.equal(output?.item.role, 'assistant');
    assert.equal(output.item.id, delta.item_id);
  }
  assert.equal(deltas.map(e => e.delta).join('').trim(), turn.answer);
}
async function lifecycle(session, child, action, state) {
  const events = await submit(session, 'Please ' + action + ' the existing subagent root/reader now using its ' +
    action + '_subagent function. Confirm when done.');
  const current = await api.subagents.retrieve(child, { session_id: session });
  assert.equal(current.status, state);
  assert.deepEqual(events.filter(e => e.type === 'agent.session.subagent.' + state).map(e => e.subagent), [current]);
  const ops = events.filter(e => e.type === 'agent.session.turn.item.done' && e.item.type === action + '_subagent_call');
  assert.equal(ops.length, 1);
  assert.equal(ops[0].item.status, 'completed');
  assert.equal(ops[0].item.recipient_agent_id, child);
  assert.equal(ops[0].item.sender_agent_id, current.parent_agent_id);
  return current;
}
const primary = p.profile === 'primary';
let session, child, closed, turns, active;
if (primary) {
  assert.ok(Number.isSafeInteger(p.revision));
  const events = await collect(await api.create({ environment: { type: 'none' }, stream: true,
    agent: { model: 'qwen3.8-flash-next', reasoning: { effort: 'none' },
      multi_agent: { enabled: true, max_concurrent_subagents: 1 },
      instructions: 'Use multi-agent functions for each explicit action. First spawn one child named reader and wait_agent on root/reader. For close or resume, call close_subagent or resume_subagent on root/reader. For recall, use followup_task on the same reader and wait for its answer. Never spawn another child.' },
    input: 'Delegate this exact task to reader: Extract the integer from this inline JSON record: {"publication_revision":' + p.revision +
      '}. Report only its publication_revision number. Wait for reader, then answer with the number.' }));
  session = events.find(e => e.type === 'agent.session.created').session.id;
  const created = events.find(e => e.type === 'agent.session.subagent.created').subagent;
  child = created.id;
  turns = await history(session, child);
  assert.equal(turns.length, 1);
  checkOutput(events, turns[0]);
  number(turns[0].answer, p.revision);
  closed = await lifecycle(session, child, 'close', 'closed');
  assert.equal(closed.opened_at, created.opened_at);
  assert.ok(closed.closed_at >= closed.opened_at);
  assert.deepEqual(await history(session, child), turns);
} else {
  ({session, child, closed} = p.prior);
  assert.deepEqual(await api.subagents.retrieve(child, { session_id: session }), closed);
  assert.deepEqual(await history(session, child), p.prior.turns);
  active = await lifecycle(session, child, 'resume', 'active');
  assert.deepEqual(active, { ...closed, closed_at: null, status: 'active' });
  assert.deepEqual(await history(session, child), p.prior.turns);
  const recalled = await submit(session, 'Give the existing reader a follow-up task: recall the publication revision you read, add one, and reply with the resulting integer. Wait and give its answer.');
  turns = await history(session, child);
  assert.equal(turns.length, 2);
  assert.deepEqual(turns[0], p.prior.turns[0]);
  assert.notEqual(turns[1].id, turns[0].id);
  checkOutput(recalled, turns[1]);
  number(turns[1].answer, p.revision + 1);
}
assert.deepEqual((await api.subagents.list(session)).data.map(s => s.id), [child]);
await assert.rejects(other.subagents.retrieve(child, { session_id: session }), e => e.status === 404);
process.stdout.write(JSON.stringify({ sdk: version, session, child, closed, active, turns,
  revision: p.revision, foreign: 404 }) + '\n');
