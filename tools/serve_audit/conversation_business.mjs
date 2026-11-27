// Verify the same stored conversation lifecycle using a stock Node client.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const config = JSON.parse(readFileSync(0, 'utf8'));
const sdk = resolve(process.argv[2]);
const {version} = JSON.parse(readFileSync(resolve(sdk, 'package.json')));
const {default: OpenAI} = await import(pathToFileURL(resolve(sdk, 'index.mjs')));
const requests = [];
async function observe(input, init) {
  const request = new Request(input, init);
  const target = new URL(request.url);
  const response = await fetch(request);
  requests.push({method: request.method, path: target.pathname, query: target.search.slice(1),
                 status: response.status, trace: response.headers.get('x-request-id')});
  return response;
}
function client(apiKey) {
  return new OpenAI({apiKey, baseURL: config.base, fetch: observe,
                     maxRetries: 0, timeout: 60000});
}
const owner = client(config.key);
const peer = client(config.peer);
const service = owner.conversations;
const created = await service.create({metadata: {state: 'draft'}});
const conversation = created.id;
assert.equal(created.object, 'conversation');
assert.deepEqual(created.metadata, {state: 'draft'});
const metadata = {sdk: version, state: 'checked'};
const updated = await service.update(conversation, {metadata});
assert.equal(updated.id, conversation);
assert.deepEqual(updated.metadata, metadata);
assert.deepEqual((await service.retrieve(conversation)).metadata, metadata);
const added = await service.items.create(conversation, {
  items: config.facts.map(content => ({role: 'user', content})),
});
const items = added.data.map(item => item.id);
assert.equal(items.length, 2);
assert.equal(new Set(items).size, 2);
const first = await service.items.list(conversation, {limit: 1, order: 'asc'});
assert.ok(first.hasNextPage());
assert.deepEqual(first.data.map(item => item.id), items.slice(0, 1));
const last = await first.getNextPage();
assert.equal(last.hasNextPage(), false);
assert.deepEqual(last.data.map(item => item.id), items.slice(1));
const lookup = {conversation_id: conversation};
const saved = await service.items.retrieve(items[1], lookup);
assert.equal(saved.id, items[1]);
assert.equal(saved.content[0].text, config.facts[1]);
await assert.rejects(peer.conversations.items.retrieve(items[1], lookup),
                     err => err instanceof OpenAI.NotFoundError && err.status === 404);
let answer = null;
if (config.generate) {
  const response = await owner.responses.create({model: config.model, conversation,
    input: config.question, temperature: 0, max_output_tokens: 128});
  answer = response.output_text.trim();
  assert.equal(response.status, 'completed');
  assert.equal(answer, config.expected);
}
const removed = await service.items.delete(items[0], lookup);
assert.equal(removed.id, conversation);
assert.equal(removed.object, 'conversation');
await assert.rejects(service.items.retrieve(items[0], lookup),
                     err => err instanceof OpenAI.NotFoundError && err.status === 404);
const deleted = await service.delete(conversation);
assert.equal(deleted.id, conversation);
assert.equal(deleted.object, 'conversation.deleted');
assert.equal(deleted.deleted, true);
await assert.rejects(service.retrieve(conversation),
                     err => err instanceof OpenAI.NotFoundError && err.status === 404);
console.log(JSON.stringify({language: 'node', version, conversation, items, answer, requests}));
