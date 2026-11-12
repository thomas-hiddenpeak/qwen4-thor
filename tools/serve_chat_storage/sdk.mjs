// Acceptance-only bridge. One official SDK request and one body consumer;
// capture actual wire bytes without keeping a hidden cloned SSE subscriber.
import { loadSdk } from '../serve_protocol/sdk.mjs';

export async function execute(input) {
  const { OpenAI, version } = loadSdk(input.sdk_directory ?? process.env.Q4T_NODE_SDK_DIR);
  const controller = new AbortController();
  const wire = [], exchanges = [];
  let bytes = 0, result, chunks, error;
  const timeout = setTimeout(() => controller.abort(), input.timeout_ms ?? 120000);
  const client = new OpenAI({ baseURL: input.base_url, apiKey: input.api_key ?? 'local',
    maxRetries: 0, timeout: input.timeout_ms ?? 120000,
    fetch: async (url, options) => {
      exchanges.push({ url: String(url), method: options.method, body: options.body });
      const response = await fetch(url, options);
      const body = response.body?.pipeThrough(new TransformStream({ transform(chunk, stream) {
        bytes += chunk.length;
        if (bytes > 32 * 1024 * 1024) throw new Error('wire capture limit');
        wire.push(Buffer.from(chunk));
        stream.enqueue(chunk);
      } }));
      return new Response(body, { status: response.status, statusText: response.statusText,
        headers: response.headers });
    } });
  try {
    const options = { signal: controller.signal };
    const chat = client.chat.completions;
    if (input.action === 'create') result = await chat.create(input.request, options);
    else if (input.action === 'retrieve') result = await chat.retrieve(input.id, options);
    else if (input.action === 'update') result = await chat.update(input.id, input.request, options);
    else if (input.action === 'delete') result = await chat.delete(input.id, options);
    else if (input.action === 'list' || input.action === 'messages') {
      const page = input.action === 'list' ? await chat.list(input.query ?? {}, options) :
        await chat.messages.list(input.id, input.query ?? {}, options);
      // CursorPage exposes data/has_more, not every raw wire field. Preserve
      // the already captured response instead of inventing/omitting cursors.
      result = JSON.parse(Buffer.concat(wire).toString('utf8'));
      if (JSON.stringify(page.data) !== JSON.stringify(result.data) ||
          page.has_more !== result.has_more) throw new Error('SDK page differs from wire');
    } else throw new Error('unknown stored Chat SDK action');
    if (input.action === 'create' && input.request.stream) {
      const stream = result;
      result = null;
      chunks = [];
      for await (const chunk of stream) chunks.push(chunk);
      const raw = Buffer.concat(wire).toString('utf8');
      if (raw.split('data: [DONE]').length !== 2) throw new Error('missing or repeated Chat terminal');
    }
    if (exchanges.length !== 1) throw new Error('SDK request replay');
  } catch (problem) {
    error = { class: problem.constructor.name, message: problem.message,
      status: problem.status, code: problem.code, param: problem.param };
    result = null;
  } finally {
    clearTimeout(timeout);
    controller.abort();
  }
  return { ok: !error, result, chunks, error, sdk_version: version,
    exchanges, wire_base64: Buffer.concat(wire).toString('base64') };
}
