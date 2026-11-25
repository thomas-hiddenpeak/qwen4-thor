// Persist a real pending call on primary, then retry it from a new browser
// session on light after the uploaded file and the primary process are gone.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const client = key => new OpenAI({apiKey: key, baseURL: plan.base, maxRetries: 0});
const project = client(plan.key), peer = client(plan.other);
const actor = fileURLToPath(new URL('./chatkit_handoff_sdk.mjs', import.meta.url));
const prepare = () => JSON.parse(execFileSync(process.execPath, [actor],
  {input: JSON.stringify({...plan, phase: 'primary'}), timeout: 90000}));
assert.ok(['primary', 'light'].includes(plan.phase));
const primary = plan.phase === 'primary';
const source = primary ? prepare() : plan.previous.source;
const session = user => project.beta.chatkit.sessions.create({workflow: {id: source.workflow},
  user, rate_limits: {max_requests_per_1_minute: 40}});
const reader = await session('handoff-reader');

async function browser(grant, type, params, status = 200) {
  const res = await fetch(plan.base + '/chatkit', {method: 'POST',
    headers: {Authorization: 'Bearer ' + grant.client_secret, 'Content-Type': 'application/json'},
    body: JSON.stringify({type, params: {thread_id: source.thread, ...params}}),
    signal: AbortSignal.timeout(45000)});
  if (res.status !== status) {
    const error = await res.json();
    throw Error(type + ' HTTP ' + res.status + ' ' + error.error?.code + ' ' + error.error?.param);
  }
  if (!res.headers.get('content-type')?.includes('text/event-stream')) return res.json();
  const events = (await res.text()).split(/\r?\n/).filter(s => s.startsWith('data: '))
    .map(s => JSON.parse(s.slice(6)));
  assert.ok(!events.some(e => e.type === 'error'));
  return events;
}
const items = async limit => (await browser(reader,'items.list',{order:'asc',limit})).data;
const original = await items(0);
assert.equal(original.length, 2);
assert.deepEqual(original, await items(null));
assert.equal(original[0].attachments[0].id, source.attachment);
assert.equal(original[1].call_id, source.call);
assert.equal(original[1].status, 'pending');
assert.equal(original[1].arguments.count, source.proposed_count);
await assert.rejects(project.files.retrieve(source.attachment), e => e.status === 404);
await assert.rejects(peer.beta.chatkit.threads.retrieve(source.thread), e => e.status === 404);
const empty = await (await project.beta.chatkit.threads.listItems(source.thread,{limit:0}).asResponse()).json();
assert.deepEqual(empty.data, []);
assert.equal(empty.has_more, true);
const originalIds = original.map(item => item.id);
let recovered;
if (!primary) {
  assert.deepEqual(originalIds, plan.previous.original_ids);
  const user = originalIds[0];
  const sibling = await session('other-reader');
  await browser(sibling,'threads.retry_after_item',{item_id:user},404);
  const events = await browser(reader,'threads.retry_after_item',{item_id:user});
  const done = events.filter(e=>e.type==='thread.item.done').map(e=>e.item);
  assert.deepEqual(done.map(item=>item.type),['client_tool_call']);
  const call = done[0];
  assert.equal(call.name,'verify_scope_count');
  assert.deepEqual(call.arguments, original[1].arguments);
  assert.notEqual(call.call_id,source.call);
  const finish = await browser(reader,'threads.add_client_tool_output',
    {result:{verified_count:call.arguments.count}});
  assert.equal(finish.find(e=>e.type==='thread.item.replaced').item.call_id,call.call_id);
  const answer = finish.find(e=>e.type==='thread.item.done' && e.item.type==='assistant_message').item;
  const text = answer.content.map(part=>part.text).join('').trim();
  assert.equal(text,String(source.proposed_count));
  const ids = (await items(0)).map(item=>item.id);
  assert.deepEqual(ids,[user,call.id,answer.id]);
  recovered = {call:call.call_id, answer:text, ids, foreign_browser:404};
}
const health = await peer.chat.completions.create({model:'qwen3.8-flash-next',temperature:0,max_tokens:16,
  messages:[{role:'user',content:'Return only the integer 6 times 7.'}],chat_template_kwargs:{enable_thinking:false}});
assert.equal(health.choices[0].message.content.trim(),'42');
process.stdout.write(JSON.stringify({sdk:version, phase:plan.phase,source, original_ids:originalIds,
  browser_zero:original.length, management_zero:0, peer:404, deleted:404, chat:'42', recovered}));
