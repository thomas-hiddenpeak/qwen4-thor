// Offline request preparation through the public Node builder; no network.
import { makeRequest, parseJson, stringifyJson, requireValue } from '../examples/js/chat_client.mjs';
const chunks = [];
let bytes = 0;
for await (const chunk of process.stdin) {
  bytes += chunk.length;
  requireValue(bytes <= 16 * 1024 * 1024, 'request preparation input exceeds 16 MiB');
  chunks.push(chunk);
}
const input = parseJson(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)),
  { exactNumbers: true });
const { messages, ...options } = input.body;
process.stdout.write(stringifyJson(makeRequest(messages, options,
  { jsonSchema: input.schema, schemaName: input.name ?? 'answer' })) + '\n');
