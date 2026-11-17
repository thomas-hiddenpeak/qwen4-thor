// One real pinned Chat SDK request; no retry or transcript-as-answer adapter.
import { loadSdk } from '../serve_protocol/sdk.mjs';
let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  if (input.length > 16 * 1024 * 1024) throw new Error('input limit');
}
const value = JSON.parse(input);
const { OpenAI, version } = loadSdk(value.sdk_directory);
const wire = [], pending = [], exchanges = [];
let bytes = 0;
const control = new AbortController();
const timer = setTimeout(() => control.abort(), 180000);
const client = new OpenAI({ baseURL: value.base_url, apiKey: 'local',
  maxRetries: 0, timeout: 180000, fetch: async (url, options) => {
    exchanges.push({ method: options.method, url: String(url) });
    const response = await fetch(url, options);
    const reader = response.clone().body?.getReader();
    const captured = (async () => {
      if (!reader) return;
      try {
        for (;;) {
          const part = await reader.read();
          if (part.done) break;
          bytes += part.value.length;
          if (bytes > 4 * 1024 * 1024) throw new Error('wire limit');
          wire.push(Buffer.from(part.value));
        }
      } finally { reader.releaseLock(); }
    })();
    captured.catch(() => control.abort());
    pending.push(captured);
    return response;
  } });
const result = { ok: false, version };
try {
  const answer = await client.chat.completions.create(value.request,
    { headers: value.headers ?? {}, signal: control.signal });
  if (value.request.stream) {
    const events = [];
    for await (const event of answer) events.push(event);
    result.result = { events };
  } else result.result = answer;
  result.ok = true;
} catch (error) {
  result.error = { type: error.name, message: error.message, status: error.status };
} finally {
  try { await Promise.all(pending); }
  catch (error) { result.ok = false; result.capture_error = error.message; }
  clearTimeout(timer);
}
result.exchanges = exchanges;
result.wire_base64 = Buffer.concat(wire).toString('base64');
process.stdout.write(JSON.stringify(result) + '\n');
if (!result.ok) process.exitCode = 1;
