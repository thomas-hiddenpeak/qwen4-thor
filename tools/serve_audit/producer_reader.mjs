import assert from 'node:assert/strict';
import {readFileSync, realpathSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const root = realpathSync(process.argv[2]);
const plan = JSON.parse(readFileSync(0, 'utf8'));
const metadata = JSON.parse(readFileSync(resolve(root,'package.json')));
assert.equal(metadata.name, 'openai');
assert.equal(plan.sdk, 'node-' + metadata.version);
const {default: OpenAI} = await import(pathToFileURL(resolve(root,'index.mjs')));
const requests = [];
const options = {baseURL:plan.base, maxRetries:0, timeout:90000, fetch:async (url, init) => {
  if (url === 'data:,') return fetch(url, init);
  const req = new Request(url, init);
  const parsed = new URL(req.url);
  assert.equal(parsed.origin, new URL(plan.base).origin);
  const reply = await fetch(req);
  requests.push({method:req.method, path:parsed.pathname, query:parsed.search.slice(1),
                 status:reply.status, trace:reply.headers.get('x-request-id')});
  return reply;
}};
const client = new OpenAI({...options, apiKey:plan.key});
const container = await client.containers.create({name:'producer-sdk-read'});
const events = [];
for await (const event of await client.responses.create({...plan.request,
    tools:[{type:'code_interpreter',container:container.id}]})) events.push(event);
const response = events.at(-1).response;
const stored = await client.responses.retrieve(response.id,{include:['code_interpreter_call.outputs']});
const foreign = new OpenAI({...options, apiKey:plan.foreign});
await assert.rejects(() => foreign.responses.retrieve(response.id), error => error.status === 404);
console.log(JSON.stringify({sdk:plan.sdk, requests, container:container.id, events, stored}));
