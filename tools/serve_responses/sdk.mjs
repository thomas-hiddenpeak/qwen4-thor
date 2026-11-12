// Official pinned SDK: one real request, no retry, bounded wire capture.
import { loadSdk } from '../serve_protocol/sdk.mjs';
const requireValue = (condition, message) => {
  if (!condition) throw new Error(message);
};
export function responseTerminal(events, wire, after = -1) {
  requireValue(!wire.includes('data: [DONE]'), 'Chat sentinel in Responses');
  requireValue(events.length > 0, 'no retained events');
  let terminal, identity;
  for (let i = 0; i < events.length; ++i) {
    const event = events[i];
    requireValue(!terminal, 'event after terminal');
    requireValue(event.sequence_number === after + 1 + i, 'event sequence gap');
    if (event.response) {
      identity ??= event.response.id;
      requireValue(identity === event.response.id, 'response identity changed');
    }
    if (['response.completed', 'response.incomplete', 'response.failed'].includes(event.type)) {
      requireValue(event.response && event.type === 'response.' + event.response.status, 'terminal status differs');
      terminal = event.response;
    }
  }
  requireValue(terminal, 'missing terminal');
  return terminal;
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
    defaultHeaders: input.headers,
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
    if (input.action === 'create') {
      const value = await client.responses.create(input.request, options);
      if (input.request.stream) {
        chunks = [];
        for await (const chunk of value) chunks.push(chunk);
      } else result = value;
    } else if (input.action === 'moderations_create') {
      result = await client.moderations.create(input.request, options);
    } else if (input.action === 'embeddings_create') {
      result = await client.embeddings.create(input.request, options);
    } else if (input.action === 'compact') {
      result = await client.responses.compact(input.request, options);
    } else if (input.action === 'input_tokens') {
      result = await client.responses.inputTokens.count(input.request, options);
    } else if (input.action === 'retrieve') {
      const value = await client.responses.retrieve(input.id, input.query ?? {}, options);
      if (input.query?.stream) {
        chunks = [];
        for await (const chunk of value) chunks.push(chunk);
      } else result = value;
    } else if (input.action === 'delete') {
      const response = await client.responses.delete(input.id, options).asResponse();
      result = await response.json();
    } else if (input.action === 'input_items') {
      const page = await client.responses.inputItems.list(input.id, input.query ?? {}, options);
      result = { data: page.data, has_more: page.has_more };
    } else if (input.action === 'conversation_create') {
      result = await client.conversations.create(input.request ?? {}, options);
    } else if (input.action === 'conversation_retrieve') {
      result = await client.conversations.retrieve(input.id, options);
    } else if (input.action === 'conversation_delete') {
      result = await client.conversations.delete(input.id, options);
    } else if (input.action === 'conversation_items') {
      const page = await client.conversations.items.list(input.id, input.query ?? {}, options);
      result = { data: page.data, has_more: page.has_more };
    } else throw new Error('unknown action');
  } catch (problem) {
    error = { class: problem.constructor.name, message: problem.message,
      status: problem.status, code: problem.code, param: problem.param };
  }
  try {
    await Promise.all(captures);
    if (!error && ['input_items', 'conversation_items'].includes(input.action)) {
      // CursorPage exposes data/has_more, not every original wire cursor.
      // Join the capture before preserving the actual response metadata.
      const page = JSON.parse(Buffer.concat(wire).toString('utf8'));
      requireValue(JSON.stringify(result.data) === JSON.stringify(page.data) &&
        result.has_more === page.has_more, 'SDK page differs from wire');
      result = page;
    }
    if (chunks && !error) result = responseTerminal(chunks, Buffer.concat(wire).toString('utf8'), input.query?.starting_after ?? -1);
    requireValue(exchanges.length === 1, 'SDK made more than one request');
  } catch (problem) {
    error ??= { class: problem.constructor.name, message: problem.message };
  } finally { clearTimeout(timeout); }
  // Harness evidence: JSON.stringify would erase negative zero in SDK arrays.
  let embedding_float32_base64;
  if (input.action === 'embeddings_create' && result?.data?.every(
      row => Array.isArray(row.embedding))) {
    embedding_float32_base64 = result.data.map(row => {
      const bytes = Buffer.alloc(row.embedding.length * 4);
      row.embedding.forEach((value, index) => bytes.writeFloatLE(value, index * 4));
      return bytes.toString('base64');
    });
  }
  return { ok: !error, result, chunks, error, sdk_version: version,
    embedding_float32_base64,
    exchanges, wire_base64: Buffer.concat(wire).toString('base64') };
}
