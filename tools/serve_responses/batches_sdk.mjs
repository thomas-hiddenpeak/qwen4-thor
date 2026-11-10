// Pinned SDK over real native HTTP/TLS, task ownership and disk. Model output
// is an explicitly identified CPU fixture; no model-quality assertion.
import assert from 'node:assert/strict';
import { writeFileSync } from 'node:fs';
import { loadSdk } from '../serve_protocol/sdk.mjs';
const [baseURL, key, otherKey, output] = process.argv.slice(2);
assert.ok(baseURL && key && otherKey && output);
assert.ok(['127.0.0.1','localhost'].includes(new URL(baseURL).hostname));
const { OpenAI, version } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
const requests = [];
let passed = false;
process.on('exit', () => {
  if (!passed) writeFileSync(output,JSON.stringify({status:'FAIL',sdk_version:version,
    requests,request_count:requests.length,model_starts:0,generation:'explicit_CPU_fixture'},null,2)+'\n');
});
const tracedFetch = async (url, options) => {
  const response = await fetch(url, options);
  const row = {method:options.method ?? 'GET', path:new URL(url).pathname, status:response.status};
  if (response.headers.get('content-type')?.includes('application/json')) row.response = await response.clone().json();
  requests.push(row);
  return response;
};
const make = apiKey => new OpenAI({baseURL, apiKey, maxRetries:0, timeout:15000, fetch:tracedFetch});
const client = make(key), other = make(otherKey);
const upload = async hold => {
  const lines = [0,1].map(index => ({custom_id:`cpu-${index}`, method:'POST', url:'/v1/responses',
    body:{model:'batch-cpu', input:'CPU protocol fixture', max_output_tokens:16,
      reasoning:{effort:'none'}, ...(hold ? {metadata:{fixture:'hold'}} : {})}}));
  return client.files.create({file:new File([lines.map(v=>JSON.stringify(v)).join('\n')+'\n'], 'requests.jsonl'), purpose:'batch'});
};
const wait = async (id, statuses) => {
  for (let turn=0;turn<60;++turn) {
    const batch = await client.batches.retrieve(id);
    if (statuses.includes(batch.status)) return batch;
    // Observation has its own real 10 rps limit, independent of data burst.
    await new Promise(resolve=>setTimeout(resolve,125));
  }
  throw new Error('Original native Batch did not reach expected status');
};
const file = await upload(false);
const created = await client.batches.create({input_file_id:file.id, endpoint:'/v1/responses', completion_window:'24h',
  metadata:{fixture:'real-api'}, output_expires_after:{anchor:'created_at',seconds:3600}});
assert.equal(created.object,'batch');assert.equal(created.status,'validating');
await assert.rejects(other.batches.retrieve(created.id),e=>e.status===404);
await assert.rejects(other.batches.cancel(created.id),e=>e.status===404);
assert.equal((await client.files.delete(file.id)).deleted,true);
const completed = await wait(created.id,['completed','failed']);
assert.equal(completed.status,'completed');assert.equal(completed.model,'batch-cpu');
assert.deepEqual(completed.request_counts,{total:2,completed:2,failed:0});
assert.equal(completed.expires_at,created.expires_at);
assert.deepEqual(completed.usage,{input_tokens:8,output_tokens:4,total_tokens:12,
  input_tokens_details:{cached_tokens:0},output_tokens_details:{reasoning_tokens:0}});
const outputFile = await client.files.retrieve(completed.output_file_id);
assert.equal(outputFile.purpose,'batch_output');assert.equal(outputFile.expires_at-outputFile.created_at,3600);
const rows = (await (await client.files.content(outputFile.id)).text()).trim().split('\n').map(JSON.parse);
assert.deepEqual(rows.map(row=>row.custom_id),['cpu-0','cpu-1']);
for (const row of rows) {
  assert.equal(row.response.status_code,200);
  assert.equal(row.response.body.output[0].type,'function_call');
  assert.deepEqual(JSON.parse(row.response.body.output[0].arguments),{key:42});
  assert.deepEqual(row.response.body.usage,{input_tokens:4,output_tokens:2,total_tokens:6,
    input_tokens_details:{cached_tokens:0},output_tokens_details:{reasoning_tokens:0}});
  assert.equal(row.error,null);
}
const heldFile = await upload(true);
const held = await client.batches.create({input_file_id:heldFile.id, endpoint:'/v1/responses', completion_window:'24h'});
await wait(held.id,['in_progress']);
assert.equal((await client.batches.cancel(held.id)).status,'cancelling');
const cancelled = await wait(held.id,['cancelled']);
assert.equal((await client.batches.cancel(held.id)).status,'cancelled');
assert.equal(cancelled.request_counts.total,2);
const first = await client.batches.list({limit:1});
assert.equal(first.data.length,1);assert.equal(first.has_more,true);
const second = await first.getNextPage();
assert.equal(second.data.length,1);assert.notEqual(first.data[0].id,second.data[0].id);
assert.equal(second.has_more,false);
assert.equal((await other.batches.list()).data.length,0);
await assert.rejects(client.batches.list({limit:0}),e=>e.status===400);
await assert.rejects(make('wrong').batches.list(),e=>e.status===401);
assert.ok(requests.length<=128);
writeFileSync(output,JSON.stringify({status:'PASS',sdk_version:version,requests,request_count:requests.length,
  actual_network:true,model_starts:0,generation:'explicit_CPU_fixture',max_retries:0,rows},null,2)+'\n');
passed = true;
console.log(JSON.stringify({status:'PASS',requests:requests.length}));
