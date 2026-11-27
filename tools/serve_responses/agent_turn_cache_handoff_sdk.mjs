// Re-read a real foreground run through a newly started capacity or recovery process.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input), before = plan.foreground;
assert.ok(['primary', 'light', 'read'].includes(plan.phase));
const url = new URL(plan.base);
assert.equal(url.hostname, '127.0.0.1');
assert.ok(['https:', 'http:'].includes(url.protocol));
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const client = apiKey => new OpenAI({baseURL: url.href, apiKey, maxRetries: 0, timeout: 90000}).beta.agents.sessions;
const owner = client(plan.key), peer = client(plan.other);
const session = before.session, child = before.child;
const binding = {session_id: session, subagent_id: child};
const digest = obj => createHash('sha256').update(JSON.stringify(obj, (_, value) => {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return value;
  return Object.fromEntries(Object.keys(value).sort().map(k => [k,value[k]]));
})).digest('hex');
const roots = (await owner.turns.list(session, {order:'asc'})).data;
const children = (await owner.subagents.turns.list(child, {session_id:session, order:'asc'})).data;
assert.equal(roots.length, 2);
assert.equal(children.length, 2);
const rows = [];
for (const [i, selection] of before.turns.entries()) {
  const {root, child: delegated, ttl} = selection;
  assert.equal(ttl, ['12h', '30m'][i]);
  assert.notEqual(root, delegated);
  assert.equal(roots[i].id, root);
  assert.equal(children[i].id, delegated);
  const rootTurn = await owner.turns.retrieve(root, {session_id:session});
  const childTurn = await owner.subagents.turns.retrieve(delegated, binding);
  assert.deepEqual(roots[i], rootTurn);
  assert.deepEqual(children[i], childTurn);
  for (const turn of [rootTurn, childTurn]) {
    assert.equal(turn.status, 'completed');
    assert.deepEqual(turn.prompt_cache_options, {ttl});
  }
  const items = (await owner.subagents.turns.items.list(delegated, {...binding,order:'asc'})).data;
  const answer = items.filter(x=>x.role==='assistant').flatMap(x=>x.content).map(x=>x.text || '').join('').trim();
  assert.equal(answer, [before.first_answer, before.followup_answer][i]);
  assert.ok(answer.match(/\d+/g)?.includes(String(plan.revision+i)));
  const missing = e => e.status===404;
  await assert.rejects(peer.turns.retrieve(root, {session_id:session}), missing);
  await assert.rejects(peer.subagents.turns.retrieve(delegated, binding), missing);
  rows.push({root, child:delegated, ttl, answer, root_sha256:digest(rootTurn), child_sha256:digest(childTurn), items_sha256:digest(items)});
}
assert.equal(rows.length, 2);
if (plan.previous) assert.deepEqual(rows, plan.previous.rows);
const outcome = {sdk:version, phase:plan.phase, session, child, revision:plan.revision,
  rows, cross_client_404:4};
if (plan.phase === 'primary') {
  assert.deepEqual((await owner.retrieve(session)).prompt_cache_options, {ttl:'30m'});
  const updated = await owner.update(session, {prompt_cache_options:{ttl:'12h'}});
  assert.deepEqual(updated.prompt_cache_options, {ttl:'12h'});
  outcome.next_ttl = updated.prompt_cache_options.ttl;
} else if (plan.phase === 'light') {
  assert.equal(plan.previous.next_ttl, '12h');
  assert.deepEqual((await owner.retrieve(session)).prompt_cache_options, {ttl:'12h'});
  const stream = await owner.events.stream(session);
  const turns = new Map();
  try {
    await owner.events.create(session, {events:[{type:'agent.session.input.message',input:[
      {role:'user',content:[{type:'input_text',text:'Give the existing reader one follow-up task. Recall the original publication revision from the first lookup, add two, and return the resulting integer. Wait for the reader, then answer with that integer.'}]}]}]});
    let idle = false;
    for await (const event of stream) {
      if (event.type === 'agent.session.turn.failed') throw Error(JSON.stringify(event.turn.error));
      if (event.type === 'agent.session.turn.completed') turns.set(event.turn.id, event.turn);
      if (event.type === 'agent.session.idle') {idle=true; break;}
    }
    assert.ok(idle);
  } finally {stream.controller.abort();}
  const later = (await owner.subagents.turns.list(child, {session_id:session, order:'asc'})).data;
  assert.deepEqual(later.slice(0,2), children);
  assert.equal(later.length, 3);
  const last = later[2];
  assert.deepEqual(last.prompt_cache_options, {ttl:'12h'});
  assert.deepEqual(turns.get(last.id), last);
  const rootPage = (await owner.turns.list(session, {order:'asc'})).data;
  assert.deepEqual(rootPage.slice(0,2), roots);
  assert.equal(rootPage.length, 3);
  const root = rootPage[2];
  assert.deepEqual(root.prompt_cache_options, {ttl:'12h'});
  assert.deepEqual(turns.get(root.id), root);
  const items = (await owner.subagents.turns.items.list(last.id, {...binding,order:'asc'})).data;
  const answer = items.filter(x=>x.role==='assistant').flatMap(x=>x.content)
    .map(x=>x.text || '').join('').trim();
  assert.ok(answer.match(/\d+/g)?.includes(String(plan.revision+2)), answer);
  outcome.continuation = {root:root.id,child:last.id,ttl:'12h',answer,
    items_sha256:digest(items),completed_sse:turns.size};
}
process.stdout.write(JSON.stringify(outcome)+'\n');
