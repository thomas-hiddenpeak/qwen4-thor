// One local TLS request through the pinned official SDK. No retries.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { check } from '../serve_realtime/conversation_checks.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  check(Buffer.byteLength(input) <= 131072, 'bounded Live startup');
}
const args = JSON.parse(input);
const sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR, 'node_modules/openai');
check(JSON.parse(fs.readFileSync(path.join(sdk, 'package.json'))).version === '7.47.0', 'SDK identity');
const endpoint = new URL(args.base_url);
check(endpoint.protocol === 'https:' && endpoint.hostname === '127.0.0.1' &&
      endpoint.pathname === '/v1' && !endpoint.username && !endpoint.password &&
      !endpoint.search && !endpoint.hash, 'local test endpoint');
const { default: OpenAI } = await import(pathToFileURL(path.join(sdk, 'index.mjs')));
const client = new OpenAI({ baseURL: endpoint.toString(), apiKey: args.key, maxRetries: 0, timeout: 20000 });
try {
  const pending = args.source ? client.live.sessions.fork(args.source, args.request) : client.live.create(args.request);
  const { response, data } = await pending.withResponse();
  console.log(JSON.stringify({status: response.status, data}));
} catch (error) {
  if (!Number.isInteger(error.status)) throw error;
  console.log(JSON.stringify({status: error.status, code: error.code}));
}
