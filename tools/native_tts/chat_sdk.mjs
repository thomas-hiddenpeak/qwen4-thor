// One actual JS Chat audio request. The transport bytes and SDK result must agree.
import fs from 'node:fs';
import { loadSdk } from '../serve_protocol/sdk.mjs';
let input = '';
for await (const part of process.stdin) {
  input += part;
  if (input.length > 65536) throw new Error('input capacity');
}
const value = JSON.parse(input);
const { OpenAI, version } = loadSdk(value.sdk_directory);
const exchanges = [], captured = [];
let bytes = 0;
const client = new OpenAI({ baseURL: value.base_url, apiKey: 'local',
  defaultHeaders: value.headers, maxRetries: 0, timeout: 180000,
  fetch: async (url, options) => {
    exchanges.push({ method: options.method, url: String(url) });
    const response = await fetch(url, options);
    const body = response.body.pipeThrough(new TransformStream({
      transform(chunk, controller) {
        bytes += chunk.byteLength;
        if (bytes > 4 * 1024 * 1024) throw new Error('wire capacity');
        captured.push(Buffer.from(chunk));
        controller.enqueue(chunk);
      }
    }));
    return new Response(body, { status: response.status,
      statusText: response.statusText, headers: response.headers });
  } });
const started = performance.now();
const result = await client.chat.completions.create(value.request);
if (exchanges.length !== 1 || !bytes) throw new Error('empty or retried');
fs.writeFileSync(value.output, Buffer.concat(captured));
process.stdout.write(JSON.stringify({ ok: true, version, result, exchanges,
  seconds: (performance.now() - started) / 1000 }) + '\n');
