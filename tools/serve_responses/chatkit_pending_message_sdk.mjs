// Exercise a user-initiated turn while the browser still holds a tool call.
import assert from 'node:assert/strict';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let raw = '';
for await (const chunk of process.stdin) raw += chunk;
const plan = JSON.parse(raw);
const {OpenAI, version} = loadSdk(plan.sdk);
const origin = new URL(plan.base);
assert.equal(origin.protocol,'https:');
assert.equal(origin.hostname,'127.0.0.1');
let requests = 0;
async function transport(input, options) {
  const url = new URL(input.url ?? input);
  assert.equal(url.origin,origin.origin);
  assert.ok(url.pathname.startsWith('/v1/') && ++requests <= 12);
  return fetch(input, options);
}
const client = key => new OpenAI({apiKey:key, baseURL:plan.base, maxRetries:0, timeout:45000,fetch:transport});
const project = client(plan.key), peer = client(plan.other);
const name = 'lookup_release';
const workflow = await project.beta.agents.create({
  model:'qwen3.8-flash-next', reasoning:{effort:'none'},
  instructions:'Use lookup_release when asked to look up a release. Answer an arithmetic question directly with only the resulting integer.',
  tools:[{type:'function', name, description:'Look up a published project release.',
    parameters:{type:'object',properties:{},required:[],additionalProperties:false}}],
});
const session = user => project.beta.chatkit.sessions.create({workflow:{id:workflow.id},user,
  rate_limits:{max_requests_per_1_minute:40},chatkit_configuration:{history:{enabled:true}}});
const grant = await session('release-reader');

async function browser(secret, type, params, expected = 200) {
  const response = await transport(plan.base+'/chatkit', {method:'POST',signal:AbortSignal.timeout(45000),
    headers:{Authorization:'Bearer '+secret,'Content-Type':'application/json'},
    body:JSON.stringify({type,params})});
  assert.equal(response.status,expected,type);
  if (!response.headers.get('content-type')?.includes('text/event-stream')) return response.json();
  const events = (await response.text()).split(/\r?\n/).filter(line => line.startsWith('data: '))
    .map(line => JSON.parse(line.slice(6)));
  assert.ok(events.length && !events.some(e => e.type === 'error'),type);
  return events;
}

const question = (text, options = {}) => ({attachments:[],content:[{type:'input_text',text}],
                                            inference_options:options});
const done = (events, kind) => events.filter(e => e.type === 'thread.item.done' && e.item.type === kind)
  .map(e => e.item);
const first = await browser(grant.client_secret,'threads.create',
  {input:question('Call lookup_release to check the project release.',{tool_choice:{id:name}})});
const thread = first.find(e => e.type === 'thread.created').thread.id;
const calls = done(first,'client_tool_call');
assert.equal(calls.length,1);
const call = calls[0];
assert.equal(call.name,name);
assert.equal(call.status,'pending');
assert.deepEqual(call.arguments,{});
const list = async () => (await browser(grant.client_secret,'items.list',{thread_id:thread,order:'asc'})).data;
const before = await list();
assert.equal(before.length,2);
assert.equal(before[1].id,call.id);
assert.equal(before[1].call_id,call.call_id);
assert.equal(before[1].status,'pending');
const follow = await browser(grant.client_secret,'threads.add_user_message',
  {thread_id:thread,input:question('New question: Compute 17 plus 25. Reply only with the number.')});
assert.equal(done(follow,'client_tool_call').length,0);
const answers = done(follow,'assistant_message');
assert.equal(answers.length,1);
const answer = answers[0].content.map(v => v.text).join('').trim();
assert.equal(answer,'42');
const items = await list();
assert.deepEqual(items.map(i => i.type),['user_message','client_tool_call','user_message','assistant_message']);
assert.deepEqual(items.slice(0,2),before);
assert.equal(items[3].id,answers[0].id);
const stale = await browser(grant.client_secret,'threads.add_client_tool_output',{thread_id:thread,
  result:{release:'late'}},409);
assert.equal(stale.error.code,'chatkit_no_pending_tool');
assert.deepEqual(await list(),items);
const sibling = await session('another-reader');
await browser(sibling.client_secret,'threads.get_by_id',{thread_id:thread},404);
await assert.rejects(peer.beta.chatkit.threads.retrieve(thread),e => e.status === 404);
const management = await project.beta.chatkit.threads.listItems(thread,{order:'asc'});
assert.deepEqual(management.data.map(i => i.id),items.map(i => i.id));
assert.equal(requests,12);
process.stdout.write(JSON.stringify({sdk:version,requests,thread,workflow:workflow.id,call:call.call_id,
  original_item:call.id,history:items.map(i => i.id),answer,late_output:409,foreign_session:404,
  foreign_project:404,unchanged_pending_call:true}));
