// Exercise a pending form over browser SSE and its original function call
// through the official management SDK, including a new session for the owner.
import assert from 'node:assert/strict';
import {randomInt} from 'node:crypto';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let config = '';
for await (const chunk of process.stdin) config += chunk;
const plan = JSON.parse(config);
const {OpenAI, version} = loadSdk(plan.sdk);
const client = key => new OpenAI({apiKey: key, baseURL: plan.base, maxRetries: 0});
const project = client(plan.key), peer = client(plan.other);
const model = plan.model, name = 'ask_for_record';

async function browser(grant, type, params, status = 200) {
  const response = await fetch(plan.base + '/chatkit', {
    method: 'POST', signal: AbortSignal.timeout(45000),
    headers: {Authorization: 'Bearer ' + grant.client_secret, 'Content-Type': 'application/json'},
    body: JSON.stringify({type, params}),
  });
  if (response.status !== status) {
    const error = await response.text();
    throw Error(type + ' HTTP ' + response.status + ' ' + error.slice(0, 320));
  }
  if (!response.headers.get('content-type')?.includes('text/event-stream')) return response.json();
  const events = (await response.text()).split(/\r?\n/).filter(s => s.startsWith('data: '))
    .map(line => JSON.parse(line.slice(6)));
  assert.ok(!events.some(event => event.type === 'error'));
  return events;
}

const question = {type: 'object', additionalProperties: false,
  properties: {id: {type: 'string', enum: ['record']},
    type: {type: 'string', enum: ['freeform']}, question: {type: 'string'}},
  required: ['id', 'type', 'question']};
const workflow = await project.beta.agents.create({model, reasoning: {effort:'none'},
  metadata: {q4t_chatkit_structured_input_tool: name},
  instructions: 'Use ask_for_record to request the numeric project record identifier from the reader. After receiving the completed form, reply with only the identifier supplied by the reader.',
  tools: [{type: 'function', name, description: 'Show one freeform question with id record asking for a numeric record identifier.',
    parameters: {type: 'object', additionalProperties: false,
      properties: {inputs: {type: 'array', items: question, minItems: 1, maxItems: 1}}, required: ['inputs']}}]});
const session = user => project.beta.chatkit.sessions.create({workflow:{id:workflow.id}, user,
  rate_limits:{max_requests_per_1_minute:40}});
const owner = await session('project-reader');
const initial = await browser(owner, 'threads.create', {input: {attachments: [],
  content: [{type:'input_text', text:'Ask me for the project record identifier using the form.'}],
  inference_options: {tool_choice: {id:name}}}});
const thread = initial.find(event => event.type === 'thread.created').thread.id;
const done = initial.filter(event => event.type === 'thread.item.done').map(event => event.item);
const form = done.find(item => item.type === 'structured_input');
assert.ok(form, 'missing structured input item');
assert.equal(form.status, 'pending');
assert.equal(form.inputs.length, 1);
assert.equal(form.inputs[0].id, 'record');
assert.equal(form.inputs[0].type, 'freeform');
assert.ok(form.inputs[0].question.length);
const rows = () => project.beta.chatkit.threads.listItems(thread, {order:'asc'});
const pending = (await rows()).data.find(item => item.id === form.id);
assert.equal(pending.type, 'chatkit.client_tool_call');
assert.equal(pending.status, 'in_progress');
assert.equal(pending.name, name);
assert.ok(pending.call_id);
assert.ok(!Object.hasOwn(pending, 'browser_form'));

const reader = await session('project-reader'), sibling = await session('different-reader');
const rpc = (grant, operation, params, code) =>
  browser(grant, operation, {thread_id: thread, ...params}, code);
const visible = (await rpc(reader,'items.list',{order:'asc'})).data;
assert.deepEqual(visible.at(-1),form);
const submit = (grant, id, input, status) =>
  rpc(grant, 'threads.add_structured_input', {item_id:id,input}, status);
await submit(sibling,form.id,{},404);
await assert.rejects(peer.beta.chatkit.threads.retrieve(thread), e => e.status === 404);
await submit(reader,visible[0].id,{},400);
await submit(reader,'item_absent',{},404);
await submit(reader,form.id,{answers:{record:{values:[4]}}},400);
await rpc(reader,'threads.add_client_tool_output',{result:'untyped'},409);
const expected = String(randomInt(10000, 100000));
const events = await submit(reader,form.id,{answers:{record:{values:[expected]}}},200);
const replaced = events.find(event => event.type === 'thread.item.replaced').item;
assert.equal(replaced.id, form.id);
assert.equal(replaced.status, 'answered');
assert.deepEqual(replaced.inputs[0].answer, {values:[expected],skipped:false});
const answer = events.find(e => e.type === 'thread.item.done' && e.item.type === 'assistant_message')
  .item.content.map(part => part.text).join('').trim();
assert.equal(answer,expected);
await submit(reader,form.id,{status:'skipped'},409);
const after = (await rows()).data.find(item => item.id === form.id);
assert.equal(after.status,'completed');
assert.equal(after.call_id,pending.call_id);
assert.deepEqual(JSON.parse(after.output), {status:'answered',inputs:replaced.inputs});
assert.deepEqual((await rpc(reader,'items.list',{order:'asc'})).data[1],replaced);
const health = await peer.chat.completions.create({model,temperature:0,max_tokens:16,
  messages:[{role:'user',content:'Return only the integer 6 times 7.'}],
  chat_template_kwargs:{enable_thinking:false}});
assert.equal(health.choices[0].message.content.trim(),'42');
process.stdout.write(JSON.stringify({sdk:version,workflow:workflow.id,thread,item:form.id,
  call:pending.call_id,expected,answer,foreign:404,duplicate:409,independent_chat:'42',
  management:[pending,after],browser:[form,replaced]}));
