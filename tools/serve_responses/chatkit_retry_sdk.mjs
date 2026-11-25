// Exercise a browser retry against a persisted tool turn and a deleted upload.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const plan = JSON.parse(input);
const actor = fileURLToPath(new URL('./chatkit_handoff_sdk.mjs', import.meta.url));
const handoff = (phase, previous) => JSON.parse(execFileSync(process.execPath,
  [actor], {input: JSON.stringify({...plan, phase, previous}), timeout: 75000}));
const first = handoff('primary');
const original = handoff('light', first);
const {OpenAI, version} = loadSdk(plan.sdk);
const project = new OpenAI({baseURL: plan.base, apiKey: plan.key, maxRetries: 0});
const session = user => project.beta.chatkit.sessions.create({
  workflow: {id: first.workflow}, user, rate_limits: {max_requests_per_1_minute: 40},
});
const reader = await session('handoff-reader');
const sibling = await session('other-reader');

async function browser(grant, type, params, status = 200) {
  const res = await fetch(plan.base + '/chatkit', {method: 'POST',
    headers: {Authorization: 'Bearer ' + grant.client_secret, 'Content-Type': 'application/json'},
    body: JSON.stringify({type, params: {thread_id: first.thread, ...params}}),
    signal: AbortSignal.timeout(45000)});
  if (res.status !== status) {
    const error = await res.json();
    throw Error(type + ' HTTP ' + res.status + ' ' + error.error?.code +
      ' param=' + error.error?.param + ' ' + error.error?.message);
  }
  if (!res.headers.get('content-type')?.includes('text/event-stream')) return res.json();
  const events = (await res.text()).split(/\r?\n/).filter(s => s.startsWith('data: '))
    .map(s => JSON.parse(s.slice(6)));
  assert.ok(!events.some(e => e.type === 'error'), 'retry stream contains an error');
  return events;
}
const list = async () => (await browser(reader, 'items.list', {order: 'asc'})).data;
const before = await list();
assert.equal(before.length, 3);
assert.equal(before[0].attachments[0].id, first.attachment);
const retry = (grant, id, status) => browser(grant, 'threads.retry_after_item', {item_id: id}, status);
await retry(sibling, before[0].id, 404);
assert.equal((await retry(reader, before[2].id, 400)).error.param, 'item_id');
assert.deepEqual(await retry(reader, 'item_absent'), []);
assert.deepEqual(await list(), before);
const events = await retry(reader, before[0].id);
const done = events.filter(e => e.type === 'thread.item.done').map(e => e.item);
assert.deepEqual(done.map(item => item.type), ['client_tool_call']);
const call = done[0];
assert.equal(call.name, 'verify_scope_count');
assert.deepEqual(call.arguments, {count: first.proposed_count});
assert.notEqual(call.call_id, first.call);
assert.equal(call.status, 'pending');
const branched = await list();
assert.deepEqual(branched.map(item => item.id), [before[0].id, call.id]);
await assert.rejects(project.files.retrieve(first.attachment), err => err.status === 404);
const finish = await browser(reader, 'threads.add_client_tool_output',
  {result: {verified_count: call.arguments.count}});
const replacement = finish.find(e => e.type === 'thread.item.replaced')?.item;
assert.equal(replacement?.call_id, call.call_id);
const answer = finish.find(e => e.type === 'thread.item.done' &&
  e.item.type === 'assistant_message')?.item;
assert.equal(answer.content.map(p => p.text).join('').trim(), original.answer);
const final = await list();
assert.deepEqual(final.map(item => item.id), [before[0].id, call.id, answer.id]);
assert.notEqual(answer.id, before[2].id);
const management = await project.beta.chatkit.threads.listItems(first.thread, {order: 'asc'});
assert.deepEqual(management.data.map(item => item.id), final.map(item => item.id));
process.stdout.write(JSON.stringify({sdk: version, thread: first.thread, user_item: before[0].id,
  source_sha256: first.sha256, file_deleted: true, foreign: 404, non_user: 400,
  missing_events: 0, original_call: first.call, retry_call: call.call_id,
  verified_count: call.arguments.count, original_answer: original.answer,
  answer: answer.content[0].text.trim(), independent_chat: original.chat,
  old_items: before.map(item => item.id), final_items: final.map(item => item.id)}));
