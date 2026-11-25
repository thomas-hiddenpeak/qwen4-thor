// A browser asks for a project record, returns actual tool output, and reads
// the resulting public reasoning summaries with a fresh session.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
const client = apiKey => new OpenAI({baseURL: p.base, apiKey, maxRetries: 0});
const owner = client(p.key), peer = client(p.other);
const document = readFileSync(new URL(
  '../../docs/evidence/serve-chatkit-browser-output-20261125/delivery.json', import.meta.url));
const publication = JSON.parse(document).publication;
const name = 'read_project_publication';
const agent = await owner.beta.agents.create({
  model: p.model, reasoning: {effort: 'low', summary: 'concise'},
  instructions: 'Use the registered tool for project publication questions. Once the tool returns, reply with just its publication_revision number.',
  tools: [{type: 'function', name, description: 'Read the verified project publication record.',
    parameters: {type: 'object', properties: {}, additionalProperties: false, required: []}}],
});
const session = user => owner.beta.chatkit.sessions.create({user, workflow: {id: agent.id}});
async function rpc(grant, type, params, status = 200) {
  const r = await fetch(p.base + '/chatkit', {method: 'POST', signal: AbortSignal.timeout(140000),
    body: JSON.stringify({type, params}), headers: {
      Authorization: 'Bearer ' + grant.client_secret, 'Content-Type': 'application/json',
    }});
  if (r.status !== status) throw Error(type + ' ' + r.status + ' ' + (await r.text()).slice(0,400));
  if (!r.headers.get('content-type')?.includes('text/event-stream')) return r.json();
  const events = (await r.text()).split(/\r?\n/).filter(line => line.startsWith('data: '))
    .map(line => JSON.parse(line.slice(6)));
  assert.ok(!events.some(e => e.type === 'error'), JSON.stringify(events.at(-1)));
  return events;
}
const grant = await session('release-reviewer');
const first = await rpc(grant, 'threads.create', {input: {attachments: [],
  inference_options: {tool_choice: {id: name}}, content: [{type: 'input_text',
    text: 'Read the Q4T project publication record with the tool. What publication revision does it report?'}]}});
const thread = first.find(e => e.type === 'thread.created')?.thread.id;
const call = first.find(e => e.type === 'thread.item.done' && e.item.type === 'client_tool_call')?.item;
assert.ok(thread && call?.name === name && call.status === 'pending');
const pending = await rpc(grant, 'items.list', {thread_id: thread, order: 'asc'});
const pendingCall = pending.data.find(i => i.id === call.id);
assert.equal(pendingCall.call_id, call.call_id);
const result = {publication_revision: publication.publication_revision, desired: publication.desired};
const second = await rpc(await session('release-reviewer'), 'threads.add_client_tool_output',
  {thread_id: thread, result});
assert.equal(second.find(e => e.type === 'thread.item.replaced')?.item.id, call.id);
const answer = second.filter(e => e.type === 'thread.item.done' && e.item.type === 'assistant_message')
  .at(-1)?.item.content.map(part => part.text).join('').trim();
assert.equal(typeof answer, 'string');
const final = await rpc(await session('release-reviewer'), 'items.list', {thread_id: thread, order: 'asc'});
const managementPage = await (await owner.beta.chatkit.threads.listItems(thread, {order: 'asc', limit: 100}).asResponse()).json();
const management = managementPage.data;
assert.deepEqual(final.data.map(x => x.id), management.map(x => x.id));
const groups = final.data.filter(x => x.type === 'workflow');
assert.ok(groups.length > 0);
for (const group of groups) {
  const m = management.find(row => row.id === group.id);
  assert.equal(m.type, 'chatkit.task_group');
  assert.deepEqual(m.tasks.map(t => [t.type,t.heading,t.summary]),
    group.workflow.tasks.map(t => [t.type,t.title,t.content]));
}
const ids = final.data.map(row => row.id);
assert.equal(new Set(ids).size, ids.length);
await rpc(await session('other-reader'), 'items.list', {thread_id: thread}, 404);
await assert.rejects(peer.beta.chatkit.threads.retrieve(thread), e => e.status === 404);
const independent = await peer.chat.completions.create({model: p.model, temperature: 0, max_tokens: 32,
  messages: [{role: 'user', content: 'Compute 17 + 25. Return only the integer.'}], chat_template_kwargs: {enable_thinking:false}});
assert.equal(independent.choices[0].message.content.trim(), '42');
// Use the same arithmetic control as the retained positive Responses capture.
const mathAgent = await owner.beta.agents.create({model: p.model,
  reasoning: {effort: 'low', summary: 'detailed'}});
const mathGrant = await owner.beta.chatkit.sessions.create({user: 'release-reviewer',
  workflow: {id: mathAgent.id}});
const math = await rpc(mathGrant, 'threads.create', {input: {attachments: [],
  inference_options: {}, content: [{type: 'input_text',
    text: 'Compute 17 + 25. Reply only with the decimal number.'}]}});
const mathThread = math.find(e => e.type === 'thread.created').thread.id;
const mathPage = await rpc(mathGrant, 'items.list', {thread_id: mathThread, order: 'asc'});
const mathAnswer = mathPage.data.filter(i => i.type === 'assistant_message')
  .at(-1)?.content.map(part => part.text).join('').trim();
assert.equal(typeof mathAnswer, 'string');
const mathManagement = await (await owner.beta.chatkit.threads.listItems(mathThread,
  {order: 'asc', limit: 100}).asResponse()).json();
assert.deepEqual(mathPage.data.map(i => i.id), mathManagement.data.map(i => i.id));
process.stdout.write(JSON.stringify({sdk: version, thread, mathThread, math, mathPage,
  mathManagement, answer, mathAnswer, call: call.call_id, item: call.id,
  source_sha256: createHash('sha256').update(document).digest('hex'), revision: result.publication_revision,
  first, pending, second, final, managementPage, other: 404, peer: 404, independent_chat: '42'}));
