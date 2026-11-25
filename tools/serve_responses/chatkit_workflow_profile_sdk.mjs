// Reopen a genuine, completed workflow and hand a new pending call to the next process.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

const evidence = new URL('../../docs/evidence/', import.meta.url);
const read = name => JSON.parse(readFileSync(new URL(name, evidence)));
const history = read('serve-chatkit-reasoning-workflow-20261126/history.json');
const release = read('serve-chatkit-generation-stream-20261125/delivery.json').publication;
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
let input = '';
for await (const block of process.stdin) input += block;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const client = apiKey => new OpenAI({apiKey, baseURL: plan.base, maxRetries: 0});
const owner = client(plan.key), peer = client(plan.other);
const open = user => owner.beta.chatkit.sessions.create({user, workflow: {id: history.agent}});
const grant = await open(history.user);
async function rpc(session, type, params, expected = 200) {
  const response = await fetch(plan.base + '/chatkit', {method: 'POST',
    headers: {Authorization: 'Bearer ' + session.client_secret, 'Content-Type': 'application/json'},
    body: JSON.stringify({type,params}), signal: AbortSignal.timeout(110000)});
  assert.equal(response.status, expected, type);
  if (!response.headers.get('content-type')?.includes('text/event-stream')) return response.json();
  const events = (await response.text()).split(/\r?\n/).filter(line => line.startsWith('data: '))
    .map(line => JSON.parse(line.slice(6)));
  assert.ok(!events.some(e => e.type === 'error'), type + ' received a terminal error');
  return events;
}
async function inspect(thread) {
  const page = await rpc(grant, 'items.list', {thread_id: thread, order:'asc', limit:100});
  const managed = await owner.beta.chatkit.threads.listItems(thread, {order:'asc', limit:100});
  const ids = page.data.map(i => i.id);
  assert.equal(new Set(ids).size, ids.length);
  assert.deepEqual(ids, managed.data.map(i => i.id));
  const groups = page.data.filter(i => i.type === 'workflow')
    .map(i => [i.id, i.workflow.tasks.map(t => t.content)]);
  assert.deepEqual(groups, managed.data.filter(i => i.type === 'chatkit.task_group')
    .map(i => [i.id, i.tasks.map(t => t.summary)]));
  return {ids, groups:groups.map(([id,tasks]) => [id,tasks.map(hash)]),
    groups_sha256:hash(groups), items:page.data};
}
const restored = await inspect(history.thread);
assert.deepEqual(restored.ids, history.items);
assert.equal(restored.groups_sha256, history.groups_sha256);
const archive = {thread:history.thread, groups_sha256:restored.groups_sha256, items:restored.ids};
let source, recovered;
if (plan.phase === 'primary') {
  const name = 'read_project_publication';
  const events = await rpc(grant, 'threads.create', {input:{attachments:[],
    content:[{type:'input_text', text:'Read the current project publication with the tool. Reply with only the revision number it returns.'}],
    inference_options:{tool_choice:{id:name}}}});
  const thread = events.find(e => e.type === 'thread.created').thread.id;
  const item = events.find(e => e.type === 'thread.item.done' && e.item.type === 'client_tool_call').item;
  assert.equal(item.status, 'pending');
  assert.equal(item.name, name);
  const view = await inspect(thread);
  assert.ok(view.groups.length > 0 && view.ids.includes(item.id));
  assert.deepEqual(view.items.find(i => i.id === item.id), item);
  source = {thread, item:item.id, call:item.call_id, ids:view.ids, groups:view.groups,
    groups_sha256:view.groups_sha256};
} else {
  assert.equal(plan.phase, 'light');
  source = plan.previous.source;
  assert.deepEqual(archive, plan.previous.archive);
  const before = await inspect(source.thread);
  assert.deepEqual(before.ids, source.ids);
  assert.equal(before.groups_sha256, source.groups_sha256);
  assert.deepEqual(before.groups, source.groups);
  const item = before.items.find(i => i.id === source.item);
  assert.equal(item.status,'pending');
  assert.equal(item.call_id,source.call);
  const sibling = await open('other-reader');
  await rpc(sibling,'items.list',{thread_id:source.thread},404);
  await assert.rejects(peer.beta.chatkit.threads.retrieve(source.thread), e => e.status === 404);
  const toolResult = {publication_revision:release.publication_revision, desired:release.desired};
  const events = await rpc(grant,'threads.add_client_tool_output',{thread_id:source.thread, result:toolResult});
  const filled = events.find(e => e.type === 'thread.item.replaced')?.item;
  assert.equal(filled.id, source.item);
  assert.equal(filled.call_id, source.call);
  assert.equal(filled.status, 'completed');
  const last = events.filter(e => e.type === 'thread.item.done' && e.item.type === 'assistant_message').at(-1).item;
  const answer = last.content.map(p => p.text).join('').trim();
  const after = await inspect(source.thread);
  assert.deepEqual(after.ids.slice(0,source.ids.length), source.ids);
  assert.equal(after.items.find(i => i.id === last.id).content.map(p => p.text).join('').trim(),answer);
  for (const [id,tasks] of source.groups) {
    const found = after.groups.find(g => g[0]===id);
    assert.ok(found);
    assert.deepEqual(found[1].slice(0,tasks.length),tasks);
  }
  await rpc(grant,'threads.add_client_tool_output',{thread_id:source.thread,result:toolResult},409);
  recovered = {answer, message:last.id, ids:after.ids, call:filled.call_id, item:filled.id,
    before_sha256:before.groups_sha256, sibling:404, peer:404, duplicate:409};
}
const independent = await peer.chat.completions.create({model:plan.model,temperature:0,max_tokens:32,
  messages:[{role:'user',content:'Compute 17 + 25. Return only the integer.'}],
  chat_template_kwargs:{enable_thinking:false}});
process.stdout.write(JSON.stringify({phase:plan.phase,sdk:version,revision:String(release.publication_revision),
  chat:independent.choices[0].message.content.trim(), archive, source,...(recovered ? {recovered}: {})}));
