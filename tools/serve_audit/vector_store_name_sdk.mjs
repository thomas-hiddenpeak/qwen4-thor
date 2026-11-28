import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {setTimeout} from 'node:timers/promises';
import {pathToFileURL} from 'node:url';

const config = JSON.parse(readFileSync(0, 'utf8'));
const root = resolve(process.argv[2]);
const {default: OpenAI} = await import(pathToFileURL(resolve(root, 'index.mjs')));
const {name, version} = JSON.parse(readFileSync(resolve(root, 'package.json')));
const origin = new URL(config.base);
assert.equal(name, 'openai');
assert.equal(origin.hostname, '127.0.0.1');
assert.equal(origin.protocol, 'https:');
const requests = [];
async function observe(input, init) {
  const request = new Request(input, init);
  const url = new URL(request.url);
  assert.equal(url.origin, origin.origin);
  const body = request.body ? await request.clone().json() : null;
  const reply = await fetch(request);
  const payload = await reply.clone().json();
  const matching = (payload.data ?? [payload]).filter(row => row.id === config.store);
  assert.equal(matching.length, 1);
  assert.ok(Object.hasOwn(matching[0], 'name'));
  requests.push({method: request.method, path: url.pathname, query: url.search.slice(1),
    body, status: reply.status, trace: reply.headers.get('x-request-id'), name: matching[0].name});
  await setTimeout(125);
  return reply;
}
const stores = new OpenAI({baseURL: config.base, apiKey: config.key, fetch: observe,
  maxRetries: 0, timeout: 25000}).vectorStores;
const id = config.store;
const renamed = await stores.update(id, {name: 'Thor 项目资料'});
const cleared = await stores.update(id, {name: null});
const retrieved = await stores.retrieve(id);
const listed = (await stores.list({limit: 100})).data.filter(row => row.id === id);
assert.equal(listed.length, 1);
console.log(JSON.stringify({sdk: 'node-' + version, store: id, requests,
  names: [renamed, cleared, retrieved, listed[0]].map(row => row.name)}));
