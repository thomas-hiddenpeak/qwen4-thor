// Read or write the same project completion through an installed official Node SDK.
import assert from 'node:assert/strict';
import {readFileSync, realpathSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const config = JSON.parse(readFileSync(0, 'utf8'));
const root = realpathSync(process.argv[2]);
const {version, name} = JSON.parse(readFileSync(resolve(root, 'package.json')));
assert.equal(name, 'openai');
const base = new URL(config.base);
assert.equal(base.protocol, 'https:');
assert.equal(base.hostname, '127.0.0.1');
const {default: OpenAI} = await import(pathToFileURL(resolve(root, 'index.mjs')));
const requests = [];
function resource(apiKey) {
  return new OpenAI({apiKey, baseURL: config.base, maxRetries: 0, timeout: 55000,
    fetch: async (url, options) => {
      if (url === 'data:,') return fetch(url, options);
      const request = new Request(url, options);
      const target = new URL(request.url);
      assert.equal(target.origin, base.origin);
      const reply = await fetch(request);
      requests.push({method: request.method, path: target.pathname, query: target.search.slice(1),
                     status: reply.status, trace: reply.headers.get('x-request-id')});
      return reply;
    }}).chat.completions;
}
const summary = (chat) => ({id: chat.id, answer: chat.choices[0].message.content, metadata: chat.metadata});
async function denied(fn) {
  try { await fn(); }
  catch (error) {
    assert.ok(error instanceof OpenAI.NotFoundError);
    return {status: error.status, code: error.code};
  }
  throw new Error('stored Chat lookup unexpectedly succeeded');
}

const owner = resource(config.key);
let result;
if (config.action === 'write') {
  result = {created: summary(await owner.create(config.request))};
} else {
  assert.equal(config.action, 'read');
  const id = config.id;
  const before = summary(await owner.retrieve(id));
  const forbidden = await denied(() => resource(config.peer).retrieve(id));
  const updated = summary(await owner.update(id, {metadata: config.metadata}));
  const listing = await owner.list({metadata: config.metadata, limit: 1});
  const pages = [];
  const query = {order: 'asc', limit: 1};
  for (let i = 0; i < 2; i++) {
    const page = await owner.messages.list(id, query);
    assert.equal(page.data.length, 1);
    const {id: messageId, role, content} = page.data[0];
    pages.push({id: messageId, role, content, has_more: page.has_more});
    query.after = messageId;
  }
  const saved = summary(await owner.retrieve(id));
  const deleted = await owner.delete(id);
  const gone = await denied(() => owner.retrieve(id));
  result = {before, forbidden, updated, listed: listing.data.map(row => row.id),
            list_has_more: listing.has_more, pages, saved, deleted: {id: deleted.id, deleted: deleted.deleted}, gone};
}
console.log(JSON.stringify({sdk: 'node-' + version, requests, ...result}));
