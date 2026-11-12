// Official pinned SDK bridge; one request, no retries, bounded wire capture.
import { loadSdk } from '../serve_protocol/sdk.mjs';

const requireValue = (condition, message) => {
  if (!condition) throw new Error(message);
};

export function combineChunks(chunks, wire) {
  requireValue(wire.split('data: [DONE]').length === 2, 'missing/duplicate DONE');
  requireValue(wire.split('data: [DONE]')[1].trim() === '', 'data after DONE');
  const choices = new Map();
  let identity, usage;
  for (const chunk of chunks) {
    requireValue(chunk.object === 'text_completion' && Array.isArray(chunk.choices),
      'invalid completion chunk');
    const current = JSON.stringify([chunk.id, chunk.model, chunk.created]);
    identity ??= current;
    requireValue(identity === current, 'completion identity changed');
    requireValue(!usage, 'chunk after aggregate usage');
    if (chunk.usage) {
      requireValue(chunk.choices.length === 0 && choices.size > 0 &&
        [...choices.values()].every(row => row.finish_reason), 'early usage');
      usage = chunk.usage;
    }
    for (const row of chunk.choices) {
      requireValue(Number.isInteger(row.index) && row.index >= 0 &&
        typeof row.text === 'string', 'invalid choice');
      const collected = choices.get(row.index) ?? {
        index: row.index, text: '', logprobs: null, finish_reason: null };
      requireValue(!collected.finish_reason, 'choice after finish');
      collected.text += row.text;
      if (row.logprobs) {
        const keys = ['tokens', 'token_logprobs', 'top_logprobs', 'text_offset'];
        requireValue(keys.every(key => Array.isArray(row.logprobs[key]) &&
          row.logprobs[key].length === row.logprobs.tokens.length), 'logprob lengths');
        collected.logprobs ??= Object.fromEntries(keys.map(key => [key, []]));
        for (const key of keys) collected.logprobs[key].push(...row.logprobs[key]);
      }
      if (row.finish_reason) {
        requireValue(['stop', 'length', 'content_filter'].includes(row.finish_reason),
          'invalid finish reason');
        collected.finish_reason = row.finish_reason;
      }
      choices.set(row.index, collected);
    }
  }
  const ordered = [...choices.values()].sort((a, b) => a.index - b.index);
  requireValue(ordered.length > 0 && ordered.every((row, i) =>
    row.index === i && row.finish_reason), 'incomplete choices');
  const [id, model, created] = JSON.parse(identity);
  return { id, model, created, object: 'text_completion', choices: ordered, usage };
}

export async function execute(input) {
  const { OpenAI, version } = loadSdk(input.sdk_directory ?? process.env.Q4T_NODE_SDK_DIR);
  const wire = [];
  const captures = [];
  const controller = new AbortController();
  let bytes = 0;
  const timeout = setTimeout(() => controller.abort(), input.timeout_ms ?? 120000);
  const exchanges = [];
  const client = new OpenAI({ baseURL: input.base_url, apiKey: input.api_key ?? 'local',
    maxRetries: 0, timeout: input.timeout_ms ?? 120000,
    fetch: async (url, options) => {
      exchanges.push({ url: String(url), method: options.method, body: options.body });
      const response = await fetch(url, options);
      const reader = response.clone().body?.getReader();
      const capture = (async () => {
        if (!reader) return;
        try {
          for (;;) {
            const part = await reader.read();
            if (part.done) break;
            bytes += part.value.length;
            requireValue(bytes <= 32 * 1024 * 1024, 'wire capture limit');
            wire.push(Buffer.from(part.value));
          }
        } catch (error) {
          controller.abort();
          throw error;
        } finally { reader.releaseLock(); }
      })();
      capture.catch(() => {}); // Join below even when the SDK rejects early.
      captures.push(capture);
      return response;
    } });
  let result, chunks, error;
  try {
    const options = { signal: controller.signal };
    if (input.action === 'completion') {
      const value = await client.completions.create(input.request, options);
      if (input.request.stream) {
        chunks = [];
        for await (const chunk of value) chunks.push(chunk);
      } else result = value;
    } else if (input.action === 'models') {
      result = await client.models.list(options);
      result = { object: 'list', data: result.data };
    } else if (input.action === 'retrieve') {
      result = await client.models.retrieve(input.model, options);
    } else if (input.action === 'delete') {
      result = await client.models.delete(input.model, options);
    } else throw new Error('unknown action');
  } catch (problem) {
    error = { class: problem.constructor.name, message: problem.message,
      status: problem.status, code: problem.code, param: problem.param };
  }
  try {
    await Promise.all(captures);
    if (chunks && !error) result = combineChunks(chunks, Buffer.concat(wire).toString('utf8'));
    requireValue(exchanges.length === 1, 'SDK made more than one request');
  } catch (problem) {
    error ??= { class: problem.constructor.name, message: problem.message };
  } finally { clearTimeout(timeout); }
  return { ok: !error, result, chunks, error, sdk_version: version,
    exchanges, wire_base64: Buffer.concat(wire).toString('base64') };
}
