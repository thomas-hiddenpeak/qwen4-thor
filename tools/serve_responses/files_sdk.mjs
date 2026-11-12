// Actual official SDK network calls to production Files handlers and storage.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { loadSdk } from '../serve_protocol/sdk.mjs';

const [baseURL, key, otherKey, output] = process.argv.slice(2);
assert.ok(baseURL && key && otherKey && output);
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(baseURL).hostname));
const { OpenAI, version } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
const requests = [];
const tracedFetch = async (url, options) => {
  const response = await fetch(url, options);
  requests.push({ method: options.method ?? 'GET',
    path: new URL(url).pathname, status: response.status });
  return response;
};
const make = apiKey => new OpenAI({ baseURL, apiKey, maxRetries: 0,
  timeout: 30000, fetch: tracedFetch });
const client = make(key), other = make(otherKey);
const bytes = Uint8Array.from({ length: 256 }, (_, index) => index);
const rows = [];
for (const purpose of ['assistants', 'batch', 'fine-tune', 'vision', 'user_data', 'evals']) {
  const file = await client.files.create({ purpose,
    file: new File([purpose === 'batch' || purpose === 'fine-tune' ? '{}\n' : bytes],
      purpose === 'batch' || purpose === 'fine-tune' ? '记录.jsonl' : '资料.bin') });
  assert.equal(file.object, 'file');
  assert.equal(file.purpose, purpose);
  assert.equal(file.status, 'processed');
  if (purpose === 'batch') assert.equal(file.expires_at-file.created_at, 2592000);
  else assert.equal(file.expires_at, undefined);
  rows.push(file);
}
const empty = await client.files.create({ file: new File([], 'empty.txt'),
  purpose: 'user_data', expires_after: { anchor: 'created_at', seconds: 3600 } });
rows.push(empty);
assert.equal(empty.bytes, 0);
assert.equal(empty.expires_at-empty.created_at, 3600);
assert.equal((await (await client.files.content(empty.id)).arrayBuffer()).byteLength, 0);
const first = await client.files.list({ limit: 2, order: 'asc' });
assert.equal(first.data.length, 2);
assert.equal(first.has_more, true);
const second = await first.getNextPage();
assert.equal(second.data.length, 2);
assert.notEqual(second.data[0].id, first.data[0].id);
const filtered = await client.files.list({ purpose: 'evals', limit: 1 });
assert.equal(filtered.data.length, 1);
assert.equal(filtered.data[0].id, rows[5].id);
assert.equal(filtered.has_more, false);
assert.deepEqual(await client.files.retrieve(rows[0].id), rows[0]);
assert.deepEqual(new Uint8Array(await (await client.files.content(rows[0].id)).arrayBuffer()), bytes);
assert.equal((await other.files.list()).data.length, 0);
for (const action of ['retrieve', 'content', 'delete'])
  await assert.rejects(other.files[action](rows[0].id), error => error.status === 404);
await assert.rejects(client.files.list({ limit: 0 }), error => error.status === 400);
await assert.rejects(client.files.create({ file: new File([bytes], 'x.bin'), purpose: 'invalid' }),
  error => error.status === 400);
await assert.rejects(make('incorrect').files.list(), error => error.status === 401);

// The client may own its fixture bytes; the server must stream beyond 16 MiB.
const large = Buffer.alloc(17 * 1024 * 1024 + 13);
for (let index = 0; index < large.length; ++index) large[index] = index % 251;
const expected = createHash('sha256').update(large).digest('hex');
const big = await client.files.create({ file: new File([large], 'large.bin'), purpose: 'user_data' });
rows.push(big);
assert.equal(big.bytes, large.length);
const downloaded = await client.files.content(big.id);
assert.equal(Number(downloaded.headers.get('content-length')), large.length);
const digest = createHash('sha256');
let length = 0;
for await (const chunk of downloaded.body) { length += chunk.length; digest.update(chunk); }
assert.equal(length, large.length);
assert.equal(digest.digest('hex'), expected);
for (const file of rows) {
  const deleted = await client.files.delete(file.id);
  assert.deepEqual(deleted, { id: file.id, object: 'file', deleted: true });
}
assert.equal((await client.files.list()).data.length, 0);
await assert.rejects(client.files.retrieve(big.id), error => error.status === 404);
const result = { status: 'PASS_FILES_SDK_NETWORK', sdk_version: version,
  requests, large_file_bytes: length, large_file_sha256: expected,
  model_starts: 0, model_generations: 0, transport: new URL(baseURL).protocol };
writeFileSync(output, JSON.stringify(result, null, 2)+'\n', { flag: 'wx', mode: 0o600 });
process.stdout.write(JSON.stringify({ status: result.status, requests: requests.length,
  large_file_bytes: length, sdk_version: version })+'\n');
