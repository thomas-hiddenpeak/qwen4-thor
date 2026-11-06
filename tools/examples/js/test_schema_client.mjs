// Standard-library-only schema contract tests, including lossy JS JSON values.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeRequest, withJsonSchema, SCHEMA_INSTRUCTION, ProtocolError } from './chat_client.mjs';

const schema = { type: 'object', properties: { direction: { type: 'string',
  enum: ['left_to_right', 'right_to_left'], description: '箭头向右 means left_to_right' } },
required: ['direction'], additionalProperties: false };
const body = (content = 'Which direction?') => makeRequest([{ role: 'user', content }]);

test('builder exposes schema semantics and supports either explicit output budget', () => {
  for (const limits of [{ max_tokens: 768 }, { max_completion_tokens: 768 }]) {
    const result = makeRequest(body().messages, limits, { jsonSchema: schema });
    assert.deepEqual(JSON.parse(result.messages[0].content.split(SCHEMA_INSTRUCTION)[1]), schema);
    assert.deepEqual(result.response_format.json_schema.schema, schema);
    assert.equal(result.max_tokens ?? result.max_completion_tokens, 768);
    assert.equal(['max_tokens', 'max_completion_tokens'].filter(k => Object.hasOwn(result, k)).length, 1);
  }
});

test('parts and actual final tool history are copied without rewriting results', () => {
  const original = body([{ type: 'image_url', image_url: 'data:image/png;base64,AA==' }, { type: 'text', text: 'Which direction?' }]);
  original.messages.push({ role: 'assistant', content: null, tool_calls: [{ id: 'c', type: 'function', function: { name: 'read', arguments: '{}' } }] },
    { role: 'tool', tool_call_id: 'c', content: '{"actual":123}' });
  original.tools = [{ type: 'function', function: { name: 'read' } }]; original.tool_choice = 'none';
  const before = structuredClone(original), supplied = structuredClone(schema);
  const result = withJsonSchema(original, supplied); supplied.properties = {};
  assert.deepEqual(original, before);
  assert.deepEqual(result.messages.slice(1), before.messages.slice(1));
  assert.deepEqual(result.messages[0].content.slice(0, -1), before.messages[0].content);
  assert.deepEqual(result.response_format.json_schema.schema, schema);
});

test('ambiguous formats, active tools, cache and missing user reject locally', () => {
  for (const extra of [{ response_format: {} }, { tools: [{}] }, { session_id: 'a'.repeat(64) },
    { prefix_cache_scope: 'x' }, { parse_response: false }, { messages: [] },
    { messages: [{ role: 'tool', content: 'x' }] }]) {
    assert.throws(() => withJsonSchema({ ...body(), ...extra }, schema), ProtocolError);
  }
});

test('lossy or unsafe JSON cannot silently change a schema', () => {
  for (const description of [NaN, Infinity, 9007199254740992, undefined, () => 1, new Date(), '\ud800']) {
    assert.throws(() => withJsonSchema(body(), { type: 'object', description }), ProtocolError);
  }
  const cycle = { type: 'object' }; cycle.cycle = cycle;
  assert.throws(() => withJsonSchema(body(), cycle), ProtocolError);
});

test('UTF8 byte limit and names fail before a request', () => {
  withJsonSchema(body(), { type: 'object', description: '中文'.repeat(10900) });
  assert.throws(() => withJsonSchema(body(), { type: 'object', description: '中文'.repeat(11000) }), ProtocolError);
  for (const name of ['', 'space name', 'x'.repeat(65), '答案']) {
    assert.throws(() => withJsonSchema(body(), schema, { name }), ProtocolError);
  }
});

test('budgets are bounded, defaults stable, conflicting aliases rejected', () => {
  assert.equal(body().max_completion_tokens, 256);
  assert.equal(body().response_format, undefined);
  for (const max_tokens of [0, -1, true, 1.5, null, 2147483648]) {
    assert.throws(() => makeRequest(body().messages, { max_tokens }), ProtocolError);
  }
  assert.throws(() => makeRequest(body().messages, { max_tokens: 1, max_completion_tokens: 1 }), ProtocolError);
});
