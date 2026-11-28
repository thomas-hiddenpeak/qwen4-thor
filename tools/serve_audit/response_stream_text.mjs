// Observe the high-level stream API of the requested, unmodified Node SDK.
import assert from 'node:assert/strict';
import { readFileSync, realpathSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const plan = JSON.parse(readFileSync(0, 'utf8'));
const root = realpathSync(process.argv[2]);
const { name, version } = JSON.parse(readFileSync(resolve(root, 'package.json')));
assert.equal(name, 'openai');
const { default: OpenAI } = await import(pathToFileURL(resolve(root, 'index.mjs')));
const base = new URL(plan.base);
assert.deepEqual([base.protocol, base.hostname, base.pathname], ['https:', '127.0.0.1', '/v1']);
const requests = [];
const client = new OpenAI({ baseURL: plan.base, apiKey: plan.key, maxRetries: 0, timeout: 90000,
  fetch: async (url, options) => {
    if (url === 'data:,') return fetch(url, options);
    const req = new Request(url, options);
    const target = new URL(req.url);
    assert.equal(target.origin, base.origin);
    const reply = await fetch(req);
    requests.push({ method: req.method, path: target.pathname, query: target.search.slice(1),
      status: reply.status, trace: reply.headers.get('x-request-id') });
    return reply;
  },
});
const stream = client.responses.stream(plan.request);
const events = [], annotations = [];
for await (const event of stream) {
  events.push([event.sequence_number, event.type]);
  if (event.type === 'response.output_text.annotation.added') {
    const { item_id, output_index, content_index, annotation_index, annotation } = event;
    annotations.push({ item_id, output_index, content_index, annotation_index, annotation });
  }
}
const response = await stream.finalResponse();
const messages = response.output.flatMap(item => item.type === 'message'
  ? item.content.filter(part => part.type === 'output_text').map(part => [item.id, part.text, part.annotations])
  : []);
const searches = response.output.filter(item => item.type === 'web_search_call')
  .map(item => [item.id, item.status, item.action]);
console.log(JSON.stringify({ sdk: 'node-'+version, id: response.id, status: response.status,
  text: response.output_text ?? null, messages, searches, events, annotations, requests }));
