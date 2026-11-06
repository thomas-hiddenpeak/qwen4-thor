// Offline request preparation through the public Node builder; no network.
import { makeRequest, parseJson } from '../examples/js/chat_client.mjs';
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = parseJson(Buffer.concat(chunks).toString('utf8'));
const { messages, ...options } = input.body;
process.stdout.write(JSON.stringify(makeRequest(messages, options,
  { jsonSchema: input.schema, schemaName: input.name ?? 'answer' })) + '\n');
