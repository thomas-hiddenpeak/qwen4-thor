// Exercise the SDK's public uploadAndPoll helper against native TLS.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const config = JSON.parse(readFileSync(0, 'utf8'));
const sdk = resolve(process.argv[2]);
const {default: OpenAI, toFile} = await import(pathToFileURL(resolve(sdk, 'index.mjs')));
const {version} = JSON.parse(readFileSync(resolve(sdk, 'package.json')));
const target = new URL(config.base);
assert.equal(target.protocol, 'https:');
assert.equal(target.hostname, '127.0.0.1');
const requests = [];
async function observe(input, init) {
  const request = new Request(input, init);
  const url = new URL(request.url);
  if (url.protocol === 'data:') return fetch(request);
  assert.equal(url.origin, target.origin);
  const reply = await fetch(request);
  requests.push({method: request.method, path: url.pathname, query: url.search.slice(1),
                 status: reply.status, trace: reply.headers.get('x-request-id'),
                 helper: request.headers.get('x-stainless-poll-helper')});
  assert.ok(requests.length <= 75);
  return reply;
}
const owner = new OpenAI({apiKey: config.key, baseURL: config.base, fetch: observe,
                          maxRetries: 0, timeout: 40000});
const store = await owner.vectorStores.create({name: 'Project release records'});
const files = await Promise.all(Object.entries(config.documents).map(
  ([name, contents]) => toFile(Buffer.from(contents, 'base64'), name, {type: 'text/plain'})));
assert.equal(files.length, 2);
const batch = await owner.vectorStores.fileBatches.uploadAndPoll(store.id, {files},
    {pollIntervalMs: 500, maxConcurrency: 2});
assert.equal(batch.status, 'completed');
assert.deepEqual(batch.file_counts, {total: 2, completed: 2, in_progress: 0, failed: 0, cancelled: 0});
console.log(JSON.stringify({version, store: store.id, batch: batch.id, counts: batch.file_counts, requests}));
