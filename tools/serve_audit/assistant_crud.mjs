// Drive the official Assistants v2 API with the same sequence as Python.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {setTimeout} from 'node:timers/promises';

const config = JSON.parse(readFileSync(0, 'utf8'));
const root = resolve(process.argv[2]);
const {default: OpenAI} = await import(pathToFileURL(resolve(root, 'index.mjs')));
const {name, version} = JSON.parse(readFileSync(resolve(root, 'package.json')));
assert.equal(name, 'openai');
const base = new URL(config.base);
assert.equal(base.hostname, '127.0.0.1');
assert.equal(base.protocol, 'https:');
const requests = [];
async function observe(input, init) {
  const request = new Request(input, init);
  const url = new URL(request.url);
  assert.equal(url.origin, base.origin);
  const body = request.body ? await request.clone().json() : null;
  const reply = await fetch(request, {redirect: 'error'});
  requests.push({method:request.method, path:url.pathname, query:url.search.slice(1),
    trace:reply.headers.get('x-request-id'), beta:request.headers.get('openai-beta'),
    status:reply.status, body, response:await reply.clone().json()});
  await setTimeout(130);
  return reply;
}
const client = key => new OpenAI({baseURL:config.base, apiKey:key, fetch:observe,
                                   maxRetries:0, timeout:20000});
const clients = {owner:client(config.key), other:client(config.other)};
const ids = {};
for (const step of config.steps) {
  const before = requests.length;
  const [group, op] = step.call.split('.');
  const beta = clients[step.scope].beta;
  const api = {assistants:beta.assistants, threads:beta.threads, messages:beta.threads.messages}[group];
  const args = step.args.map(key => ids[key]);
  const fields = {...step.fields};
  if (step.parent) fields.thread_id = ids.thread;
  if (Object.keys(fields).length) args.push(fields);
  let value;
  try {
    const row = await api[op](...args);
    assert.equal(step.status, 200);
    if (step.save) ids[step.save] = row.id;
    value = op === 'list' ? {object:'list',ids:row.data.map(v => v.id)} : row;
  } catch (error) {
    assert.ok(error instanceof OpenAI.APIError && error.status === 404 && step.status === 404,
              step.name + ': ' + error.message);
    value = {status:error.status};
  }
  assert.equal(requests.length, before + 1);
  Object.assign(requests.at(-1), {step:step.name,scope:step.scope,sdk_value:value});
}
console.log(JSON.stringify({sdk:'node-'+version, ids, requests}));
