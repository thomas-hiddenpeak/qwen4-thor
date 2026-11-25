// Check empty and nonempty pages through the installed SDK on an isolated service.
import assert from 'node:assert/strict';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.equal(base.protocol, 'https:');
assert.equal(base.hostname, '127.0.0.1');
const {OpenAI, version} = loadSdk(plan.sdk);
const client = new OpenAI({baseURL:base.href, apiKey:plan.key, maxRetries:0,
  timeout:60000});
const threads = client.beta.threads;
const {messages, runs} = threads;
const assistants = client.beta.assistants;
const endpoints = {
  assistants:'/assistants', messages:'/threads/{thread_id}/messages',
  runs:'/threads/{thread_id}/runs', steps:'/threads/{thread_id}/runs/{run_id}/steps',
  chatkit:'/chatkit/threads',
};
const wire = [];

async function page(request, path, empty, ids = {}) {
  const response = await request.asResponse();
  assert.equal(response.status, 200);
  const actual = new URL(response.url);
  const route = path.replace(/\{(\w+)\}/g, (_, name) => {
    assert.equal(typeof ids[name], 'string');
    return encodeURIComponent(ids[name]);
  });
  assert.equal(actual.origin, base.origin);
  assert.equal(actual.pathname, base.pathname.replace(/\/$/, '') + route);
  assert.equal(response.headers.get('content-type')?.split(';')[0], 'application/json');
  const body = await response.json();
  assert.equal(body.object, 'list');
  assert.equal(body.has_more, false);
  assert.ok(Array.isArray(body.data));
  if (empty) {
    assert.equal(body.data.length, 0);
    const cursor = path === endpoints.chatkit ? null : '';
    assert.equal(body.first_id, cursor);
    assert.equal(body.last_id, cursor);
  } else {
    assert.ok(body.data.length > 0);
    assert.equal(body.first_id, body.data[0].id);
    assert.equal(body.last_id, body.data.at(-1).id);
  }
  wire.push({path, method:'get', status:response.status, value:body});
  return body;
}

await page(assistants.list(), endpoints.assistants, true);
await page(client.beta.chatkit.threads.list(), endpoints.chatkit, true);
const assistant = await assistants.create({model:plan.model, temperature:0,
  reasoning_effort:'none', instructions:'Answer arithmetic with only the integer.'});
const thread = await threads.create();
const options = {thread_id:thread.id};
await page(messages.list(thread.id), endpoints.messages, true, options);
await page(runs.list(thread.id), endpoints.runs, true, options);
await messages.create(thread.id, {role:'user', content:'Return only the product of 6 and 7.'});
const run = await runs.createAndPoll(thread.id, {assistant_id:assistant.id},
  {pollIntervalMs:300});
assert.equal(run.status, 'completed');
const ids = {...options, run_id:run.id};
const calls = [
  [query => assistants.list(query), endpoints.assistants],
  [query => messages.list(thread.id, query), endpoints.messages],
  [query => runs.list(thread.id, query), endpoints.runs],
  [query => runs.steps.list(run.id, {...options, ...query}), endpoints.steps],
];
for (const [list, path] of calls) {
  const entries = await page(list({order:'asc'}), path, false, ids);
  if (path === endpoints.messages)
    assert.equal(entries.data.at(-1).content[0].text.value.trim(), '42');
  await page(list({order:'asc', after:entries.last_id}), path, true, ids);
}
process.stdout.write(JSON.stringify({sdk:version, answer:'42', wire}));
