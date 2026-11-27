import assert from 'node:assert/strict';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const options = JSON.parse(readFileSync(0, 'utf8'));
const original = options.original;
const version = JSON.parse(readFileSync(resolve(options.sdk, 'package.json'))).version;
assert.equal(version, options.version);
assert.ok(['6.34.0', '7.47.0'].includes(version));
const {default: OpenAI} = await import(pathToFileURL(resolve(options.sdk, 'index.mjs')));
const requests = [];
function connect(key, peer = false) {
  return new OpenAI({
    apiKey: key, baseURL: options.base, timeout: 70000, maxRetries: 0,
    fetch: async (url, init) => {
      const headers = new Headers(init.headers);
      assert.equal(headers.get('x-stainless-package-version'), version);
      assert.equal(headers.get('x-stainless-lang'), 'js');
      const response = await fetch(url, init);
      requests.push({method: init.method, path: new URL(url).pathname, status: response.status, peer});
      return response;
    },
  });
}
const client = connect(options.key);
const peer = connect(options.other, true);
assert.notEqual(options.key, options.other);
const folder = options.capture;
mkdirSync(folder, {mode: 0o700});
const save = (name, value) => writeFileSync(resolve(folder, name + '.json'), JSON.stringify(value),
                                            {flag: 'wx', mode: 0o600});

async function jsonResponse(pending, label) {
  const response = await pending.asResponse();
  // Node adds output_text after parsing. Keep both the server object and SDK view.
  const raw = await response.clone().json();
  const parsed = await pending;
  const messages = raw.output.filter(item => item.type === 'message');
  const text = messages.flatMap(item => item.content.filter(part => part.type === 'output_text'))
                       .map(part => part.text).join('');
  // Some releases omit the convenience property for retrievals.
  if (Object.hasOwn(parsed, 'output_text')) assert.deepEqual(parsed, {...raw, output_text: text});
  else assert.deepEqual(parsed, raw);
  save(label, raw);
  return raw;
}
const call = await jsonResponse(client.responses.retrieve(original.response_id), 'call');
const answer = await jsonResponse(client.responses.retrieve(original.followup), 'answer');
assert.equal(call.id, original.response_id);
assert.equal(answer.previous_response_id, call.id);
for (const id of [call.id, answer.id]) {
  await assert.rejects(peer.responses.retrieve(id), err => err.status === 404);
}

async function readStream(label, after) {
  const query = {stream: true, include_obfuscation: false};
  if (after !== undefined) query.starting_after = after;
  const frames = [];
  for await (const event of await client.responses.retrieve(call.id, query)) frames.push(event);
  assert.equal(frames.at(-1)?.type, 'response.completed');
  save(label, frames);
  return frames;
}
const full = await readStream('events');
const suffix = await readStream('resumed', original.cursor);
assert.deepEqual(full.map(event => event.sequence_number), full.map((_, i) => i));
assert.deepEqual(suffix, full.slice(original.cursor + 1));
const marker = suffix[0];
assert.equal(marker.type, 'response.function_call_arguments.done');
assert.equal(marker.item_id, original.item_id);
const item = call.output[marker.output_index];
assert.equal(item.id, marker.item_id);
assert.equal(item.call_id, original.call_id);
assert.equal(item.name, original.function);
assert.equal(item.arguments, marker.arguments);
const named = version === '6.34.0';
assert.equal(Object.hasOwn(marker, 'name'), named);
if (named) assert.equal(marker.name, item.name);
assert.equal(call.prompt_cache_retention, named ? 'in-memory' : 'in_memory');
assert.deepEqual(full.at(-1).response, call);
if (options.continue_tool) {
  const fresh = await jsonResponse(client.responses.create({
    model: options.model, previous_response_id: call.id, store: true, temperature: 0,
    tool_choice: 'none', max_output_tokens: 140, reasoning: {effort: 'none'},
    instructions: 'Reply with just the publication revision number returned by the tool.',
    input: [{type: 'function_call_output', call_id: item.call_id,
             output: JSON.stringify({revision: options.revision})}],
  }), 'continuation');
  assert.equal(fresh.status, 'completed');
  assert.equal(fresh.previous_response_id, call.id);
  assert.ok(fresh.id !== call.id && fresh.id !== answer.id);
  assert.equal(fresh.output.filter(i => i.type === 'message')
                           .flatMap(i => i.content.filter(p => p.type === 'output_text'))
                           .map(p => p.text).join('').trim(), String(options.revision));
}
assert.equal(requests.length, options.continue_tool ? 7 : 6);
assert.equal(requests.filter(r => r.method === 'POST').length, Number(options.continue_tool));
console.log(JSON.stringify({sdk: version, foreign_reads: requests.filter(r => r.peer).map(r => r.status),
                            requests, named}));
