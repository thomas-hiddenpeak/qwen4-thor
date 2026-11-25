// Exercise typed function history after the tool definition leaves the conversation.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {loadSdk} from './sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const connect = apiKey => new OpenAI({baseURL: plan.base, apiKey, maxRetries: 0, timeout: 45000});
const owner = connect(plan.key);
const peer = connect(plan.other);
const source = new URL('../../docs/evidence/serve-scope-decision-20261120/decision.json', import.meta.url);
const scope = JSON.parse(readFileSync(source)).counts.required_inference_or_application_service;
const name = 'lookup_project_scope';
const query = {role: 'user', content: 'Use the project scope lookup to read the number of required API paths.'};
const finalQuestion = {role: 'user', content: 'Audit the actual selector sent in your previous function call. Report its JSON type and the number of required paths returned by the tool. Read the recorded arguments as JSON. Report string for quoted text and null for an unquoted null.'};
const options = {model: plan.model, reasoning_effort: 'none', temperature: 0, max_completion_tokens: 128};
const responseFormat = {type: 'json_schema', json_schema: {name: 'scope_audit', strict: true, schema: {
  type: 'object', properties: {selector_type: {type: 'string', enum: ['string', 'null']},
    required_paths: {type: 'integer'}}, required: ['selector_type', 'required_paths'], additionalProperties: false}}};
async function retrieveHistory(item) {
  const page = await owner.chat.completions.messages.list(item.id, {order: 'asc'});
  const message = page.data.find(entry => entry.tool_calls?.some(call => call.id === item.call_id));
  const call = message?.tool_calls.find(call => call.id === item.call_id);
  assert.equal(call?.function.name, name);
  assert.equal(call.function.arguments, item.arguments);
  const result = page.data.find(entry => entry.role === 'tool' && entry.tool_call_id === item.call_id);
  assert.equal(result?.content, JSON.stringify({required_paths: scope}));
  await assert.rejects(peer.chat.completions.messages.list(item.id), error => error.status === 404);
  return [query, {role: 'assistant', content: null, tool_calls: [call]},
    {role: 'tool', tool_call_id: call.id, content: result.content}, finalQuestion];
}

async function complete(messages, stream) {
  const body = {...options, messages, response_format: responseFormat, store: true};
  let id, text = '', terminal, promptTokens;
  if (stream) {
    const events = await owner.chat.completions.create({...body, stream: true, stream_options: {include_usage: true}});
    for await (const chunk of events) {
      id = chunk.id;
      for (const item of chunk.choices) {
        text += item.delta.content ?? '';
        if (item.finish_reason) terminal = item.finish_reason;
      }
      if (chunk.usage) promptTokens = chunk.usage.prompt_tokens;
    }
  } else {
    const completion = await owner.chat.completions.create(body);
    id = completion.id;
    text = completion.choices[0].message.content;
    terminal = completion.choices[0].finish_reason;
    promptTokens = completion.usage.prompt_tokens;
  }
  assert.equal(terminal, 'stop');
  assert.ok(promptTokens > 0);
  return {id, answer: JSON.parse(text), terminal, prompt_tokens: promptTokens};
}

const cases = [];
for (const value of [null, 'null']) {
  const expected = value === null ? 'null' : 'string';
  let call, messages, sourceId;
  if (plan.previous) {
    const previous = plan.previous.cases.find(item => item.expected === expected);
    assert.deepEqual(JSON.parse(previous.arguments), {selector: value});
    messages = await retrieveHistory(previous);
    call = messages[1].tool_calls[0];
    sourceId = previous.id;
  } else {
    const selector = value === null ? {type: 'null'} : {type: 'string', enum: [value]};
    const tool = {type: 'function', function: {name, description: 'Read the saved scope decision.',
      strict: true, parameters: {type: 'object', properties: {selector}, required: ['selector'], additionalProperties: false}}};
    const initial = await owner.chat.completions.create({...options, messages: [query], tools: [tool],
      tool_choice: {type: 'function', function: {name}}});
    const choice = initial.choices[0];
    assert.equal(choice.finish_reason, 'tool_calls');
    assert.equal(choice.message.tool_calls.length, 1);
    [call] = choice.message.tool_calls;
    assert.equal(call.function.name, name);
    assert.deepEqual(JSON.parse(call.function.arguments), {selector: value});
    messages = [query, {role: 'assistant', content: null, tool_calls: [call]},
      {role: 'tool', tool_call_id: call.id, content: JSON.stringify({required_paths: scope})}, finalQuestion];
  }
  const item = {expected, arguments: call.function.arguments, call_id: call.id,
    ...await complete(messages, value !== null)};
  if (sourceId) item.recovered_from = sourceId;
  process.stderr.write(JSON.stringify({expected, arguments: item.arguments, answer: item.answer}) + '\n');
  await retrieveHistory(item);
  cases.push(item);
}
process.stdout.write(JSON.stringify({sdk: version, cases, source_paths: scope, protected_peer: 404}));
