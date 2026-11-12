// Test-only Node preload. In-memory Fetch; it never listens or opens a socket.
import { appendFileSync, readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';

// The production client now uses an explicit Node HTTP adapter. Substitute
// that module only in this opt-in preload; CPU bridge tests must not open IO.
registerHooks({ resolve(specifier, context, nextResolve) {
  if (specifier === './node_transport.mjs' &&
      context.parentURL?.endsWith('/tools/examples/js/chat_client.mjs'))
    return { url: new URL('./fixture_node_transport.mjs', import.meta.url).href,
      shortCircuit: true };
  return nextResolve(specifier, context);
} });

const config = JSON.parse(readFileSync(process.env.Q4T_PROTOCOL_FIXTURE, 'utf8'));
globalThis.fetch = async (url, init) => {
  const request = init.body ? JSON.parse(init.body) : null;
  appendFileSync(config.calls, JSON.stringify({ url: String(url), method: init.method,
    request, request_raw: init.body ?? null })+'\n');
  const path = new URL(url).pathname;
  if (path === '/v1/capabilities') return Response.json(config.capabilities);
  if (path === '/v1/models') return Response.json({ object: 'list', data: [
    { id: config.capabilities.model, object: 'model', created: 42, owned_by: 'q4t' }] });
  assert.equal(path, '/v1/chat/completions');
  if (config.mode === 'http-error') return Response.json({ error: {
    message: 'busy fixture', type: 'server_error', code: 'overloaded', param: null,
  } }, { status: 503 });
  const base = { id: request.request_id, model: request.model, created: 42 };
  const usage = { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 };
  const toolResults = request.messages.filter(m => m.role === 'tool');
  let message, finish;
  if (toolResults.length) {
    assert.equal(toolResults.length, config.paths.length);
    for (const [index, result] of toolResults.entries()) assert.equal(result.tool_call_id, 'call-'+index);
    const values = toolResults.map(r => JSON.parse(r.content));
    message = { role: 'assistant', content: values.map(v => v.ok ?
      '资料已读取 ' + v.source.citation : '无法核实 ' + v.error.code).join('；') };
    finish = 'stop';
  } else {
    message = { role: 'assistant', content: null, tool_calls: config.paths.map((path, index) => ({
      id: 'call-'+index, type: 'function', function: {
        name: config.mode === 'unknown-tool' && index === 1 ? 'unknown' : 'read_project_record',
        arguments: JSON.stringify({ path, start_line: 1, line_count: 8 }),
      },
    })) };
    finish = 'tool_calls';
  }
  if (!request.stream) return Response.json({ ...base, object: 'chat.completion', usage,
    choices: [{ index: 0, message, finish_reason: finish, logprobs: null }] });
  const event = (delta, terminal = null) => ({ ...base, object: 'chat.completion.chunk', usage: null,
    choices: [{ index: 0, delta, finish_reason: terminal, logprobs: null }] });
  const events = [event({ role: 'assistant', content: '' }), event(message.tool_calls ?
    { tool_calls: message.tool_calls.map((call, index) => ({ index, ...call })) } :
    { content: message.content }), event({}, finish),
  { ...base, object: 'chat.completion.chunk', choices: [], usage }];
  const data = Buffer.from(events.map(e => 'data: '+JSON.stringify(e)+'\n\n').join('') +
    (config.mode === 'missing-done' ? '' : 'data: [DONE]\n\n'));
  if (['disconnect', 'child-crash'].includes(config.mode)) {
    return new Response(new ReadableStream({ start(controller) {
      controller.enqueue(Buffer.from('data: '+JSON.stringify(events[0])+'\n\n'));
      setTimeout(() => config.mode === 'child-crash' ? process.exit(17) :
        controller.error(new Error('fixture transport disconnect')), 50);
    } }), { headers: { 'content-type': 'text/event-stream' } });
  }
  return new Response(data, { headers: { 'content-type': 'text/event-stream' } });
};
