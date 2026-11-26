// Carry an interrupted project document review across a capacity or process change.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {existsSync, readFileSync, readdirSync} from 'node:fs';
import {join} from 'node:path';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const bytes of process.stdin) input += bytes;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const connect = apiKey => new OpenAI({apiKey, baseURL: plan.base, maxRetries: 0});
const owner = connect(plan.key), peer = connect(plan.other);
const retry = plan.interruptedRetry;
const release = JSON.parse(readFileSync(retry?.release ?? new URL(
  '../../docs/evidence/serve-chatkit-browser-output-20261125/delivery.json', import.meta.url)));
const revision = String(release.publication.publication_revision);
const document = Buffer.from(JSON.stringify({
  publication_revision: release.publication.publication_revision,
  previous_revision: release.previous_revision,
  desired: release.publication.desired,
  primary: release.publication.primary,
  fallback: release.publication.fallback,
}));
const sha = data => createHash('sha256').update(data).digest('hex');
const session = (workflow, user = 'project-reviewer') => owner.beta.chatkit.sessions.create({
  workflow: {id: workflow}, user, chatkit_configuration: {
    file_upload: {enabled: true, max_files: 1, max_file_size: 1},
  },
});
async function send(grant, path, body, status = 200, signal = AbortSignal.timeout(45000)) {
  const upload = body instanceof FormData;
  const response = await fetch(plan.base + path, {
    method: 'POST', signal, body: upload ? body : JSON.stringify(body),
    headers: {Authorization: 'Bearer ' + grant.client_secret,
      ...(upload ? {} : {'Content-Type': 'application/json'})},
  });
  if (response.status !== status) throw Error(path + ' ' + response.status + ' ' + await response.text());
  return response;
}
const rpc = (grant, type, params, status, signal) => send(grant, '/chatkit', {type, params}, status, signal);
const text = (value, attachments = []) => ({content: [{type: 'input_text', text: value}], attachments, inference_options: {}});
const list = async (grant, thread) => (await rpc(grant, 'items.list', {thread_id: thread, order: 'asc'})).json();

function journal(thread, assistant) {
  const records = readdirSync(retry.store).filter(name => name.includes('.response.resp_') && name.endsWith('.json'))
    .map(name => readFileSync(join(retry.store, name)))
    .map(bytes => ({bytes, record: JSON.parse(bytes).record}))
    .filter(({record}) => record.data.chatkit_thread_id === thread &&
      record.data.response.output.some(item => item.id === assistant));
  assert.equal(records.length, 1);
  const {bytes, record} = records[0];
  const {response, history, own_input, input_snapshots} = record.data;
  assert.deepEqual(history, own_input);
  assert.equal(history.length, 1);
  assert.equal(history[0].role, 'user');
  assert.equal(input_snapshots.length, 1);
  const binding = input_snapshots[0];
  assert.equal(binding.item_id, history[0].id);
  assert.equal(binding.path, '/content/0');
  const itemFile = join(retry.store, record.owner + '.item.' + binding.item_id + '.json');
  return {response: response.id, status: response.status, record_sha256: sha(bytes),
    file: history[0].content[0].file_id, published: existsSync(itemFile), binding};
}

async function interrupt() {
  const workflow = (await owner.beta.agents.create({model: plan.model, reasoning: {effort: 'none'}})).id;
  const grant = await session(workflow);
  const form = new FormData();
  form.set('file', new File([document], 'publication.json', {type: 'application/json'}));
  const file = (await (await send(grant, '/chatkit/attachments', form)).json()).id;
  const stop = new AbortController();
  const r = await rpc(grant, 'threads.create', {input: text(
    retry ? 'Start with the integer publication_revision from the attached record, then explain what that number identifies in one sentence.' :
    'Explain the attached project publication to a reviewer in thirty numbered sentences, one full sentence per line.', [file],
  )}, 200, AbortSignal.any([stop.signal, AbortSignal.timeout(45000)]));
  const reader = r.body.getReader(), decoder = new TextDecoder();
  let carry = '', thread, item, prefix = '', allowCancel = false;
  while (!prefix.trim()) {
    const chunk = await reader.read();
    assert.ok(!chunk.done, 'model stream ended before its first assistant text');
    carry += decoder.decode(chunk.value, {stream: true});
    let sep;
    while ((sep = /\r?\n\r?\n/.exec(carry))) {
      const line = carry.slice(0, sep.index);
      carry = carry.slice(sep.index + sep[0].length);
      if (!line.startsWith('data: ')) continue;
      const event = JSON.parse(line.slice(6));
      if (event.type === 'thread.created') thread = event.thread.id;
      if (event.type === 'stream_options') allowCancel = event.stream_options.allow_cancel;
      if (event.update?.type === 'assistant_message.content_part.text_delta') {
        item = event.item_id;
        prefix += event.update.delta;
      }
    }
  }
  stop.abort();
  await reader.cancel().catch(() => {});
  assert.ok(allowCancel && thread && item);
  const end = Date.now() + 40000;
  let partial;
  do {
    const rows = await owner.beta.chatkit.threads.listItems(thread, {order: 'asc'});
    partial = rows.data.find(row => row.id === item);
    if (!partial) await new Promise(resolve => setTimeout(resolve, 200));
  } while (!partial && Date.now() < end);
  assert.ok(partial?.content[0].text.startsWith(prefix));
  const saved = await list(grant, thread);
  assert.deepEqual(saved.data.map(row => row.type), ['user_message', 'assistant_message']);
  assert.equal(saved.data[0].attachments[0].id, file);
  assert.equal(saved.data[1].content[0].text, partial.content[0].text);
  const source = {workflow, thread, file, item, user: saved.data[0].id, received: prefix,
    partial: partial.content[0].text, document_sha256: sha(document)};
  if (retry) {
    source.interrupted = journal(thread, item);
    assert.equal(source.interrupted.status, 'cancelled');
    assert.equal(source.interrupted.file, file);
    assert.equal(source.interrupted.published, false);
    const uploaded = Buffer.from(await (await owner.files.content(file)).arrayBuffer());
    assert.equal(sha(uploaded), source.document_sha256);
    await owner.files.delete(file);
    await assert.rejects(owner.files.content(file), error => error.status === 404);
    source.deleted = 404;
  }
  return source;
}

async function resume(source) {
  const {workflow, thread, file, item, user, partial} = source;
  const grant = await session(workflow), sibling = await session(workflow, 'other-reader');
  await rpc(sibling, 'items.list', {thread_id: thread}, 404);
  await assert.rejects(peer.beta.chatkit.threads.retrieve(thread), error => error.status === 404);
  let bytes;
  if (retry) {
    await assert.rejects(owner.files.content(file), error => error.status === 404);
    assert.deepEqual(journal(thread, item), source.interrupted);
  } else {
    bytes = Buffer.from(await (await owner.files.content(file)).arrayBuffer());
    assert.equal(sha(bytes), sha(document));
  }
  const before = await list(grant, thread);
  assert.deepEqual(before.data.map(row => row.id), [user, item]);
  assert.equal(before.data[1].content[0].text, partial);
  const response = retry
    ? await rpc(grant, 'threads.retry_after_item', {thread_id: thread, item_id: user})
    : await rpc(grant, 'threads.add_user_message', {thread_id: thread,
    input: text('What is publication_revision in the attached project publication? Reply only with that integer.')});
  const events = (await response.text()).split(/\r?\n/).filter(line => line.startsWith('data: '))
    .map(line => JSON.parse(line.slice(6)));
  assert.ok(!events.some(event => event.type === 'error'));
  const answer = events.find(event => event.type === 'thread.item.done' && event.item.type === 'assistant_message')?.item;
  if (retry) {
    const text = answer?.content[0].text.trim();
    assert.equal(text?.match(/^([0-9]+)\b/)?.[1], revision, text);
    const after = await list(grant, thread);
    const management = await owner.beta.chatkit.threads.listItems(thread, {order: 'asc'});
    const ids = [user, answer.id];
    assert.deepEqual(after.data.map(row => row.id), ids);
    assert.deepEqual(management.data.map(row => row.id), ids);
    assert.deepEqual(after.data[0], before.data[0]);
    assert.equal(management.data[1].content[0].text, text);
    const original = journal(thread, item), replacement = journal(thread, answer.id);
    assert.deepEqual(original, {...source.interrupted, published: true});
    assert.deepEqual(replacement.binding, original.binding);
    assert.equal(replacement.file, file);
    assert.equal(replacement.status, 'completed');
    assert.notEqual(replacement.response, original.response);
    return {ids, text, peer: 404, sibling: 404, deleted: 404, original, replacement};
  }
  assert.equal(answer?.content[0].text.trim(), revision);
  const after = await list(grant, thread);
  assert.deepEqual(after.data.slice(0, 2), before.data);
  const history = await owner.beta.chatkit.threads.listItems(thread, {order: 'asc'});
  const ids = after.data.map(row => row.id);
  assert.deepEqual(history.data.map(row => row.id), ids);
  assert.equal(history.data[1].content[0].text, partial);
  assert.equal(history.data.at(-1).content[0].text.trim(), revision);
  return {answer: revision, ids, peer: 404, sibling: 404, file_sha256: sha(bytes)};
}

assert.ok(['primary', 'light'].includes(plan.phase));
const source = plan.phase === 'primary' ? await interrupt() : plan.previous.source;
const recovered = plan.phase === 'light' ? await resume(source) : undefined;
const chat = await peer.chat.completions.create({model: plan.model, temperature: 0, max_tokens: 16,
  messages: [{role: 'user', content: 'Return only the integer 6 times 7.'}], chat_template_kwargs: {enable_thinking: false}});
assert.equal(chat.choices[0].message.content.trim(), '42');
process.stdout.write(JSON.stringify({sdk: version, phase: plan.phase, source, recovered, revision, chat: '42'}));
