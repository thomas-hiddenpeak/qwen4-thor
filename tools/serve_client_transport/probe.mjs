// One actual pinned-SDK request to a local acceptance peer. No tool execution.
import { loadSdk } from '../serve_protocol/sdk.mjs';
import { ChatClient } from '../examples/js/chat_client.mjs';
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = JSON.parse(Buffer.concat(chunks));
const { OpenAI } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
const client = new ChatClient(OpenAI, { baseURL: input.url, timeoutMs: 250,
  apiKey: 'transport-probe-key', caFile: input.ca, connectHost: input.connect });
const began = performance.now();
let row;
try {
  const result = await client.models();
  row = { ok: true, models: result.models, status: result.status };
} catch (error) {
  const causes = [];
  for (let e = error; e && causes.length < 5; e = e.cause)
    causes.push({ name: e.constructor.name, code: e.code });
  row = { ok: false, status: error.status, causes };
}
row.seconds = (performance.now() - began) / 1000;
process.stdout.write(JSON.stringify(row) + '\n');
