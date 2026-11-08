// Development acceptance bridge with a single consumer of the real HTTP body.
// A cloned response would keep an SSE subscriber alive after the SDK aborts.
import { loadSdk } from '../serve_protocol/sdk.mjs';
import { responseTerminal } from './sdk.mjs';

const requireValue = (condition, message) => {
  if (!condition) throw new Error(message);
};

export function prefix(events, after = -1, identity) {
  requireValue(events.length > 0, 'empty event prefix');
  let terminal = false;
  for (let i = 0; i < events.length; ++i) {
    const event = events[i];
    requireValue(!terminal, 'event after terminal');
    requireValue(event.sequence_number === after + 1 + i, 'event sequence gap');
    if (event.response) {
      identity ??= event.response.id;
      requireValue(event.response.id === identity, 'event response identity changed');
    }
    terminal = ['response.completed', 'response.incomplete', 'response.failed'].includes(event.type);
    requireValue(!['response.cancelled', 'error'].includes(event.type), 'unexpected event in business prefix');
  }
  return { identity, terminal, last_sequence: events.at(-1).sequence_number };
}

export async function execute(input) {
  const { OpenAI, version } = loadSdk(input.sdk_directory ?? process.env.Q4T_NODE_SDK_DIR);
  const controller = new AbortController();
  const wire = [], exchanges = [];
  let bytes = 0, events, result, error, stopped = false;
  const timeout = setTimeout(() => controller.abort(), input.timeout_ms ?? 120000);
  const client = new OpenAI({ baseURL: input.base_url, apiKey: input.api_key ?? 'local',
    maxRetries: 0, timeout: input.timeout_ms ?? 120000,
    fetch: async (url, options) => {
      exchanges.push({ url: String(url), method: options.method, body: options.body });
      const response = await fetch(url, options);
      const body = response.body?.pipeThrough(new TransformStream({ transform(chunk, stream) {
        bytes += chunk.length;
        requireValue(bytes <= 32 * 1024 * 1024, 'wire capture limit');
        wire.push(Buffer.from(chunk));
        stream.enqueue(chunk);
      } }));
      return new Response(body, { status: response.status, statusText: response.statusText,
        headers: response.headers });
    } });
  try {
    const options = { signal: controller.signal };
    let value;
    if (input.action === 'create') value = await client.responses.create(input.request, options);
    else if (input.action === 'retrieve') value = await client.responses.retrieve(input.id, input.query ?? {}, options);
    else if (input.action === 'cancel') value = await client.responses.cancel(input.id, options);
    else if (input.action === 'input_items') {
      const page = await client.responses.inputItems.list(input.id, input.query ?? {}, options);
      value = { object: 'list', data: page.data, first_id: page.first_id,
        last_id: page.last_id, has_more: page.has_more };
    } else if (input.action === 'delete') {
      value = await (await client.responses.delete(input.id, options).asResponse()).json();
    } else throw new Error('unknown background SDK action');
    const streaming = input.action === 'create' ? input.request.stream :
      input.action === 'retrieve' && input.query?.stream;
    if (!streaming) result = value;
    else {
      events = [];
      for await (const event of value) {
        events.push(event);
        if (input.stream_mode === 'first_text_delta' && event.type === 'response.output_text.delta') {
          stopped = true;
          break;
        }
      }
      if (stopped) controller.abort();
      const status = prefix(events, input.query?.starting_after ?? -1, input.id);
      if (input.stream_mode === 'first_text_delta') {
        requireValue(stopped && !status.terminal, 'did not disconnect an active text stream');
        result = { id: status.identity, last_sequence: status.last_sequence, subscriber_aborted: true };
      } else if (input.stream_mode === 'cancelled_replay') {
        requireValue(!status.terminal, 'cancelled replay fabricated terminal');
        result = { id: input.id, last_sequence: status.last_sequence, terminal_event: false };
      } else result = responseTerminal(events, Buffer.concat(wire).toString('utf8'), input.query?.starting_after ?? -1);
    }
    requireValue(exchanges.length === 1, 'SDK request replay');
    requireValue(!Buffer.concat(wire).includes(Buffer.from('data: [DONE]')), 'Chat DONE in Responses');
  } catch (problem) {
    error = { class: problem.constructor.name, message: problem.message,
      status: problem.status, code: problem.code, param: problem.param };
  } finally {
    clearTimeout(timeout);
    controller.abort();
  }
  return { ok: !error, result, chunks: events, error, sdk_version: version,
    exchanges, wire_base64: Buffer.concat(wire).toString('base64') };
}
