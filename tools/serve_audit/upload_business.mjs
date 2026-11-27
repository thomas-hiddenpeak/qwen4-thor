// Issue a real multipart or cancellation request with the selected Node SDK.
import assert from 'node:assert/strict';
import {readFileSync, realpathSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const config = JSON.parse(readFileSync(0, 'utf8'));
const root = realpathSync(process.argv[2]);
const {version, name} = JSON.parse(readFileSync(resolve(root, 'package.json')));
assert.equal(name, 'openai');
assert.equal(version, config.version);
assert.equal(new URL(config.base).hostname, '127.0.0.1');
const {default: OpenAI} = await import(pathToFileURL(resolve(root, 'index.mjs')));
const wire = [];
const client = new OpenAI({baseURL: config.base, apiKey: config.key, maxRetries: 0,
  timeout: 15000, fetch: async (url, init) => {
    if (url === 'data:,') return fetch(url, init);
    const request = new Request(url, init);
    const target = new URL(request.url);
    assert.equal(target.origin, new URL(config.base).origin);
    const response = await fetch(request);
    wire.push({method: request.method, path: target.pathname, query: target.search.slice(1),
               status: response.status, trace: response.headers.get('x-request-id')});
    return response;
  }});
let data;
try {
  if (config.action === 'part') {
    const bytes = Buffer.from(config.body.data, 'base64');
    data = await client.uploads.parts.create(config.id, {data: new File([bytes], 'part.bin')});
  } else {
    assert.equal(config.action, 'cancel');
    data = await client.uploads.cancel(config.id);
  }
} catch (error) {
  if (!(error instanceof OpenAI.APIError) || !error.status) throw error;
  data = {error: error.code, status: error.status};
}
assert.equal(wire.length, 1);
console.log(JSON.stringify({sdk: 'node-' + version, wire: wire[0], data}));
