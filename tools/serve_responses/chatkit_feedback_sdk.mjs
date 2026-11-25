// Send feedback for an actual assistant message using the browser protocol.
import assert from 'node:assert/strict';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let raw = '';
for await (const piece of process.stdin) raw += piece;
const plan = JSON.parse(raw);
const {OpenAI, version} = loadSdk(plan.sdk);
const makeClient = apiKey => new OpenAI({baseURL:plan.base, apiKey, timeout:45000, maxRetries:0});
const owner = makeClient(plan.key), peer = makeClient(plan.other);
const workflow = await owner.beta.agents.create({model:plan.model, reasoning:{effort:'none'},
  instructions:'Answer with the requested integer only.'});
const session = user => owner.beta.chatkit.sessions.create({user, workflow:{id:workflow.id}});
const reader = await session('feedback-reader');
const sibling = await session('other-reader');
async function rpc(secret, type, params, expected=200) {
  const result = await fetch(plan.base+'/chatkit', {method:'POST', signal:AbortSignal.timeout(45000),
    headers:{'Content-Type':'application/json',Authorization:'Bearer '+secret},
    body:JSON.stringify({type,params})});
  assert.equal(result.status,expected);
  if (result.headers.get('content-type')?.includes('text/event-stream')) {
    return (await result.text()).split(/\r?\n/).filter(line=>line.startsWith('data: '))
      .map(line=>JSON.parse(line.slice(6)));
  }
  return result.json();
}
const events = await rpc(reader.client_secret,'threads.create',{input:{
  content:[{type:'input_text',text:'Return only the integer six times seven.'}],
  attachments:[],inference_options:{}}});
const thread = events.find(event=>event.type==='thread.created').thread.id;
const answer = events.find(event=>event.type==='thread.item.done' &&
  event.item.type==='assistant_message').item;
assert.equal(answer.content.map(part=>part.text).join('').trim(),'42');
const history = () => owner.beta.chatkit.threads.listItems(thread,{order:'asc'});
const original = (await history()).data;
assert.equal(original.length,2);
const ids = original.map(row=>row.id);
assert.equal(ids[1],answer.id);
const vote = (secret,item_ids,kind,status) =>
  rpc(secret,'items.feedback',{thread_id:thread,item_ids,kind},status);
assert.deepEqual(await vote(reader.client_secret,ids,'positive'),{});
assert.deepEqual(await vote(reader.client_secret,ids,'positive'),{});
await vote(reader.client_secret,[ids[0],'msg_missing'],'negative',404);
await vote(sibling.client_secret,ids,'negative',404);
await vote(reader.client_secret,ids,'unknown',400);
assert.deepEqual(await vote(reader.client_secret,[ids[1]],'negative'),{});
assert.deepEqual((await history()).data,original);
await assert.rejects(peer.beta.chatkit.threads.retrieve(thread),e=>e.status===404);
await owner.beta.chatkit.sessions.cancel(reader.id);
await vote(reader.client_secret,ids,'positive',401);
process.stdout.write(JSON.stringify({sdk:version,thread,items:ids,expected_votes:['positive','negative'],
  model_answer:'42',sibling:404,peer:404,missing_item:404,revoked:401,repeat:200,
  private_feedback:true,public_rows_identical:true}));
