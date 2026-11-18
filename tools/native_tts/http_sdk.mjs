// One real JS Speech SDK stream; bounded bytes, no automatic retry.
import fs from 'node:fs';
import crypto from 'node:crypto';
import { loadSdk } from '../serve_protocol/sdk.mjs';
let input = '';
for await (const part of process.stdin) {
  input += part;
  if (input.length > 65536) throw new Error('input limit');
}
const value = JSON.parse(input);
const { OpenAI, version } = loadSdk(value.sdk_directory);
const exchanges = [];
const client = new OpenAI({ baseURL: value.base_url, apiKey: 'local',
  maxRetries: 0, timeout: 180000, fetch: async (url, options) => {
    exchanges.push({ method: options.method, url: String(url) });
    return fetch(url, options);
  } });
const started = performance.now();
const response = await client.audio.speech.create(value.request);
if (!response.ok || !response.body) throw new Error('Speech HTTP failure');
const chunks = [];
let count = 0, first = null;
for await (const part of response.body) {
  count += part.length;
  if (count > 2 * 1024 * 1024) throw new Error('Speech body limit');
  if (part.length) {
    if (first === null) first = (performance.now() - started) / 1000;
    chunks.push(Buffer.from(part));
  }
}
const data = Buffer.concat(chunks);
if (!data.length || exchanges.length !== 1) throw new Error('empty/retried');
fs.writeFileSync(value.output, data);
process.stdout.write(JSON.stringify({ ok: true, version, status: response.status,
  headers: Object.fromEntries(response.headers), chunks: chunks.length,
  first_seconds: first, seconds: (performance.now() - started) / 1000,
  bytes: data.length, sha256: crypto.createHash('sha256').update(data).digest('hex'),
  exchanges }) + '\n');
