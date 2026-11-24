// Exercise the official project SDK and the session-scoped browser protocol
// through the same native TLS gateway. Never export a live credential.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
// Keep only the actual JSON from the six successful SDK methods. The parent
// validates these samples in memory before recording redacted evidence.
const wire = [];
const options = {baseURL: plan.base, maxRetries: 0, timeout: 45000, fetch: async (url, init)=>{
  const response = await fetch(url,init);
  const path = new URL(response.url).pathname.replace(/^.*?\/v1/,'');
  if (response.ok && path.startsWith('/chatkit/')) {
    const canonical = path.replace(/\/cksess_[^/]+/, '/{session_id}')
      .replace(/\/cthr_[^/]+/, '/{thread_id}');
    wire.push({method:init.method.toLowerCase(), path:canonical, body:await response.clone().json()});
  }
  return response;
}};
const project = new OpenAI({...options, apiKey: plan.key});
const peer = new OpenAI({...options, apiKey: plan.other});
const sessions = project.beta.chatkit.sessions;
const threads = project.beta.chatkit.threads;
const release = JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-webhooks-20261124/preparation-delivery.json', import.meta.url)));
const revision = String(release.publication.publication_revision);
const agent = await project.beta.agents.create({
  model: plan.model, reasoning: {effort:'none'},
  instructions: 'Use the project lookup for a new release question. Once the lookup returns, answer with the returned integer only. Answer follow-ups using the conversation.',
  tools:[{type:'function',name:'read_release_record',description:'Read the actual q4t publication receipt.',
    parameters:{type:'object',properties:{},required:[],additionalProperties:false}}],

});
const session = user => sessions.create({user, workflow: {id:agent.id},
  rate_limits:{max_requests_per_1_minute:30}});
const [reader, sibling] = await Promise.all([session('reader'), session('other-reader')]);
assert.ok(reader.client_secret.startsWith('cksec_'));
const adminDenial = new OpenAI({...options, apiKey:reader.client_secret});
await assert.rejects(adminDenial.beta.chatkit.threads.list(), e=>e.status===401);

async function browser(token, type, params, expected = 200) {
  const response = await fetch(plan.base+'/chatkit', {
    method: 'POST',
    headers: {Authorization:'Bearer '+token, 'Content-Type':'application/json'},
    body: JSON.stringify({type,params}), signal:AbortSignal.timeout(45000),
  });
  if (response.status !== expected) {
    throw new Error(type+' HTTP '+response.status+': '+(await response.text()).slice(0,512));
  }
  if (response.headers.get('content-type')?.includes('text/event-stream')) {
    const rows = (await response.text()).split(/\r?\n/).filter(l=>l.startsWith('data: '))
      .map(l=>JSON.parse(l.slice(6)));
    assert.ok(!rows.some(row=>row.type==='error'),JSON.stringify(rows.filter(row=>row.type==='error')));
    return rows;
  }
  return response.json();
}
const message = text=>({content:[{type:'input_text',text}], attachments:[], inference_options:{}});
const turn = async(type,params)=>{
  const events = await browser(reader.client_secret,type,params);
  const terminal = events.find(e=>e.type==='thread.item.done' && e.item.type==='assistant_message');
  assert.ok(terminal,'missing assistant completion');
  const answer = terminal.item.content.map(p=>p.text).join('').trim();
  const deltas = events.filter(e=>e.type==='thread.item.updated' && e.item_id===terminal.item.id &&
    e.update.type==='assistant_message.content_part.text_delta');
  assert.ok(deltas.length);
  assert.equal(deltas.map(e=>e.update.delta).join('').trim(),answer);
  return {events,answer};
};
const toolInput = message('Look up the current publication revision in the project release record.');
toolInput.inference_options.tool_choice = {id:'read_release_record'};
const opened = await browser(reader.client_secret,'threads.create',{input:toolInput});
const id = opened.find(e=>e.type==='thread.created').thread.id;
const call = opened.find(e=>e.type==='thread.item.done' && e.item.type==='client_tool_call')?.item;
assert.ok(call,'missing model tool call');
assert.equal(call.status,'pending');
assert.equal(call.name,'read_release_record');
assert.deepEqual(call.arguments,{});
await browser(sibling.client_secret,'threads.add_client_tool_output',{thread_id:id,result:0},404);
const result = {publication_revision:release.publication.publication_revision};
const first = await turn('threads.add_client_tool_output',{thread_id:id,result});
const received = first.events.find(e=>e.type==='thread.item.replaced')?.item;
assert.equal(received?.call_id,call.call_id);
assert.deepEqual(received?.output,result);
await browser(reader.client_secret,'threads.add_client_tool_output',{thread_id:id,result},409);
assert.equal(first.answer,revision);
const second = await turn('threads.add_user_message',{thread_id:id,input:message(
  'What was the publication revision from my previous message? Return only its integer.')});
assert.equal(second.answer,revision);
const listed = await threads.list({user:'reader',limit:1});
assert.deepEqual(listed.data.map(t=>t.id),[id]);
const thread = await threads.retrieve(id);
assert.equal(thread.user,'reader');
const items = await threads.listItems(id,{order:'asc'});
assert.deepEqual(items.data.map(x=>x.type),['chatkit.user_message','chatkit.client_tool_call','chatkit.assistant_message',
  'chatkit.user_message','chatkit.assistant_message']);
const page = await browser(reader.client_secret,'items.list',{thread_id:id,limit:2,order:'asc'});
assert.equal(page.after,items.data[1].id);
const next = await browser(reader.client_secret,'items.list',{thread_id:id,after:page.after,order:'asc'});
assert.deepEqual(next.data.map(x=>x.id),items.data.slice(2).map(x=>x.id));
await browser(sibling.client_secret,'threads.get_by_id',{thread_id:id},404);
assert.deepEqual((await browser(sibling.client_secret,'threads.list',{})).data,[]);
await assert.rejects(peer.beta.chatkit.threads.retrieve(id),e=>e.status===404);
await assert.rejects(peer.beta.chatkit.sessions.cancel(reader.id),e=>e.status===404);
const cancelled = await sessions.cancel(reader.id);
assert.equal(cancelled.status,'cancelled');
assert.equal(cancelled.client_secret,reader.client_secret);
await browser(reader.client_secret,'threads.get_by_id',{thread_id:id},401);
const deleted = await threads.delete(id);
assert.ok(deleted.deleted);
await assert.rejects(threads.retrieve(id),e=>e.status===404);
const chat = await peer.chat.completions.create({model:plan.model,temperature:0,max_tokens:16,
  messages:[{role:'user',content:'Return only the integer 6 times 7.'}],chat_template_kwargs:{enable_thinking:false}});
const independent = chat.choices[0].message.content.trim();
assert.equal(independent,'42');
assert.equal(new Set(wire.map(x=>x.method+' '+x.path)).size,6);
const evidence = {sdk:version,answers:[first.answer,second.answer],independent_chat:independent,
  peer:404,other_user:404,revoked:401,secret_on_admin:401,items:items.data.length,
  call_id:call.call_id,tool_result:received.output,duplicate_result:409,
  sse_events:[opened.length,first.events.length,second.events.length],wire};
process.stdout.write(JSON.stringify(evidence));
