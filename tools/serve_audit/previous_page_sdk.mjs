import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {setTimeout} from 'node:timers/promises';
import {pathToFileURL} from 'node:url';

const config = JSON.parse(readFileSync(0, 'utf8'));
const root = resolve(process.argv[2]);
const {default: OpenAI} = await import(pathToFileURL(resolve(root, 'index.mjs')));
const {name, version} = JSON.parse(readFileSync(resolve(root, 'package.json')));
assert.equal(name, 'openai');
const origin = new URL(config.base);
assert.equal(origin.hostname, '127.0.0.1');
assert.equal(origin.protocol, 'https:');
const requests = [];
async function observe(input, init) {
  const request = new Request(input, init);
  const target = new URL(request.url);
  assert.equal(target.origin, origin.origin);
  const reply = await fetch(request);
  requests.push({method: request.method, path: target.pathname, query: target.search.slice(1),
    status: reply.status, trace: reply.headers.get('x-request-id')});
  await setTimeout(125);
  return reply;
}
const client = new OpenAI({baseURL: config.base, apiKey: config.key, fetch: observe,
  maxRetries: 0, timeout: 25000});
const stores = client.vectorStores;
const resources = {
  stores: query => stores.list(query),
  files: query => stores.files.list(config.store, {...query, filter: 'completed'}),
  batch_files: query => stores.fileBatches.listFiles(config.batch,
    {...query, vector_store_id: config.store, filter: 'completed'}),
};
const pages = [];
for (const [resource, list] of Object.entries(resources)) {
  for (const order of ['asc', 'desc']) {
    const ids = [...config[resource === 'stores' ? 'stores' : 'files']];
    if (order === 'desc') ids.reverse();
    let cursor = ids.at(-1);
    for (const end of [4, 2]) {
      const page = await list({order, before: cursor, limit: 2});
      const values = page.data.map(item => item.id);
      assert.deepEqual(values, ids.slice(end - 2, end));
      assert.deepEqual([page.body.first_id, page.body.last_id, page.has_more],
        [values[0], values[1], end > 2]);
      pages.push({resource, order, before: cursor, ids: values, first: page.body.first_id,
        last: page.body.last_id, more: page.has_more});
      cursor = values[0];
    }
  }
}
console.log(JSON.stringify({sdk: 'node-' + version, pages, requests}));
