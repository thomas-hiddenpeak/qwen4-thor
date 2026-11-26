// Abort a real generation, reopen the thread with a renewed user grant, and
// compare the partial answer from the browser and management representations.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync, readdirSync} from 'node:fs';
import {join} from 'node:path';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const config = JSON.parse(input);
const {OpenAI, version} = loadSdk(config.sdk);
const owner = new OpenAI({apiKey: config.key, baseURL: config.base, maxRetries: 0});
const agent = await owner.beta.agents.create({
  model: config.model, reasoning: {effort: 'none'},
});
const grant = () => owner.beta.chatkit.sessions.create({
  workflow: {id: agent.id}, user: 'disconnect-reader',
  ...(config.retryInterrupted && {chatkit_configuration: {file_upload: {enabled: true, max_files: 1, max_file_size: 1}}}),
});
const message = (text, attachments = []) => ({
  input: {attachments, inference_options: {}, content: [{type: 'input_text', text}]},
});
async function send(session, type, params, signal = AbortSignal.timeout(45000)) {
  const response = await fetch(config.base + '/chatkit', {
    method: 'POST', signal, body: JSON.stringify({type, params}),
    headers: {Authorization: 'Bearer ' + session.client_secret, 'Content-Type': 'application/json'},
  });
  if (response.status !== 200) throw Error(type + ' ' + response.status + ' ' + await response.text());
  return response;
}

const initial = await grant();
let publication, revision;
if (config.retryInterrupted) {
  const bytes = readFileSync(config.publication);
  assert.equal(createHash('sha256').update(bytes).digest('hex'), config.publicationSha256);
  revision = JSON.parse(bytes).publication_revision;
  assert.ok(Number.isSafeInteger(revision) && revision > 0);
  const form = new FormData();
  form.set('file', new File([bytes], 'publication.json', {type: 'application/json'}));
  const response = await fetch(config.base + '/chatkit/attachments', {
    method: 'POST', body: form, signal: AbortSignal.timeout(20000),
    headers: {Authorization: 'Bearer ' + initial.client_secret},
  });
  assert.equal(response.status, 200);
  publication = (await response.json()).id;
  assert.ok(publication);
}
const controller = new AbortController();
const response = await send(initial, 'threads.create', message(
  config.retryInterrupted ? 'From the attached project publication record, give the value of publication_revision, then explain what that number identifies in one sentence. Start your answer with the integer.' :
  'Write a numbered list of thirty observations about software testing, with one ordinary sentence per line.',
  publication ? [publication] : [],
), AbortSignal.any([controller.signal, AbortSignal.timeout(45000)]));
const reader = response.body.getReader();
const decoder = new TextDecoder();
let carry = '', thread, excerpt = '', item, deltaCount = 0, options;
while (!excerpt.trim()) {
  const chunk = await reader.read();
  assert.ok(!chunk.done, 'stream ended before any nonempty assistant text');
  carry += decoder.decode(chunk.value, {stream: true});
  let sep;
  while ((sep = /\r?\n\r?\n/.exec(carry))) {
    const line = carry.slice(0, sep.index);
    carry = carry.slice(sep.index + sep[0].length);
    if (!line.startsWith('data: ')) continue;
    const event = JSON.parse(line.slice(6));
    if (event.type === 'stream_options') options = event.stream_options;
    if (event.type === 'thread.created') thread = event.thread.id;
    if (event.update?.type === 'assistant_message.content_part.text_delta') {
      item = event.item_id;
      excerpt += event.update.delta;
      ++deltaCount;
    }
  }
}
assert.ok(thread && item);
controller.abort();
await reader.cancel().catch(() => {});
assert.deepEqual(options, {allow_cancel: true});

const deadline = Date.now() + 42000;
let partial;
do {
  const items = await owner.beta.chatkit.threads.listItems(thread, {order: 'asc'});
  partial = items.data.find(candidate => candidate.id === item);
  if (!partial) await new Promise(resolve => setTimeout(resolve, 300));
} while (!partial && Date.now() < deadline);
assert.ok(partial, 'cancelled assistant text missing from thread');
const observed = partial.content[0].text;
assert.ok(observed.startsWith(excerpt));

const fresh = await grant();
const list = async () => (await send(fresh, 'items.list', {thread_id: thread, order: 'asc'})).json();
const before = await list();
assert.equal(before.data.filter(row => row.id === item).length, 1);
assert.equal(before.data.find(row => row.id === item).content[0].text, observed);
let retried, continuedFrom = before;
if (config.retryInterrupted) {
  const original = before.data[0];
  assert.equal(original.type, 'user_message');
  const peer = new OpenAI({apiKey: config.other, baseURL: config.base, maxRetries: 0});
  await assert.rejects(peer.beta.chatkit.threads.retrieve(thread), e => e.status === 404);
  // The isolated acceptance driver exposes its object directory only to this
  // probe. Check the cancelled generation before a later turn can publish
  // a standalone item for an already materialized attachment.
  const names = readdirSync(config.privateObjects);
  const matches = names.filter(name => name.includes('.response.resp_') && name.endsWith('.json'))
    .map(name => readFileSync(join(config.privateObjects, name)))
    .filter(bytes => {
      const saved = JSON.parse(bytes).record.data;
      return saved.chatkit_thread_id === thread && saved.response.output.some(row => row.id === item);
    });
  assert.equal(matches.length, 1);
  const saved = JSON.parse(matches[0]).record;
  assert.equal(saved.data.response.status, 'cancelled');
  assert.equal(saved.data.own_input.length, 1);
  const canonical = saved.data.own_input[0].id;
  assert.ok(!names.includes(saved.owner + '.item.' + canonical + '.json'));
  const interruptedSha256 = createHash('sha256').update(matches[0]).digest('hex');
  await owner.files.delete(publication);
  await assert.rejects(owner.files.content(publication), e => e.status === 404);
  const retriedStream = await send(fresh, 'threads.retry_after_item', {
    thread_id: thread, item_id: original.id,
  });
  const retryEvents = (await retriedStream.text()).split(/\r?\n/)
    .filter(line => line.startsWith('data: ')).map(line => JSON.parse(line.slice(6)));
  assert.ok(!retryEvents.some(e => e.type === 'error'));
  const done = retryEvents.filter(e => e.type === 'thread.item.done' && e.item.type === 'assistant_message');
  assert.equal(done.length, 1);
  const replacement = done[0].item;
  assert.notEqual(replacement.id, item);
  const text = replacement.content.map(part => part.text).join('').trim();
  const number = text.match(/^([0-9]+)\b/);
  assert.equal(number?.[1], String(revision), text);
  const history = await list();
  assert.deepEqual(history.data.map(row => row.id), [original.id, replacement.id]);
  const persisted = await owner.beta.chatkit.threads.listItems(thread, {order: 'asc'});
  assert.deepEqual(persisted.data.map(row => row.id), [original.id, replacement.id]);
  continuedFrom = history;
  retried = {user: original.id, assistant: replacement.id, text, revision,
             file: publication, source_deleted: 404, source_sha256: config.publicationSha256,
             obsolete: [item], canonical, interruptedSha256, peer: 404,
             events: retryEvents.length};
}

const next = await send(fresh, 'threads.add_user_message', {
  thread_id: thread, ...message('Reply with exactly the decimal number: what is 6 times 7?'),
});
const events = (await next.text()).split(/\r?\n/).filter(line => line.startsWith('data: '))
  .map(line => JSON.parse(line.slice(6)));
const answer = events.find(event => event.type === 'thread.item.done' && event.item.type === 'assistant_message')?.item;
assert.equal(answer?.content[0].text.trim(), '42', JSON.stringify(events));
const after = await list();
assert.deepEqual(after.data.slice(0, continuedFrom.data.length), continuedFrom.data);
assert.deepEqual(after.data.map(row => row.type), ['user_message', 'assistant_message', 'user_message', 'assistant_message']);

const chat = await owner.chat.completions.create({
  model: config.model, messages: [{role: 'user', content: 'Compute 17 + 25. Reply only with the decimal integer.'}],
  temperature: 0, max_tokens: 32, chat_template_kwargs: {enable_thinking: false},
});
assert.equal(chat.choices[0].message.content.trim(), '42');
process.stdout.write(JSON.stringify({
  thread, item, excerpt, deltaCount, observed, next: answer.id, continued: answer.content[0].text.trim(),
  visible: after.data.map(row => row.id), options, retried, sdk: version, independent_chat: '42',
}));
