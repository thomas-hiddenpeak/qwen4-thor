// Abort a real generation, reopen the thread with a renewed user grant, and
// compare the partial answer from the browser and management representations.
import assert from 'node:assert/strict';
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
});
const message = text => ({
  input: {attachments: [], inference_options: {}, content: [{type: 'input_text', text}]},
});
async function send(session, type, params, signal = AbortSignal.timeout(45000)) {
  const response = await fetch(config.base + '/chatkit', {
    method: 'POST', signal, body: JSON.stringify({type, params}),
    headers: {Authorization: 'Bearer ' + session.client_secret, 'Content-Type': 'application/json'},
  });
  if (response.status !== 200) throw Error(type + ' ' + response.status + ' ' + await response.text());
  return response;
}

const controller = new AbortController();
const response = await send(await grant(), 'threads.create', message(
  'Write a numbered list of thirty observations about software testing, with one ordinary sentence per line.',
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
const next = await send(fresh, 'threads.add_user_message', {
  thread_id: thread, ...message('Reply with exactly the decimal number: what is 6 times 7?'),
});
const events = (await next.text()).split(/\r?\n/).filter(line => line.startsWith('data: '))
  .map(line => JSON.parse(line.slice(6)));
const answer = events.find(event => event.type === 'thread.item.done' && event.item.type === 'assistant_message')?.item;
assert.equal(answer?.content[0].text.trim(), '42', JSON.stringify(events));
const after = await list();
assert.deepEqual(after.data.slice(0, before.data.length), before.data);
assert.deepEqual(after.data.map(row => row.type), ['user_message', 'assistant_message', 'user_message', 'assistant_message']);

const chat = await owner.chat.completions.create({
  model: config.model, messages: [{role: 'user', content: 'Compute 17 + 25. Reply only with the decimal integer.'}],
  temperature: 0, max_tokens: 32, chat_template_kwargs: {enable_thinking: false},
});
assert.equal(chat.choices[0].message.content.trim(), '42');
process.stdout.write(JSON.stringify({
  thread, item, excerpt, deltaCount, observed, next: answer.id, continued: answer.content[0].text.trim(),
  visible: after.data.map(row => row.id), options, sdk: version, independent_chat: '42',
}));
