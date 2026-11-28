// Exercise the same published API on a stock Node SDK.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const config = JSON.parse(readFileSync(0, 'utf8'));
const root = resolve(process.argv[2]);
const {name, version} = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8'));
assert.equal(name, 'openai');
const {default: OpenAI} = await import(pathToFileURL(resolve(root, 'index.mjs')));
const requests = [];
async function observe(input, init) {
  const request = new Request(input, init);
  const url = new URL(request.url);
  assert.equal(url.origin, new URL(config.base).origin);
  const result = await fetch(request);
  requests.push({method: request.method, path: url.pathname, query: url.search.slice(1),
    status: result.status, trace: result.headers.get('x-request-id')});
  return result;
}
const client = new OpenAI({baseURL: config.base, apiKey: config.key, fetch: observe,
  maxRetries: 0, timeout: 60000});
const catalogue = (await client.models.list()).data;
const selected = catalogue.find(row => row.id === config.model)?.id;
assert.ok(selected);
const model = await client.models.retrieve(selected);
await assert.rejects(client.models.retrieve('q4t-missing-business-model'),
  error => error instanceof OpenAI.NotFoundError && error.status === 404);
const body = {...config.completion, model: selected};
const completion = await client.completions.create(body);
const chunks = [];
for await (const chunk of await client.completions.create({...body, stream: true,
  stream_options: {include_usage: true}})) {
  chunks.push(chunk);
}
const embeddings = {};
for (const encoding of ['float', 'base64']) {
  embeddings[encoding] = await client.embeddings.create({model: selected, input: config.documents,
    dimensions: config.dimensions, encoding_format: encoding});
}
console.log(JSON.stringify({sdk: 'node-'+version, catalogue, model, completion, chunks, embeddings, requests}));
