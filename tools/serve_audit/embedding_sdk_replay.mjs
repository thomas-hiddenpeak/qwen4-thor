import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const input = JSON.parse(readFileSync(0, 'utf8'));
const root = resolve(process.argv[2]);
const {name, version} = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8'));
assert.equal(name, 'openai');
const {default: OpenAI} = await import(pathToFileURL(resolve(root, 'index.mjs')));
const formats = [];
async function replay(url, init) {
  const request = new Request(url, init);
  assert.equal(request.method, 'POST');
  assert.equal(request.url, 'https://replay.invalid/v1/embeddings');
  const body = await request.json();
  assert.deepEqual(body, input.request);
  formats.push(body.encoding_format);
  return new Response(readFileSync(input.wire), {headers: {'content-type': 'application/json'}});
}

const client = new OpenAI({baseURL: 'https://replay.invalid/v1', apiKey: 'fixture', fetch: replay, maxRetries: 0});
const response = await client.embeddings.create(input.arguments);
const rows = response.data.map(({index, embedding}) => {
  assert.ok(Array.isArray(embedding) || embedding instanceof Float32Array);
  const bytes = Buffer.alloc(embedding.length * 4);
  embedding.forEach((v, i) => {
    assert.equal(typeof v, 'number');
    bytes.writeFloatLE(v, 4 * i);
  });
  return {index, sha256: createHash('sha256').update(bytes).digest('hex')};
});
console.log(JSON.stringify({sdk: 'node-' + version, wire_formats: formats, rows}));
