// Bounded real Files SDK bridge; private test inputs, no request replay.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { loadSdk } from '../serve_protocol/sdk.mjs';

const chunks = [];
let size = 0;
for await (const chunk of process.stdin) {
  size += chunk.length;
  assert.ok(size <= 8 * 1024 * 1024, 'bridge input limit');
  chunks.push(chunk);
}
const command = JSON.parse(Buffer.concat(chunks).toString('utf8'));
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(command.base_url).hostname));
const { OpenAI, version } = loadSdk(command.sdk_directory);
const exchanges = [], captures = [];
const tracedFetch = async (url, options) => {
  const response = await fetch(url, options);
  const row = { method: options.method ?? 'GET', path: new URL(url).pathname,
    status: response.status, headers: Object.fromEntries(response.headers) };
  exchanges.push(row);
  captures.push((async () => {
    const values = [];
    let count = 0;
    for await (const chunk of response.clone().body) {
      count += chunk.length;
      assert.ok(count <= 4 * 1024 * 1024, 'bridge output limit');
      values.push(chunk);
    }
    row.wire_base64 = Buffer.concat(values).toString('base64');
  })());
  return response;
};
const client = new OpenAI({ baseURL: command.base_url, apiKey: command.api_key ?? 'local-test',
  defaultHeaders: command.headers ?? {}, maxRetries: 0, timeout: 15000, fetch: tracedFetch });
const output = { ok: false, sdk_version: version, exchanges };
try {
  const body = command.request ?? {};
  if (command.action === 'create') {
    const { data_base64, filename, ...fields } = body;
    output.result = await client.files.create({ ...fields,
      file: new File([Buffer.from(data_base64, 'base64')], filename) });
  } else if (command.action === 'list') {
    const page = await client.files.list(body);
    output.result = { data: page.data, has_more: page.has_more };
  } else if (command.action === 'content') {
    const response = await client.files.content(command.id);
    const data = Buffer.from(await response.arrayBuffer());
    output.result = { bytes: data.length, sha256: createHash('sha256').update(data).digest('hex'),
      data_base64: data.toString('base64') };
  } else {
    assert.ok(['retrieve', 'delete'].includes(command.action), 'unknown Files action');
    output.result = await client.files[command.action](command.id);
  }
  output.ok = true;
} catch (error) {
  output.error = { status: error.status ?? null, message: error.message,
    code: error.code ?? null };
}
await Promise.all(captures);
assert.equal(exchanges.length, 1, 'SDK request replay or no network');
if (output.ok && command.action === 'list') {
  // CursorPage exposes data/has_more, not every original wire field.
  const wire = JSON.parse(Buffer.from(exchanges[0].wire_base64, 'base64').toString('utf8'));
  assert.deepEqual(wire.data, output.result.data);
  assert.equal(wire.has_more, output.result.has_more);
  output.result = wire;
}
process.stdout.write(JSON.stringify(output)+'\n');
if (!output.ok) process.exitCode = 1;
