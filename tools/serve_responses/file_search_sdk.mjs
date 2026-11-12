// Consume captured native output; this peer does not run a model or search.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { loadSdk } from '../serve_protocol/sdk.mjs';

const [baseURL, directory, report] = process.argv.slice(2);
const { OpenAI, version } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
const sdk = new OpenAI({ baseURL, apiKey: 'cpu-captured-wire', maxRetries: 0,
  timeout: 3000 });
const fixture = (name, suffix) => JSON.parse(
  readFileSync(resolve(directory, `${name}.${suffix}`), 'utf8'));
function compare(actual, expected) {
  if (Array.isArray(expected)) {
    assert.ok(Array.isArray(actual));
    assert.equal(actual.length, expected.length);
    expected.forEach((value, i) => compare(actual[i], value));
  } else if (expected && typeof expected === 'object') {
    for (const [key, value] of Object.entries(expected)) {
      assert.ok(Object.hasOwn(actual, key), `missing ${key}`);
      compare(actual[key], value);
    }
  } else assert.deepEqual(actual, expected);
}

const consumed = [];
for (const name of ['included', 'hidden']) {
  const expected = fixture(name, 'json');
  const request = fixture(name, 'request.json');
  delete request.stream;
  delete request.stream_options;
  const response = await sdk.responses.create(request);
  compare(response, expected);
  assert.equal(response.output_text, expected.output[1].content[0].text);
  const stream = sdk.responses.stream({ ...request,
    stream_options: { include_obfuscation: false } });
  const events = [];
  for await (const event of stream) events.push(event);
  compare(await stream.finalResponse(), expected);
  events.forEach((event, index) => assert.equal(event.sequence_number, index));
  consumed.push({ name, response, events });
}
writeFileSync(report, JSON.stringify({ status: 'PASS', version, consumed }, null, 2) + '\n');
