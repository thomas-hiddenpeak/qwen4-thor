// Capture actual official SDK multipart encoding without a network/model.
// The injected response is only a fixture; this does not accept a Files API.
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve, relative } from 'node:path';
import { loadSdk } from '../serve_protocol/sdk.mjs';

const output = resolve(process.argv[2] ?? '');
const work = resolve('.q4t-work');
assert.ok(output !== work && !relative(work, output).startsWith('..') && process.argv[2]);
mkdirSync(output, { recursive: false, mode: 0o700 });
const { OpenAI, version } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
const bytes = Buffer.from(Array.from({ length: 256 }, (_, i) => i));
writeFileSync(resolve(output, 'binary.bin'), bytes, { flag: 'wx', mode: 0o600 });
let label = 'file', requests = 0;
const client = new OpenAI({ apiKey: 'fixture', baseURL: 'http://fixture.invalid/v1', maxRetries: 0,
  fetch: async (url, options) => {
    const request = new Request(url, options);
    assert.equal(request.method, 'POST');
    assert.equal(new URL(url).hostname, 'fixture.invalid');
    assert.equal(new URL(url).pathname, label === 'file' ? '/v1/files' : '/v1/uploads/upload_fixture/parts');
    const contentType = request.headers.get('content-type');
    assert.ok(contentType.startsWith('multipart/form-data; boundary='));
    const body = Buffer.from(await request.arrayBuffer());
    writeFileSync(resolve(output, `${label}.body`), body, { flag: 'wx', mode: 0o600 });
    const fields = label === 'file' ? [
      { name: 'file', filename: '资料.bin', bytes_file: 'binary.bin' },
      { name: 'purpose', value: 'user_data' },
      { name: 'expires_after[anchor]', value: 'created_at' },
      { name: 'expires_after[seconds]', value: '3600' },
    ] : [{ name: 'data', filename: 'part.bin', bytes_file: 'binary.bin' }];
    writeFileSync(resolve(output, `${label}.json`), JSON.stringify({ sdk_version: version,
      content_type: contentType, fields, request_bytes: body.length }, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
    ++requests;
    const response = label === 'file' ? { id: 'file_fixture', object: 'file',
      bytes: bytes.length, created_at: 1, filename: '资料.bin', purpose: 'user_data', status: 'processed' }
      : { id: 'part_fixture', object: 'upload.part', upload_id: 'upload_fixture', created_at: 1 };
    return new Response(JSON.stringify(response), { status: 200, headers: { 'Content-Type': 'application/json' } });
  } });
await client.files.create({ file: new File([bytes], '资料.bin'), purpose: 'user_data',
  expires_after: { anchor: 'created_at', seconds: 3600 } });
label = 'part';
await client.uploads.parts.create('upload_fixture', { data: new File([bytes], 'part.bin') });
assert.equal(requests, 2);
process.stdout.write(JSON.stringify({ status: 'PASS_SDK_ENCODING_CAPTURE', requests, sdk_version: version,
  actual_network_requests: 0, model_starts: 0, api_acceptance: false }) + '\n');
