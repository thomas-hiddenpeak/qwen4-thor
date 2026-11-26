// Read a stored response at a cursor captured independently by a Python client.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {loadSdk} from '../serve_protocol/sdk.mjs';

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const textEvents = new Set(['response.output_text.delta', 'response.output_text.done']);

function eventFields(event) {
  const fields = [event.sequence_number, event.type];
  if (textEvents.has(event.type)) {
    fields.push(event.item_id, event.output_index, event.content_index,
                event.type === 'response.output_text.delta' ? event.delta : event.text);
  }
  return fields;
}

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.ok(['https:', 'http:'].includes(base.protocol) && base.hostname === '127.0.0.1' &&
          base.pathname === '/v1' && !base.username && !base.password && !base.search && !base.hash);
const {OpenAI, version} = loadSdk(plan.sdk);
const options = {baseURL: base.href, maxRetries: 0, timeout: 10000};
const owner = new OpenAI({...options, apiKey: plan.key}).responses;
const peer = new OpenAI({...options, apiKey: plan.other}).responses;
const {response: id, cursor, last} = plan.expected;
assert.ok(Number.isSafeInteger(cursor) && Number.isSafeInteger(last) && cursor >= 0 && cursor < last);
const raw = await owner.retrieve(id).asResponse();
const bytes = Buffer.from(await raw.arrayBuffer());
const stored = JSON.parse(bytes.toString('utf8'));
assert.equal(sha(bytes), plan.expected.resource_sha256);
assert.equal(stored.status, 'completed');
assert.equal(stored.id, id);
const message = stored.output[plan.expected.output_index];
assert.equal(message.id, plan.expected.item);
assert.equal(message.content.length, 1);
assert.equal(sha(message.content[0].text), plan.expected.text_sha256);

function verifyTerminal(resource) {
  for (const key of ['id', 'status', 'model', 'previous_response_id', 'usage'])
    assert.deepEqual(resource[key], stored[key]);
  assert.deepEqual(resource.output.map(item => [item.id, item.type]),
                   stored.output.map(item => [item.id, item.type]));
  const item = resource.output[plan.expected.output_index];
  assert.deepEqual(item.content.map(part => [part.type, part.text]),
                   message.content.map(part => [part.type, part.text]));
}

const tail = [];
for await (const event of await owner.retrieve(id, {
  stream: true, starting_after: cursor, include_obfuscation: false,
})) {
  assert.equal(event.sequence_number, cursor + tail.length + 1);
  assert.ok(event.sequence_number <= last && !Object.hasOwn(event, 'obfuscation'));
  if (event.sequence_number === last) {
    assert.equal(event.type, 'response.completed');
    verifyTerminal(event.response);
  }
  tail.push(eventFields(event));
}
assert.equal(tail.length, last - cursor);
const suffix = sha(JSON.stringify(tail));
assert.equal(suffix, plan.expected.suffix_sha256);

// The aggregate helper reconstructs the complete response before filtering
// its public events at the supplied cursor.
const aggregated = owner.stream({response_id: id, starting_after: cursor});
const observed = [];
aggregated.on('event', event => observed.push(eventFields(event)));
verifyTerminal(await aggregated.finalResponse());
assert.deepEqual(observed, tail);
const exhausted = await owner.retrieve(id, {stream: true, starting_after: last});
for await (const event of exhausted) assert.fail('unexpected event past the terminal: ' + event.type);
await assert.rejects(owner.retrieve(id, {stream: true, starting_after: last + 1}),
                     error => error instanceof OpenAI.APIError && error.status === 400 &&
                              error.param === 'starting_after');
await assert.rejects(peer.retrieve(id, {stream: true, starting_after: cursor}),
                     error => error instanceof OpenAI.APIError && error.status === 404);
console.log(JSON.stringify({sdk: version, expected: plan.expected, delivered: tail.length,
  aggregate: true, past_terminal_events: 0, invalid_cursor_status: 400, foreign_status: 404}));
