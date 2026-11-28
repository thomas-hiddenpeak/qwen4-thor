// Exchange the same stored project response through the official Node resource.
import assert from 'node:assert/strict';
import {appendFileSync, readFileSync, realpathSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const plan = JSON.parse(readFileSync(0, 'utf8'));
const root = realpathSync(process.argv[2]);
const {version, name} = JSON.parse(readFileSync(resolve(root, 'package.json')));
assert.equal(name, 'openai');
const {default: OpenAI} = await import(pathToFileURL(resolve(root, 'index.mjs')));
const base = new URL(plan.base);
assert.equal(base.protocol, 'https:');
assert.equal(base.hostname, '127.0.0.1');
assert.equal(base.pathname, '/v1');
const requests = [];
function resource(apiKey) {
  const api = new OpenAI({apiKey, baseURL: plan.base, maxRetries: 0, timeout: 45000,
    fetch: async (url, options) => {
      if (url === 'data:,') return fetch(url, options);
      const req = new Request(url, options);
      const target = new URL(req.url);
      assert.equal(target.origin, base.origin);
      const reply = await fetch(req);
      const record = {method: req.method, path: target.pathname, query: target.search.slice(1),
        status: reply.status, trace: reply.headers.get('x-request-id')};
      appendFileSync(plan.capture, JSON.stringify(record)+'\n');
      requests.push(record);
      return reply;
    }});
  return (plan.beta ? api.beta : api).responses;
}

function summary(response) {
  const text = response.output.filter(item => item.type === 'message')
    .flatMap(item => item.content.filter(p => p.type === 'output_text').map(p => p.text)).join('');
  return {id:response.id, status:response.status, text, metadata:response.metadata,
    previous:response.previous_response_id ?? null};
}
async function capture(stream) {
  const events = [];
  for await (const e of stream) {
    const event = {seq:e.sequence_number, type:e.type};
    if (e.type === 'response.output_text.delta') event.delta = e.delta;
    if (e.type === 'response.completed') event.response = summary(e.response);
    events.push(event);
  }
  return events;
}
async function missing(fn) {
  try { await fn(); }
  catch (e) {
    assert.ok(e instanceof OpenAI.NotFoundError);
    return {status:e.status, code:e.code};
  }
  throw new Error('expected an inaccessible response');
}

const owner = resource(plan.key);
let result;
if (plan.action === 'write') {
  const {input, model} = plan.request;
  const count = (await owner.inputTokens.count({model, input})).input_tokens;
  const events = await capture(await owner.create({...plan.request, stream:true}));
  result = {count, created:events.at(-1).response, events};
} else {
  assert.equal(plan.action, 'read');
  const id = plan.id;
  const before = summary(await owner.retrieve(id));
  const foreign = await missing(() => resource(plan.peer).retrieve(id));
  const pages = [];
  const query = {limit:1, order:'asc'};
  for (let i = 0; i < 2; i++) {
    const page = await owner.inputItems.list(id, query);
    assert.equal(page.data.length, 1);
    const item = page.data[0];
    pages.push({id:item.id, role:item.role, text:item.content.map(p => p.text), more:page.has_more});
    query.after = item.id;
  }
  const follow = summary(await owner.create({...plan.followup, previous_response_id:id}));
  const events = await capture(await owner.retrieve(id, {stream:true}));
  await owner.delete(id);
  const gone = await missing(() => owner.retrieve(id));
  result = {before, foreign, pages, follow, events, gone};
}
console.log(JSON.stringify({sdk:'node-'+version, beta:plan.beta, requests, ...result}));
