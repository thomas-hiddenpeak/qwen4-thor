// One real Uploads SDK operation, with response capture and retries disabled.
import assert from 'node:assert/strict';
import { loadSdk } from '../serve_protocol/sdk.mjs';

const chunks = [];
let bytes = 0;
for await (const chunk of process.stdin) {
  bytes += chunk.length;
  assert.ok(bytes <= 8 * 1024 * 1024, 'bridge input bound');
  chunks.push(chunk);
}
const command = JSON.parse(Buffer.concat(chunks).toString('utf8'));
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(command.base_url).hostname));
const { OpenAI, version } = loadSdk(command.sdk_directory);
const exchanges = [], captures = [];
let attempts = 0;
const client = new OpenAI({ baseURL: command.base_url, apiKey: command.api_key,
  defaultHeaders: command.headers ?? {}, maxRetries: 0, timeout: 15000,
  fetch: async (url, options) => {
    // The SDK probes FormData support with a local data URI, not an API call.
    if (url === 'data:,') return fetch(url, options);
    attempts++;
    const response = await fetch(url, options);
    const row = { method: options.method ?? 'GET', path: new URL(url).pathname,
      status: response.status, headers: Object.fromEntries(response.headers) };
    exchanges.push(row);
    captures.push((async () => {
      const data = [];
      let count = 0;
      for await (const chunk of response.clone().body) {
        count += chunk.length;
        assert.ok(count <= 1024 * 1024, 'bridge output bound');
        data.push(chunk);
      }
      row.wire_base64 = Buffer.concat(data).toString('base64');
    })());
    return response;
  } });
const output = { ok: false, sdk_version: version, exchanges };
try {
  if (command.action === 'part') {
    output.result = await client.uploads.parts.create(command.id, {
      data: new File([Buffer.from(command.request.data_base64, 'base64')], 'part.bin') });
  } else if (command.action === 'create') {
    output.result = await client.uploads.create(command.request);
  } else {
    assert.ok(['complete', 'cancel'].includes(command.action));
    output.result = command.action === 'cancel' ? await client.uploads.cancel(command.id)
      : await client.uploads.complete(command.id, command.request);
  }
  output.ok = true;
} catch (error) {
  output.error = { status: error.status ?? null, message: error.message,
    code: error.code ?? null, cause: error.cause?.message ?? null,
    cause_code: error.cause?.code ?? error.cause?.cause?.code ?? null };
}
await Promise.all(captures);
assert.equal(attempts, 1, 'SDK replay or missing actual request');
output.attempts = attempts;
process.stdout.write(JSON.stringify(output) + '\n');
if (!output.ok) process.exitCode = 1;
