// Read device-issued Responses over HTTP with an installed stock Node SDK.
// The published SDK declarations supply the expected wire representation.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const [sdkDirectory, fixture] = process.argv.slice(2);
const read = path => readFileSync(path, 'utf8');
const sdk = resolve(sdkDirectory);
const version = JSON.parse(read(resolve(sdk, 'package.json'))).version;
const {default: OpenAI} = await import(pathToFileURL(resolve(sdk, 'index.mjs')));
const types = read(resolve(sdk, 'resources/responses/responses.d.ts'));
function declaration(name) {
  const match = types.match(new RegExp(
    '^([ \\t]*)(?:export )?interface ' + name + ' \\{([\\s\\S]*?)^\\1}', 'm'));
  assert.ok(match, name);
  return match[2];
}
const named = /^\s*name: string;/m.test(
  declaration('ResponseFunctionCallArgumentsDoneEvent'));
const errorType = declaration('McpCall').match(/^\s*error\??: ([^;]+);/m)?.[1];
assert.ok(['string | null', 'ResponsesAPI.McpToolCallError | null'].includes(errorType));
const cacheType = types.match(/^\s*prompt_cache_retention\??: ([^;]+);/m)?.[1];
if (cacheType) assert.ok(["'in_memory' | '24h' | null",
                          "'in-memory' | '24h' | null"].includes(cacheType));
const cache = cacheType?.includes("'in-memory'") ? 'in-memory' : 'in_memory';

const {baseURL, functions, mcp} = JSON.parse(read(fixture));
const apiKey = process.env.Q4T_NODE_READER_KEY;
const foreignKey = process.env.Q4T_NODE_OTHER_KEY;
assert.ok(apiKey && foreignKey && apiKey !== foreignKey);
const counters = {requests: 0, frames: 0, resumed: 0, foreign: 0};
const client = new OpenAI({
  apiKey, baseURL, maxRetries: 0, timeout: 10000,
  fetch: async (url, init) => {
    const headers = new Headers(init.headers);
    assert.equal(headers.get('X-Stainless-Lang'), 'js');
    assert.equal(headers.get('X-Stainless-Package-Version'), version);
    const result = await fetch(url, init);
    assert.ok(result.ok);
    const vary = (result.headers.get('vary') ?? '').toLowerCase().split(',').map(v => v.trim());
    assert.ok(vary.includes('x-stainless-lang') && vary.includes('x-stainless-package-version'));
    ++counters.requests;
    return result;
  },
});
const foreign = new OpenAI({apiKey: foreignKey, baseURL, maxRetries: 0, timeout: 10000});

async function events(id, options = {}) {
  const out = [];
  // Random padding is per connection. Request stable journal bytes on both reads.
  const query = {stream: true, include_obfuscation: false, ...options};
  for await (const event of await client.responses.retrieve(id, query)) {
    out.push(event);
  }
  assert.equal(out.at(-1)?.type, 'response.completed');
  return out;
}

async function checkResource(id, milestone) {
  const resource = await client.responses.retrieve(id);
  assert.equal(resource.id, id);
  assert.equal(resource.prompt_cache_retention, cache);
  const full = await events(id);
  assert.deepEqual(full.at(-1).response.output, resource.output);
  assert.equal(full.at(-1).response.prompt_cache_retention, cache);
  const event = milestone(resource, full);
  assert.ok(Number.isInteger(event.sequence_number) && event.sequence_number > 0);
  const resumed = await events(id, {starting_after: event.sequence_number - 1});
  assert.deepEqual(resumed, full.filter(e => e.sequence_number >= event.sequence_number));
  await assert.rejects(foreign.responses.retrieve(id), e => e.status === 404);
  ++counters.foreign;
  counters.frames += full.length;
  counters.resumed += resumed.length;
}

assert.ok(functions.length > 0);
for (const {id, item_id, name, arguments: args} of functions) {
  await checkResource(id, (resource, stream) => {
    const done = stream.find(e =>
      e.type === 'response.function_call_arguments.done' && e.item_id === item_id);
    assert.ok(done);
    const item = resource.output[done.output_index];
    assert.equal(item.type, 'function_call');
    assert.equal(item.id, item_id);
    assert.equal(item.name, name);
    assert.equal(item.arguments, args);
    assert.equal(done.arguments, args);
    assert.equal(Object.hasOwn(done, 'name'), named);
    if (named) assert.equal(done.name, name);
    return done;
  });
}
await checkResource(mcp.id, (resource, stream) => {
  const item = resource.output.find(i => i.type === 'mcp_call' && i.id === mcp.item_id);
  assert.ok(item);
  assert.equal(typeof item.error, errorType === 'string | null' ? 'string' : 'object');
  assert.deepEqual(typeof item.error === 'string' ? JSON.parse(item.error) : item.error, mcp.error);
  const done = stream.find(e => e.type === 'response.output_item.done' && e.item.id === item.id);
  assert.ok(done);
  assert.deepEqual(done.item, item);
  return done;
});
console.log(JSON.stringify({version, named, errorType, cache, ...counters}));
