// Exercise the container lifecycle with an installed stock OpenAI Node SDK.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const config = JSON.parse(readFileSync(0, 'utf8'));
const root = resolve(process.argv[2]);
const {name, version} = JSON.parse(readFileSync(resolve(root, 'package.json')));
assert.equal(name, 'openai');
const {default: OpenAI, toFile} = await import(pathToFileURL(resolve(root, 'index.mjs')));
const requests = [];
let localUploadProbes = 0;
async function observe(input, init) {
  const request = new Request(input, init);
  const url = new URL(request.url);
  // The SDK detects FormData support with a local data URL before uploading.
  if (url.href === 'data:,' && request.method === 'GET') {
    localUploadProbes++;
    return fetch(request);
  }
  assert.equal(url.origin, new URL(config.base).origin);
  const response = await fetch(request);
  requests.push({method: request.method, path: url.pathname, query: url.search.slice(1),
    status: response.status, trace: response.headers.get('x-request-id')});
  return response;
}
function client(apiKey) {
  return new OpenAI({apiKey, baseURL: config.base, fetch: observe,
    maxRetries: 0, timeout: 30000}).containers;
}
function missing(result) {
  return assert.rejects(result, err => err instanceof OpenAI.NotFoundError && err.status === 404);
}
const api = client(config.key);
const peer = client(config.peer);
const doc = readFileSync(config.document);
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const box = await api.create({name: 'q4t-sdk-publication'});
assert.equal(box.object, 'container');
assert.equal(box.status, 'running');
assert.ok((await api.list({limit: 100})).data.some(item => item.id === box.id));
assert.equal((await api.retrieve(box.id)).name, box.name);
const lookup = {container_id: box.id};
const file = await api.files.create(box.id, {file: await toFile(doc, 'release.json',
  {type: 'application/json'})});
const note = await api.files.create(box.id, {file: await toFile(Buffer.from(digest(doc)),
  'sha256.txt', {type: 'text/plain'})});
const files = [file.id, note.id];
const first = await api.files.list(box.id, {limit: 1, order: 'asc'});
assert.equal(first.data.length, 1);
assert.ok(first.hasNextPage());
const second = await first.getNextPage();
assert.equal(second.data.length, 1);
assert.equal(second.hasNextPage(), false);
const pages = [first.data[0].id, second.data[0].id];
assert.equal(new Set(files).size, 2);
assert.deepEqual(new Set(pages), new Set(files));
const saved = await api.files.retrieve(file.id, lookup);
assert.deepEqual([saved.container_id, saved.path, saved.bytes],
  [box.id, '/mnt/data/release.json', doc.length]);
const download = await api.files.content.retrieve(file.id, lookup);
const bytes = Buffer.from(await download.arrayBuffer());
assert.deepEqual(bytes, doc);
await missing(peer.retrieve(box.id));
await missing(peer.files.content.retrieve(file.id, lookup));
await api.files.delete(file.id, lookup);
await missing(api.files.retrieve(file.id, lookup));
await api.delete(box.id);
await missing(api.retrieve(box.id));
console.log(JSON.stringify({language: 'node', version, container: box.id, files, pages,
  bytes: bytes.length, sha256: digest(bytes), localUploadProbes, requests}));
