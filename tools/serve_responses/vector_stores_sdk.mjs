// Real official SDK/HTTP/storage. Embedding math is the explicit CPU fixture.
import assert from 'node:assert/strict';
import { writeFileSync } from 'node:fs';
import { loadSdk } from '../serve_protocol/sdk.mjs';
const [baseURL, key, otherKey, output] = process.argv.slice(2);
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(baseURL).hostname));
const { OpenAI, version } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
const requests = [];
let status = 'FAIL';
process.on('exit', () => writeFileSync(output, JSON.stringify({status,
  sdk_version:version, requests, model_starts:0,
  vectors:'explicit_CPU_math_fixture', actual_network:true, max_retries:0}, null, 2)+'\n'));
const tracedFetch = async (url, options) => {
  const reply = await fetch(url, options);
  const row = {method:options.method ?? 'GET', path:new URL(url).pathname,
    status:reply.status, response:await reply.clone().json()};
  if (typeof options.body === 'string') row.request = JSON.parse(options.body);
  requests.push(row);
  return reply;
};
const make = apiKey => new OpenAI({baseURL, apiKey, maxRetries:0, timeout:15000, fetch:tracedFetch});
const client = make(key), other = make(otherKey);
const text = 'Project record: release 80 preserves source-bound document retrieval.\n';
const upload = name => client.files.create({file:new File([text], name), purpose:'user_data'});
const a = await upload('STATUS.md'), b = await upload('release.md');
const v = client.vectorStores;
const store = await v.create({name:'项目记录', file_ids:[a.id]});
const second = await v.create({name:'第二库'});
assert.equal((await v.update(store.id, {metadata:{stage:'80'}})).metadata.stage, '80');
assert.equal((await v.retrieve(store.id)).name, '项目记录');
await assert.rejects(other.vectorStores.retrieve(store.id), e => e.status === 404);
await v.files.create(store.id, {file_id:b.id, attributes:{release:80, stable:true}});
const wait = async (get, states) => {
  for (let i = 0; i < 25; ++i) {
    const value = await get();
    if (states.includes(value.status)) return value;
    assert.equal(value.status, 'in_progress');
    await new Promise(resolve => setTimeout(resolve,150));
  }
  throw Error('Native ingestion did not settle');
};
await wait(() => v.files.retrieve(a.id, {vector_store_id:store.id}), ['completed']);
await wait(() => v.files.retrieve(b.id, {vector_store_id:store.id}), ['completed']);
assert.deepEqual((await v.files.content(a.id, {vector_store_id:store.id})).data,
  [{type:'text', text}]);
assert.equal((await v.files.update(b.id, {vector_store_id:store.id,
  attributes:{release:81}})).attributes.release,81);
let page = await v.files.list(store.id, {limit:1, order:'asc', filter:'completed'});
assert.equal(page.data.length,1);assert.equal(page.has_more,true);
assert.equal((await page.getNextPage()).data.length,1);
const batch = await v.fileBatches.create(second.id, {files:[{file_id:a.id},
  {file_id:b.id, attributes:{release:80}}]});
assert.equal(batch.object,'vector_store.files_batch');
await wait(() => v.fileBatches.retrieve(batch.id, {vector_store_id:second.id}), ['completed']);
assert.equal((await v.fileBatches.listFiles(batch.id,
  {vector_store_id:second.id, filter:'completed'})).data.length,2);
assert.equal((await v.fileBatches.cancel(batch.id, {vector_store_id:second.id})).status,'completed');
page = await v.list({limit:1});
assert.equal(page.data.length,1);assert.equal(page.has_more,true);
assert.equal((await page.getNextPage()).data.length,1);
await assert.rejects(v.list({limit:0}), e => e.status === 400);
await assert.rejects(other.vectorStores.files.content(a.id,
  {vector_store_id:store.id}), e => e.status === 404);
assert.equal((await v.files.delete(a.id, {vector_store_id:store.id})).deleted,true);
assert.equal((await client.files.retrieve(a.id)).id,a.id);
await assert.rejects(v.files.content(a.id,{vector_store_id:store.id}),e=>e.status===404);
assert.equal((await v.delete(store.id)).deleted,true);
assert.equal((await v.delete(second.id)).deleted,true);
assert.equal((await client.files.delete(a.id)).deleted,true);
assert.equal((await client.files.delete(b.id)).deleted,true);
assert.ok(requests.length <= 100);
status = 'PASS';
console.log(JSON.stringify({status,requests:requests.length}));
