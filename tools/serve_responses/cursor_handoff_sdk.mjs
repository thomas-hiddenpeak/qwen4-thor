// Exercise both the SDK page iterator and the response delivered over HTTPS.
import assert from 'node:assert/strict';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const {base, key, other, sdk, previous} = JSON.parse(input);
const url = new URL(base);
assert.equal(url.protocol, 'https:');
assert.equal(url.hostname, '127.0.0.1');
const {OpenAI, version} = loadSdk(sdk);
let requests = 0;
let uploadProbes = 0;
let polls = 0;
let pollingPath;
const limit = previous ? 11 : 16;
const pollLimit = previous ? 1 : 25;
function countedFetch(...args) {
  const target = new URL(args[0] instanceof Request ? args[0].url : args[0]);
  if (target.href === 'data:,') {
    assert.ok(!previous && ++uploadProbes === 1, 'unexpected SDK upload probe');
  } else {
    assert.equal(target.origin, url.origin);
    if (target.pathname === pollingPath && args[1]?.method === 'GET') {
      assert.ok(++polls <= pollLimit, 'file status polling budget exceeded');
    } else {
      assert.ok(++requests <= limit, 'cursor client exceeded its request budget');
    }
  }
  return fetch(...args);
}
const client = apiKey => new OpenAI({baseURL:base, apiKey, maxRetries:0, timeout:20000,
  fetch:countedFetch});
const owner = client(key);
const peer = client(other);
const pages = {};

async function page(name, request, ids, cursor = '') {
  const {data, response} = await request.withResponse();
  assert.equal(response.status, 200);
  assert.equal(new URL(response.url).origin, url.origin);
  assert.equal(data.hasNextPage(), false);
  const body = data.body;
  assert.deepEqual(body.data.map(item => item.id), ids);
  assert.deepEqual(data.data.map(item => item.id), ids);
  assert.equal(body.object, 'list');
  assert.equal(body.has_more, false);
  assert.equal(body.first_id, ids.at(0) ?? cursor);
  assert.equal(body.last_id, ids.at(-1) ?? cursor);
  pages[name] = [body.first_id, body.last_id];
}

await page('batches', owner.batches.list(), []);
await page('consents', owner.audio.voiceConsents.list(), [], null);
await page('peer_stores', peer.vectorStores.list(), []);
let store, file, batch;
if (previous) {
  ({store, file, batch} = previous);
} else {
  await page('initial_stores', owner.vectorStores.list(), []);
  store = (await owner.vectorStores.create({name:'Pagination qualification'})).id;
  await page('initial_files', owner.vectorStores.files.list(store), []);
  file = (await owner.files.create({file:new File(['q4t pagination qualification\n'],
    'project.txt'), purpose:'user_data'})).id;
  batch = (await owner.vectorStores.fileBatches.create(store, {file_ids:[file]})).id;
}
const vector = owner.vectorStores;
pollingPath = url.pathname.replace(/\/$/, '') + '/vector_stores/' + store + '/file_batches/' + batch;
let state;
for (let i = 0; i < pollLimit; i++) {
  state = await vector.fileBatches.retrieve(batch, {vector_store_id:store});
  if (state.status !== 'in_progress') break;
  if (i + 1 < pollLimit) await new Promise(resolve => setTimeout(resolve, 800));
}
assert.equal(state.status, 'completed', JSON.stringify(state.file_counts));
assert.equal(state.file_counts.completed, 1);
const search = await vector.search(store, {query:'q4t pagination qualification', max_num_results:1});
assert.equal(search.data[0]?.file_id, file);
assert.ok(search.data[0].content.some(part => part.text?.includes('q4t pagination qualification')));
await page('owned_stores', vector.list(), [store]);
await page('files', vector.files.list(store), [file]);
await page('filtered_files', vector.files.list(store, {filter:'cancelled'}), []);
const options = {vector_store_id:store};
await page('members', vector.fileBatches.listFiles(batch, options), [file]);
await page('filtered_members', vector.fileBatches.listFiles(batch, {...options, filter:'cancelled'}), []);
await assert.rejects(peer.vectorStores.files.list(store), error => error.status === 404);
await assert.rejects(peer.vectorStores.fileBatches.listFiles(batch, options), error => error.status === 404);
assert.equal(requests, limit);
assert.equal(uploadProbes, previous ? 0 : 1);
process.stdout.write(JSON.stringify({sdk:version, store, file, batch, pages, requests, polls, indexed_file:search.data[0].file_id, upload_probes:uploadProbes, foreign_404:2}));
