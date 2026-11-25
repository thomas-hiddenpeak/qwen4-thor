// Replay a validation error, repair the call, and use a real project record.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {loadSdk} from './sdk.mjs';

let input = '';
for await (const piece of process.stdin) input += piece;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
const connect = apiKey => new OpenAI({baseURL: p.base, apiKey, timeout: 45000, maxRetries: 0});
const owner = connect(p.key), other = connect(p.other);
const {tools: {history: capability}} = await owner.get('/capabilities');
assert.equal(capability.arguments, 'object_or_original_identified_argument_text');
assert.equal(capability.unidentified_arguments, 'required_object_or_json_object_text');
assert.equal(capability.non_mapping_text, 'quoted_assistant_history');
const raw = '{"record":';
const name = 'get_project_scope';
const source = new URL('../../docs/evidence/serve-scope-decision-20261120/decision.json', import.meta.url);
const tool = {type: 'function', function: {name, description: 'Read the project scope decision.',
  strict: true, parameters: {type: 'object', properties: {record: {type: 'string', enum: ['scope']}},
    required: ['record'], additionalProperties: false}}};
const messages = [
  {role: 'user', content: 'Use get_project_scope with record scope. Return only the number of required paths from its result.'},
  {role: 'assistant', content: null, tool_calls: [
    {id: 'call_previous', type: 'function', function: {name, arguments: raw}}]},
  {role: 'tool', tool_call_id: 'call_previous',
    content: 'The argument text ended before the record value. Retry with record set to scope.'}];
const base = {model: p.model, reasoning_effort: 'none', temperature: 0, max_completion_tokens: 96};
const repaired = p.previous ? await owner.chat.completions.retrieve(p.previous.stored) :
  await owner.chat.completions.create({...base, store: true, tools: [tool], messages,
  tool_choice: {type: 'function', function: {name}}});
const choice = repaired.choices[0];
assert.equal(choice.finish_reason, 'tool_calls');
assert.equal(choice.message.tool_calls.length, 1);
const call = choice.message.tool_calls[0];
assert.equal(call.function.name, name);
assert.notEqual(call.id, 'call_previous');
assert.deepEqual(JSON.parse(call.function.arguments), {record: 'scope'});
const saved = [];
for await (const item of owner.chat.completions.messages.list(repaired.id, {order: 'asc'})) saved.push(item);
assert.equal(saved.find(item => item.tool_calls?.[0]?.id === 'call_previous').tool_calls[0].function.arguments, raw);
await assert.rejects(other.chat.completions.messages.list(repaired.id), err => err.status === 404);
const record = {sdk: version, stored: repaired.id, source_call: 'call_previous',
  corrected_call: call.id, corrected_arguments: JSON.parse(call.function.arguments),
  original_text: raw, capability, peer_status: 404};
if (p.phase === 'primary') {
  process.stdout.write(JSON.stringify(record));
} else {
  const count = JSON.parse(readFileSync(source)).counts.required_inference_or_application_service;
  const history = [...messages, {role: 'assistant', content: null, tool_calls: [call]},
    {role: 'tool', tool_call_id: call.id, content: JSON.stringify({required_paths: count})}];
  const stream = await owner.chat.completions.create({...base, stream: true, tools: [tool], tool_choice: 'none', messages: history});
  let answer = '', terminal;
  for await (const part of stream) {
    for (const item of part.choices) {
      answer += item.delta.content ?? '';
      if (item.finish_reason) terminal = item.finish_reason;
    }
  }
  assert.equal(answer.trim(), String(count));
  assert.equal(terminal, 'stop');
  const response = await owner.responses.create({model: p.model, store: true,
    reasoning: {effort: 'none'}, temperature: 0, max_output_tokens: 64,
    input: [messages[0], {type: 'function_call', call_id: 'call_previous', name, arguments: raw},
      {type: 'function_call_output', call_id: 'call_previous', output: messages[2].content},
      {type: 'function_call', call_id: call.id, name, arguments: call.function.arguments},
      {type: 'function_call_output', call_id: call.id, output: history.at(-1).content}]});
  assert.equal(response.output_text.trim(), String(count));
  const items = [];
  for await (const item of owner.responses.inputItems.list(response.id, {order: 'asc'})) items.push(item);
  assert.equal(items.find(item => item.type === 'function_call' && item.call_id === 'call_previous').arguments, raw);
  await assert.rejects(other.responses.inputItems.list(response.id), err => err.status === 404);
  process.stdout.write(JSON.stringify({...record, response: response.id,
    response_answer: response.output_text.trim(), answer: answer.trim(), terminal}));
}
