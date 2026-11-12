// Node consumer for Q4T's parsed text profile. The caller executes tools.
import { randomBytes } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import { nodeFetch } from './node_transport.mjs';

export const MAX_RESPONSE_BYTES = 8 * 1024 * 1024;
export const MAX_EVENT_BYTES = 1024 * 1024;
export const MAX_TOOL_CALLS = 128;

export class ProtocolError extends Error {}
export class RemoteError extends Error {
  constructor(error) {
    super(error.message);
    this.remote = error;
  }
}
export function requireValue(value, message) {
  if (!value) throw new ProtocolError(message);
}
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const integer = value => Number.isSafeInteger(value) && value >= 0;
// Explicit exact JSON data uses the JS engine's raw-JSON serialization;
// SDK JSON.stringify therefore sends numeric tokens, never quoted strings.
export class ExactJsonNumber {
  constructor(lexeme) {
    requireValue(typeof lexeme === 'string' && lexeme.length <= 16384 &&
      /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$/.test(lexeme),
    'invalid exact JSON number');
    this.lexeme = lexeme;
    Object.freeze(this);
  }
  toJSON() {
    requireValue(typeof JSON.rawJSON === 'function', 'exact numbers require JSON.rawJSON');
    return JSON.rawJSON(this.lexeme);
  }
  toString() { return this.lexeme; }
}

function decimalIdentity(token) {
  let negative = token.startsWith('-');
  const [mantissa, exponent = '0'] = token.replace(/^-/, '').toLowerCase().split('e');
  const fraction = (mantissa.split('.')[1] ?? '').length;
  let digits = mantissa.replace('.', '').replace(/^0+/, '');
  if (!digits) return '0';
  const zeros = /0*$/.exec(digits)[0].length;
  digits = digits.slice(0, digits.length - zeros);
  return (negative ? '-' : '') + digits + 'e' +
    (BigInt(exponent) - BigInt(fraction) + BigInt(zeros));
}

function clone(value) {
  let nodes = 0;
  function copy(item, depth) {
    requireValue(depth <= 64 && ++nodes <= 65536, 'JSON complexity limit');
    if (item instanceof ExactJsonNumber) return new ExactJsonNumber(item.lexeme);
    if (typeof item === 'bigint') return new ExactJsonNumber(item.toString());
    if (item === null || typeof item === 'boolean') return item;
    if (typeof item === 'string') return text(item, 'invalid JSON string');
    if (typeof item === 'number') {
      requireValue(Number.isFinite(item) && (!Number.isInteger(item) || Number.isSafeInteger(item)),
        'use BigInt or ExactJsonNumber for integers beyond the safe range');
      return item;
    }
    if (Array.isArray(item)) {
      requireValue(Object.keys(item).length === item.length, 'invalid JSON array');
      return Array.from(item, v => copy(v, depth + 1));
    }
    requireValue(object(item) && [Object.prototype, null].includes(Object.getPrototypeOf(item)) &&
      Object.getOwnPropertySymbols(item).length === 0, 'value must be JSON');
    const result = {};
    for (const [key, child] of Object.entries(item)) {
      text(key, 'invalid JSON key');
      Object.defineProperty(result, key, { value: copy(child, depth + 1),
        enumerable: true, writable: true, configurable: true });
    }
    return result;
  }
  return copy(value, 0);
}

export function stringifyJson(value) {
  const encoded = JSON.stringify(clone(value));
  requireValue(Buffer.byteLength(encoded) <= 16 * 1024 * 1024, 'JSON exceeds 16 MiB');
  return encoded;
}
function text(value, message, nonempty = false) {
  requireValue(typeof value === 'string' && value.isWellFormed() && (!nonempty || value.length), message);
  return value;
}
function fields(value, allowed) {
  requireValue(object(value) && Object.keys(value).every(key => allowed.includes(key)), 'unsupported object field');
}

function exactInteger(token) {
  const [mantissa, exponent = '0'] = token.toLowerCase().replace(/^-/, '').split('e');
  const fraction = (mantissa.split('.')[1] ?? '').length;
  const digits = mantissa.replace('.', '');
  if (/^0+$/.test(digits)) return true;
  const shift = Number(exponent) - fraction;
  return shift >= 0 || (digits.length + shift > 0 && /^0+$/.test(digits.slice(digits.length + shift)));
}

export function parseJson(source, { exactNumbers = false } = {}) {
  // JSON.parse alone discards duplicate keys and rounds near-integer lexemes.
  text(source, 'JSON must be Unicode text');
  let offset = 0, nodes = 0, stringBytes = 0;
  const whitespace = () => { while (/[ \t\r\n]/.test(source[offset] ?? 'x')) offset++; };
  function string() {
    const start = offset++;
    while (offset < source.length) {
      const char = source[offset++];
      if (char === '\\') { offset++; continue; }
      if (char === '"') {
        const value = JSON.parse(source.slice(start, offset));
        text(value, 'invalid Unicode JSON string');
        stringBytes += Buffer.byteLength(value);
        requireValue(stringBytes <= MAX_RESPONSE_BYTES, 'JSON string byte limit');
        return value;
      }
    }
    throw new ProtocolError('unterminated JSON string');
  }
  function value(depth) {
    requireValue(depth <= 64 && ++nodes <= 65536, 'JSON complexity limit');
    whitespace();
    const char = source[offset];
    if (char === '"') return string();
    if (char === '{' || char === '[') {
      offset++;
      const mapping = char === '{', end = mapping ? '}' : ']';
      const result = mapping ? Object.create(null) : [], seen = new Set();
      whitespace();
      if (source[offset] === end) { offset++; return result; }
      while (true) {
        let key;
        if (mapping) {
          requireValue(source[offset] === '"', 'expected JSON object key');
          key = string(); whitespace();
          requireValue(!seen.has(key) && source[offset++] === ':', 'duplicate key or missing colon');
          seen.add(key);
        }
        const item = value(depth + 1);
        if (mapping) result[key] = item; else result.push(item);
        whitespace();
        if (source[offset] === end) { offset++; return result; }
        requireValue(source[offset++] === ',', 'missing JSON separator');
        whitespace();
      }
    }
    for (const [word, result] of [['true', true], ['false', false], ['null', null]]) {
      if (source.startsWith(word, offset)) { offset += word.length; return result; }
    }
    const token = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(source.slice(offset))?.[0];
    requireValue(token && token.length <= 16384, 'invalid or over-budget JSON number');
    offset += token.length;
    const result = Number(token);
    if (exactNumbers && (!Number.isFinite(result) ||
        (Number.isInteger(result) && !Number.isSafeInteger(result)) ||
        decimalIdentity(token) !== decimalIdentity(String(result))))
      return new ExactJsonNumber(token);
    requireValue(Number.isFinite(result), 'nonfinite JSON number');
    if (Number.isInteger(result)) {
      requireValue(Number.isSafeInteger(result) && exactInteger(token), 'unsafe or rounded integer');
    }
    return result;
  }
  const result = value(0); whitespace();
  requireValue(offset === source.length, 'trailing JSON data');
  // Return ordinary dictionaries without interpreting __proto__ as setters.
  return clone(result);
}

function usage(value, choiceCount = 1) {
  requireValue(object(value), 'missing usage');
  for (const key of ['prompt_tokens', 'completion_tokens', 'total_tokens']) {
    requireValue(integer(value[key]), 'invalid usage ' + key);
  }
  requireValue(value.total_tokens === value.prompt_tokens + value.completion_tokens, 'usage total mismatch');
  const details = value.completion_tokens_details, budget = value.q4t_thinking_budget;
  if (details != null) requireValue(object(details) && integer(details.reasoning_tokens) &&
    details.reasoning_tokens <= value.completion_tokens, 'reasoning usage mismatch');
  if (details && ('accepted_prediction_tokens' in details || 'rejected_prediction_tokens' in details)) {
    requireValue(integer(details.accepted_prediction_tokens) && integer(details.rejected_prediction_tokens) &&
      details.accepted_prediction_tokens + details.rejected_prediction_tokens <= value.completion_tokens,
    'prediction usage mismatch');
  }
  if (budget != null) {
    requireValue(object(budget) && details && ['limit', 'consumed', 'forced_closure_tokens'].every(k => integer(budget[k])), 'invalid thinking budget usage');
    requireValue(budget.consumed <= budget.limit * choiceCount && budget.limit <= 2147483647 &&
      budget.forced_closure_tokens <= 14 * choiceCount && budget.consumed + budget.forced_closure_tokens === details.reasoning_tokens,
    'thinking budget usage mismatch');
  }
  if (value.prompt_tokens_details != null) requireValue(object(value.prompt_tokens_details) &&
    integer(value.prompt_tokens_details.cached_tokens) && value.prompt_tokens_details.cached_tokens <= value.prompt_tokens,
  'cached usage mismatch');
  return clone(value);
}

function argumentsObject(raw) {
  const result = parseJson(text(raw, 'arguments must be JSON text'), { exactNumbers: true });
  requireValue(object(result), 'arguments must encode an object');
  return result;
}

function message(value) {
  fields(value, ['role', 'content', 'refusal', 'reasoning', 'reasoning_content', 'tool_calls', 'function_call']);
  requireValue(value.role === 'assistant' && Object.hasOwn(value, 'content'), 'expected assistant message');
  if (value.content !== null) text(value.content, 'invalid content');
  if (value.refusal != null) text(value.refusal, 'invalid refusal');
  const reasoning = ['reasoning', 'reasoning_content'].filter(k => value[k] != null);
  requireValue(reasoning.length <= 1, 'mixed reasoning fields');
  for (const key of reasoning) text(value[key], 'invalid reasoning');
  const calls = value.tool_calls ?? [];
  requireValue(Array.isArray(calls) && calls.length <= MAX_TOOL_CALLS, 'invalid tool batch');
  const ids = new Set();
  if (value.function_call != null) {
    requireValue(!calls.length, 'mixed call dialects');
    fields(value.function_call, ['name', 'arguments']);
    text(value.function_call.name, 'missing function name', true);
    argumentsObject(value.function_call.arguments);
  }
  for (const call of calls) {
    requireValue(['function', 'custom'].includes(call?.type), 'invalid tool type');
    const kind = call.type, key = kind === 'custom' ? 'input' : 'arguments';
    fields(call, ['id', 'type', kind]);
    requireValue(!ids.has(text(call.id, 'missing tool ID', true)), 'duplicate tool ID');
    ids.add(call.id);
    fields(call[kind], ['name', key]);
    text(call[kind].name, 'missing tool name', true);
    if (kind === 'custom') text(call.custom.input, 'custom input must be text');
    else argumentsObject(call.function.arguments);
  }
  return clone(value);
}

function logprobs(value) {
  if (value === null) return null;
  fields(value, ['content', 'refusal']);
  function token(item, top) {
    fields(item, top ? ['token', 'logprob', 'bytes'] : ['token', 'logprob', 'bytes', 'top_logprobs']);
    text(item.token, 'invalid probability token');
    requireValue(typeof item.logprob === 'number' && Number.isFinite(item.logprob) && item.logprob <= 0,
      'invalid token logprob');
    requireValue(item.bytes === null || (Array.isArray(item.bytes) &&
      item.bytes.every(byte => integer(byte) && byte <= 255)), 'invalid token bytes');
    if (!top) {
      requireValue(Array.isArray(item.top_logprobs) && item.top_logprobs.length <= 20, 'invalid top_logprobs');
      for (const alternative of item.top_logprobs) token(alternative, true);
    }
  }
  for (const key of ['content', 'refusal']) {
    requireValue(value[key] === null || Array.isArray(value[key]), 'invalid logprobs list');
    for (const item of value[key] ?? []) token(item, false);
  }
  return clone(value);
}

function choiceResults(id, model, values, counts) {
  requireValue(values.length >= 1 && values.length <= 128, 'invalid choice count');
  values.sort((a, b) => a.index - b.index);
  for (const [index, value] of values.entries()) requireValue(value.index === index, 'missing or duplicate choice index');
  const checked = usage(counts, values.length);
  const choices = values.map(value => {
    const result = completion(id, model, value.message, value.finish_reason, checked, values.length);
    return { index: value.index, message: result.message, finish_reason: result.finish_reason,
      logprobs: logprobs(value.logprobs) };
  });
  if (choices.length === 1) {
    const result = completion(id, model, choices[0].message, choices[0].finish_reason, checked);
    if (choices[0].logprobs !== null) result.logprobs = choices[0].logprobs;
    return result;
  }
  return { id, model, choices, usage: checked };
}

function completion(id, model, msg, finish, counts, choiceCount = 1) {
  text(id, 'missing completion ID', true); text(model, 'missing model', true);
  const result = message(msg), calls = result.tool_calls ?? [];
  requireValue(result.refusal == null || (!calls.length && result.function_call == null),
    'refusal cannot authorize tool execution');
  requireValue(['stop', 'length', 'tool_calls', 'function_call'].includes(finish), 'unsupported finish reason');
  requireValue((finish === 'tool_calls') === (calls.length > 0), 'finish/tool batch mismatch');
  requireValue((finish === 'function_call') === (result.function_call != null), 'finish/function mismatch');
  return { id, model, message: result, finish_reason: finish, usage: usage(counts, choiceCount) };
}

function envelope(value, kind) {
  requireValue(object(value), 'response must be an object');
  if (value.error) {
    fields(value, ['error']);
    fields(value.error, ['message', 'type', 'code', 'param']);
    text(value.error.message, 'missing error message'); text(value.error.type, 'missing error type', true);
    text(value.error.code, 'missing error code', true);
    requireValue(value.error.param === null || typeof value.error.param === 'string', 'invalid error param');
    throw new RemoteError(value.error);
  }
  requireValue(value.object === kind && integer(value.created) && value.created > 0 && Array.isArray(value.choices), 'invalid response envelope');
  text(value.id, 'missing response ID', true); text(value.model, 'missing response model', true);
}

export function parseJsonWire(raw) {
  requireValue(raw.length <= MAX_RESPONSE_BYTES, 'response byte limit');
  const event = parseJson(new TextDecoder('utf-8', { fatal: true }).decode(raw));
  envelope(event, 'chat.completion');
  return { result: choiceResults(event.id, event.model, clone(event.choices), event.usage), events: [event] };
}

export function parseSseWire(raw) {
  requireValue(raw.length <= MAX_RESPONSE_BYTES, 'response byte limit');
  const source = new TextDecoder('utf-8', { fatal: true }).decode(raw);
  requireValue(source.endsWith('\n'), 'incomplete SSE line');
  let data = [], eventBytes = 0, done = false, identity, counts = null;
  const states = new Map(), events = [];
  const finished = () => states.size > 0 && [...states.values()].every(state => state.finish !== null);
  function feed(event) {
    envelope(event, 'chat.completion.chunk');
    const current = [event.id, event.model, event.created];
    identity ??= current;
    requireValue(isDeepStrictEqual(current, identity), 'response identity changed');
    events.push(event);
    if (event.choices.length === 0) {
      requireValue(finished() && counts === null, 'unexpected usage terminal');
      counts = usage(event.usage, states.size); return;
    }
    requireValue(event.choices.length >= 1 && event.choices.length <= 128 && counts === null && event.usage == null,
      'choice after usage or mixed usage');
    const indices = new Set();
    for (const choice of event.choices) {
    requireValue(integer(choice.index) && choice.index < 128 && !indices.has(choice.index), 'invalid choice index');
    indices.add(choice.index);
    if (!states.has(choice.index)) states.set(choice.index,
      { role: false, finish: null, content: '', refusal: null, reasoning: '', reasoningField: undefined, calls: [], functionCall: null, logprobs: null });
    const state = states.get(choice.index);
    requireValue(state.finish === null, 'choice after terminal');
    let { role, finish, content, reasoning, reasoningField, calls } = state;
    const delta = choice.delta;
    const probabilities = logprobs(choice.logprobs);
    if (probabilities !== null) {
      state.logprobs ??= { content: null, refusal: null };
      for (const key of ['content', 'refusal']) if (probabilities[key] !== null) {
        state.logprobs[key] ??= [];
        state.logprobs[key].push(...probabilities[key]);
      }
    }
    fields(delta, ['role', 'content', 'refusal', 'reasoning', 'reasoning_content', 'tool_calls', 'function_call']);
    if (Object.hasOwn(delta, 'role')) { requireValue(!role && delta.role === 'assistant', 'invalid role delta'); role = true; }
    requireValue(role, 'delta before role');
    if (delta.refusal != null) {
      state.refusal ??= '';
      state.refusal += text(delta.refusal, 'invalid refusal');
    }
    if (delta.content != null) content += text(delta.content, 'invalid content delta');
    for (const key of ['reasoning', 'reasoning_content']) if (delta[key] != null) {
      requireValue(!reasoningField || reasoningField === key, 'reasoning field changed');
      reasoningField = key; reasoning += text(delta[key], 'invalid reasoning delta');
    }
    requireValue(delta.tool_calls === undefined || Array.isArray(delta.tool_calls), 'invalid tool delta list');
    for (const tool of delta.tool_calls ?? []) {
      requireValue(state.functionCall === null, 'mixed call dialects');
      requireValue(integer(tool.index) && tool.index <= calls.length, 'noncontiguous tool index');
      if (tool.index === calls.length) {
        requireValue(calls.length < MAX_TOOL_CALLS && ['function', 'custom'].includes(tool.type), 'invalid tool type/count');
        const id = text(tool.id, 'missing tool ID', true), kind = tool.type;
        requireValue(!calls.some(c => c.id === id), 'duplicate tool ID');
        text(tool[kind]?.name, 'missing initial tool name', true);
        calls.push({ id, type: kind, [kind]: { name: '', [kind === 'custom' ? 'input' : 'arguments']: '' } });
      }
      const call = calls[tool.index], kind = call.type, key = kind === 'custom' ? 'input' : 'arguments';
      fields(tool, ['index', 'id', 'type', kind]);
      requireValue((tool.id ?? call.id) === call.id && (tool.type ?? kind) === kind, 'tool identity changed');
      const part = tool[kind] ?? {}; fields(part, ['name', key]);
      for (const field of ['name', key]) if (Object.hasOwn(part, field)) call[kind][field] += text(part[field], 'invalid tool delta');
    }
    if (delta.function_call != null) {
      requireValue(!calls.length, 'mixed call dialects');
      fields(delta.function_call, ['name', 'arguments']);
      state.functionCall ??= { name: '', arguments: '' };
      for (const key of ['name', 'arguments']) if (Object.hasOwn(delta.function_call, key))
        state.functionCall[key] += text(delta.function_call[key], 'invalid legacy function delta');
    }
    if (choice.finish_reason != null) {
      requireValue(['stop', 'length', 'tool_calls', 'function_call'].includes(choice.finish_reason), 'unsupported finish reason');
      finish = choice.finish_reason;
    }
    Object.assign(state, { role, finish, content, reasoning, reasoningField });
    }
  }
  for (let line of source.slice(0, -1).split('\n')) {
    eventBytes += Buffer.byteLength(line) + 1;
    requireValue(eventBytes <= MAX_EVENT_BYTES, 'SSE event byte limit');
    if (line.endsWith('\r')) line = line.slice(0, -1);
    if (!line) {
      if (data.length) {
        requireValue(!done, 'SSE data after DONE');
        const payload = data.join('\n');
        if (payload === '[DONE]') {
          requireValue(finished() && counts !== null, 'DONE without finish/usage'); done = true;
        } else feed(parseJson(payload));
      }
      data = []; eventBytes = 0;
    } else if (line.startsWith(':')) { /* heartbeat */
    } else if (line.startsWith('data:')) {
      const value = line.slice(5); data.push(value.startsWith(' ') ? value.slice(1) : value);
    } else throw new ProtocolError('unsupported SSE field');
  }
  requireValue(done && !data.length && identity && finished(), 'incomplete SSE terminal');
  const choices = [...states.entries()].map(([index, state]) => {
    const { calls, content, reasoning, reasoningField } = state;
    requireValue(state.role, 'choice missing role');
    const msg = { role: 'assistant', content: (calls.length || state.functionCall || state.refusal !== null) && !content ? null : content };
    if (state.refusal !== null) msg.refusal = state.refusal;
    if (reasoning) msg[reasoningField] = reasoning;
    if (calls.length) msg.tool_calls = calls;
    if (state.functionCall) msg.function_call = state.functionCall;
    return { index, message: msg, finish_reason: state.finish, logprobs: state.logprobs };
  });
  return { result: choiceResults(identity[0], identity[1], choices, counts), events };
}

export function prepareToolCalls(result, validators, customValidators = {}) {
  requireValue(['tool_calls', 'function_call'].includes(result.finish_reason), 'not a completed tool turn');
  return invocationCalls(message(result.message)).map(call => {
    if (call.type === 'custom') {
      const { name, input } = call.custom;
      requireValue(Object.hasOwn(customValidators, name) && typeof customValidators[name] === 'function', 'unregistered custom tool');
      requireValue(customValidators[name](input) === undefined, 'validator must return undefined or throw');
      return { call_id: call.id, type: 'custom', name, input };
    }
    const name = call.function.name, args = argumentsObject(call.function.arguments);
    requireValue(Object.hasOwn(validators, name) && typeof validators[name] === 'function', 'unregistered tool');
    requireValue(validators[name](clone(args)) === undefined, 'validator must return undefined or throw');
    return { call_id: call.id, name, arguments: args };
  });
}

function invocationCalls(msg) {
  return msg.function_call == null ? (msg.tool_calls ?? []) :
    [{ id: msg.function_call.name, type: 'function', function: msg.function_call }];
}

export function appendToolResults(history, result, results) {
  requireValue(['tool_calls', 'function_call'].includes(result.finish_reason) && object(results), 'not a completed tool batch');
  const msg = message(result.message), calls = invocationCalls(msg);
  requireValue(calls.length && calls.length === Object.keys(results).length && calls.every(c => Object.hasOwn(results, c.id)), 'provide exactly one result per tool ID');
  return [...clone(history), msg, ...calls.map(c => ({
    ...(msg.function_call != null ? { role: 'function', name: c.function.name } :
      { role: 'tool', tool_call_id: c.id }),
    content: text(results[c.id], 'tool result must be text') }))];
}

export function appendFinal(history, result) {
  requireValue(result.finish_reason === 'stop' && !result.message.tool_calls?.length &&
    !result.message.function_call, 'not a final response');
  return [...clone(history), message(result.message)];
}

export const SCHEMA_INSTRUCTION = 'Return exactly one JSON object matching the following JSON Schema. ' +
  'Use its descriptions to interpret the fields and enum values. Do not include markdown fences.\n';

export function withJsonSchema(body, schema, { name = 'answer' } = {}) {
  requireValue(object(body) && !Object.hasOwn(body, 'response_format'), 'response_format already set or invalid request');
  requireValue(object(schema) && (schema.type === 'object' || typeof schema.$ref === 'string'), 'an object JSON Schema is required');
  requireValue(typeof name === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(name), 'invalid schema name');
  let encoded, checked;
  try {
    encoded = stringifyJson(schema);
    requireValue(Buffer.byteLength(encoded) <= 2 * 1024 * 1024, 'schema exceeds 2 MiB');
    checked = parseJson(encoded, { exactNumbers: true });
  } catch (error) { throw new ProtocolError('invalid JSON schema: ' + error.message); }
  requireValue(body.tools == null || (Array.isArray(body.tools) && body.tools.length <= 128),
    'tools must be an array with at most 128 declarations');
  for (const tool of body.tools ?? [])
    requireValue(object(tool) && ['function', 'custom'].includes(tool.type) &&
      object(tool[tool.type]) && typeof tool[tool.type].name === 'string' && tool[tool.type].name.length,
    'invalid tool definition');
  requireValue(body.parse_response !== false, 'schema requires parsed messages');
  requireValue(Array.isArray(body.messages) && body.messages.length && body.messages.every(object) &&
    body.messages.some(m => ['system', 'developer', 'user', 'assistant'].includes(m.role)),
  'schema requires a valid message history');
  const result = clone(body), user = result.messages.findLast(m => m.role === 'user');
  const instruction = SCHEMA_INSTRUCTION + encoded;
  if (!user) result.messages.unshift({ role: 'system', content: instruction });
  else if (typeof user.content === 'string') user.content += '\n\n' + instruction;
  else {
    requireValue(Array.isArray(user.content), 'invalid user content');
    user.content.push({ type: 'text', text: instruction });
  }
  result.response_format = { type: 'json_schema', json_schema: { name, strict: true, schema: checked } };
  return result;
}

export function makeRequest(messages, options = {}, { jsonSchema, schemaName = 'answer' } = {}) {
  requireValue(Array.isArray(messages) && messages.length, 'messages are required');
  requireValue(!(Object.hasOwn(options, 'max_tokens') && Object.hasOwn(options, 'max_completion_tokens')),
    'provide only one output token limit');
  const request = { model: 'qwen3.8-flash-next', messages: clone(messages), parse_response: true,
    stream: true, stream_options: { include_usage: true }, enable_thinking: false,
    max_completion_tokens: 256, request_timeout_ms: 120000,
    request_id: 'js-' + randomBytes(16).toString('hex'), cancel_token: randomBytes(32).toString('hex'), ...clone(options) };
  if (Object.hasOwn(options, 'max_tokens')) delete request.max_completion_tokens;
  const budget = request.max_tokens ?? request.max_completion_tokens;
  requireValue(Number.isInteger(budget) && budget >= 1 && budget <= 2147483647, 'invalid output budget');
  if (!request.stream) delete request.stream_options;
  return jsonSchema === undefined ? request : withJsonSchema(request, jsonSchema, { name: schemaName });
}

export function validateCapabilities(value, model, protocolSha256) {
  requireValue(value?.object === 'q4t.capabilities' && value.schema_version === 1 && value.model === model, 'incompatible capabilities');
  for (const key of ['messages', 'reasoning_content', 'tool_calls', 'stream', 'stream_include_usage']) requireValue(value.chat?.[key] === true, 'missing chat capability ' + key);
  requireValue(value.tools?.type === 'function' && value.tools.execution === 'client' &&
    value.tools.tool_choice?.includes('auto') && value.tools.parallel_tool_calls?.includes(true), 'incompatible tools');
  requireValue(['qwen3.8-native-v2', 'qwen3.8-openai-v3'].includes(value.template?.message_profile),
    'incompatible message profile');
  for (const hash of [value.protocol_source_sha256, ...['reference_sha256', 'renderer_source_sha256',
    'tool_history_source_sha256', 'fixture_sha256'].map(k => value.template[k])]) requireValue(/^[0-9a-f]{64}$/.test(hash), 'invalid capability identity');
  requireValue(!protocolSha256 || value.protocol_source_sha256 === protocolSha256, 'protocol identity mismatch');
  return clone(value);
}

export class ChatClient {
  #apiKey;
  constructor(OpenAI, { baseURL = 'http://127.0.0.1:8080/v1', model = 'qwen3.8-flash-next',
    protocolSha256, timeoutMs = 120000, apiKey, caFile, connectHost, fetchImpl } = {}) {
    const url = new URL(baseURL);
    const rawPath = /^https?:\/\/[^/?#]+(\/[^?#]*)?$/.exec(baseURL)?.[1];
    requireValue(['http:', 'https:'].includes(url.protocol) && !url.username && !url.password &&
      !url.search && !url.hash && rawPath === url.pathname && url.pathname.length <= 516 &&
      /^(?:\/[A-Za-z0-9._~-]+)*\/v1\/?$/.test(url.pathname) &&
      url.pathname.split('/').every(part => part !== '.' && part !== '..'),
    'expected explicit /v1 base URL with an optional canonical path prefix');
    requireValue(integer(timeoutMs) && timeoutMs > 0 && timeoutMs <= 1200000, 'invalid total deadline');
    requireValue(apiKey === undefined || typeof apiKey === 'string' && /^[!-~]{1,4096}$/.test(apiKey), 'invalid API key');
    requireValue(fetchImpl === undefined || typeof fetchImpl === 'function' && caFile === undefined &&
      connectHost === undefined, 'custom Fetch cannot silently override CA or connection IP');
    this.OpenAI = OpenAI; this.baseURL = url.href.replace(/\/$/, ''); this.model = model;
    this.protocolSha256 = protocolSha256; this.timeoutMs = timeoutMs;
    this.#apiKey = apiKey ?? 'q4t-local-no-auth';
    this.fetchImpl = fetchImpl ?? nodeFetch(this.baseURL, { caFile, connectHost });
  }

  async transaction(action, { signal, sink } = {}) {
    const controller = new AbortController(), records = [];
    const stop = (reason = new Error('transaction deadline/abort')) => {
      controller.abort(reason);
      for (const r of records) r.reader?.cancel(controller.signal.reason).catch(() => {});
    };
    const timer = setTimeout(stop, this.timeoutMs);
    const abort = () => stop();
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) stop();
    const captureFetch = async (url, init) => {
      requireValue(records.length === 0, 'automatic retry or multiple requests forbidden');
      const record = { chunks: [], bytes: 0, complete: false }; records.push(record);
      const response = await this.fetchImpl(url, { ...init, redirect: 'error', signal: AbortSignal.any([controller.signal, ...(init.signal ? [init.signal] : [])]) });
      record.status = response.status; record.headers = Object.fromEntries(response.headers);
      requireValue(response.body, 'missing response body');
      const [sdkBody, capture] = response.body.tee(); record.reader = capture.getReader();
      record.finished = (async () => {
        try {
          while (true) {
            const { done, value } = await record.reader.read();
            requireValue(!controller.signal.aborted, 'response aborted before complete EOF');
            if (done) { record.complete = true; break; }
            record.bytes += value.byteLength;
            requireValue(record.bytes <= MAX_RESPONSE_BYTES, 'response byte limit');
            const bytes = Buffer.from(value); record.chunks.push(bytes);
            if (sink) await sink(bytes);
          }
        } catch (error) { controller.abort(error); throw error; }
        finally { record.reader.releaseLock(); record.reader = null; }
      })();
      record.finished.catch(() => {});
      return new Response(sdkBody, { status: response.status, statusText: response.statusText, headers: response.headers });
    };
    const sdk = new this.OpenAI({ baseURL: this.baseURL, apiKey: this.#apiKey,
      maxRetries: 0, timeout: this.timeoutMs, fetch: captureFetch });
    try {
      const value = await action(sdk, controller.signal);
      requireValue(records.length === 1, 'request was not captured');
      await records[0].finished;
      const record = records[0];
      requireValue(record.complete, 'incomplete response');
      return { value, raw: Buffer.concat(record.chunks), status: record.status, headers: record.headers };
    } catch (error) {
      stop(error);
      await Promise.allSettled(records.map(r => r.finished));
      error.capture = records.map(r => ({ raw: Buffer.concat(r.chunks), status: r.status,
        headers: r.headers, transport_eof: r.complete, automatic_retry: false }));
      throw error;
    } finally {
      clearTimeout(timer); signal?.removeEventListener('abort', abort);
    }
  }

  async models(options) {
    const response = await this.transaction((sdk, signal) => sdk.models.list({ signal, maxRetries: 0 }), options);
    const raw = parseJson(new TextDecoder('utf-8', { fatal: true }).decode(response.raw));
    requireValue(raw.object === 'list' && Array.isArray(raw.data) && raw.data.some(m => m.id === this.model), 'served model missing');
    requireValue(isDeepStrictEqual(response.value.data, raw.data), 'SDK model interpretation differs');
    return { models: raw, ...response, value: undefined };
  }

  async capabilities(options) {
    const response = await this.transaction((sdk, signal) => sdk.get('/capabilities', { signal, maxRetries: 0 }), options);
    const raw = parseJson(new TextDecoder('utf-8', { fatal: true }).decode(response.raw));
    return validateCapabilities(raw, this.model, this.protocolSha256);
  }

  async chat(request, options = {}) {
    requireValue(request.messages?.length && request.parse_response !== false &&
      request.model === this.model && (!request.stream || request.stream_options?.include_usage === true),
    'client requires parsed messages, served model and streamed usage');
    const response = await this.transaction(async (sdk, signal) => {
      const result = await sdk.chat.completions.create(clone(request), { signal, maxRetries: 0 });
      if (!request.stream) return result;
      const events = [];
      for await (const event of result) events.push(event);
      return events;
    }, options);
    try {
      const parsed = request.stream ? parseSseWire(response.raw) : parseJsonWire(response.raw);
      const interpreted = request.stream ? response.value : { ...response.value };
      if (!request.stream) delete interpreted._request_id;
      requireValue(isDeepStrictEqual(request.stream ? interpreted : [interpreted], parsed.events), 'SDK/wire interpretation differs');
      requireValue((parsed.result.choices?.length ?? 1) === (request.n ?? 1), 'response choice count mismatch');
      requireValue(parsed.result.model === request.model && (!request.request_id || parsed.result.id === request.request_id), 'request/response identity mismatch');
      return { ...parsed.result, raw: response.raw, headers: response.headers };
    } catch (error) {
      error.capture = [{ raw: response.raw, status: response.status, headers: response.headers,
        transport_eof: true, automatic_retry: false }];
      throw error;
    }
  }
}
