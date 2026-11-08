// Bounded subprocess bridge: one command, at most one HTTP request, no tools.
import { loadSdk } from './sdk.mjs';
import { writeSync } from 'node:fs';
import { ChatClient, parseJson, prepareToolCalls, requireValue } from '../examples/js/chat_client.mjs';
import { VALIDATORS, CUSTOM_VALIDATORS } from '../examples/js/project_profile.mjs';

const buffers = [];
let bytes = 0;
for await (const chunk of process.stdin) {
  bytes += chunk.length;
  requireValue(bytes <= 16 * 1024 * 1024, 'bridge input limit'); buffers.push(chunk);
}
const input = parseJson(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(buffers)));
const { OpenAI, version } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
const client = new ChatClient(OpenAI, { baseURL: input.base_url, model: input.model,
  protocolSha256: input.protocol_sha256, timeoutMs: input.timeout_ms ?? 120000,
  apiKey: input.api_key ?? undefined, caFile: input.ca_file ?? undefined,
  connectHost: input.connect_host ?? undefined });
const wire = [];
const wireFd = process.env.Q4T_WIRE_FD;
if (wireFd !== undefined) requireValue(/^[0-9]+$/.test(wireFd) && Number(wireFd) >= 3,
  'invalid capture descriptor');
try {
  let result, planned;
  const options = { sink: chunk => {
    wire.push(chunk);
    if (wireFd !== undefined) {
      let offset = 0;
      while (offset < chunk.length) {
        const count = writeSync(Number(wireFd), chunk, offset, chunk.length - offset);
        requireValue(count > 0, 'capture write failed'); offset += count;
      }
    }
  } };
  if (input.action === 'capabilities') result = await client.capabilities(options);
  else if (input.action === 'models') result = (await client.models(options)).models;
  else if (input.action === 'chat') {
    result = await client.chat(input.request, options);
    delete result.raw;
    if (['tool_calls', 'function_call'].includes(result.finish_reason)) {
      requireValue(input.tool_profile === 'project-records-v1', 'explicit tool profile required');
      planned = prepareToolCalls(result, VALIDATORS, CUSTOM_VALIDATORS);
    }
  } else throw new Error('unknown SDK bridge action');
  process.stdout.write(JSON.stringify({ ok: true, sdk_version: version, result,
    planned_tools: planned, wire_base64: Buffer.concat(wire).toString('base64') })+'\n');
} catch (error) {
  process.stdout.write(JSON.stringify({ ok: false, sdk_version: version,
    error: { class: error.constructor.name, message: error.message, status: error.status,
      code: error.code ?? error.remote?.code, param: error.param ?? error.remote?.param,
      automatic_retry: false, tools_executed: 0 },
    wire_base64: Buffer.concat(wire).toString('base64') })+'\n');
  process.exitCode = 1;
}
