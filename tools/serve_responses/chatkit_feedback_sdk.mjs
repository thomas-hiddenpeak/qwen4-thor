// Exercise browser feedback and entity history alongside the management SDK.
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let rawPlan = '';
for await (const piece of process.stdin) rawPlan += piece;
const plan = JSON.parse(rawPlan);
const {OpenAI, version} = loadSdk(plan.sdk);
const wire = [];
const verbs = new WeakMap();
async function fetchSdk(input, init) {
  const response = await fetch(input, init);
  verbs.set(response, (init?.method || input.method || 'GET').toLowerCase());
  return response;
}
const client = apiKey => new OpenAI({baseURL:plan.base, apiKey, timeout:45000,
  maxRetries:0, fetch:fetchSdk});
const owner = client(plan.key), peer = client(plan.other);
const threads = '/chatkit/threads', threadPath = threads+'/{thread_id}';
async function observe(method, path, actualPath, request) {
  const response = await request.asResponse();
  assert.equal(verbs.get(response), method);
  assert.equal(new URL(response.url).pathname, new URL(plan.base).pathname+actualPath);
  wire.push({method,path,status:response.status,value:await response.clone().json()});
  return await request;
}
function receipt() {
  const validator = fileURLToPath(new URL('../serve_audit/sdk_wire_receipt.py', import.meta.url));
  const result = spawnSync(plan.python, ['-B',validator,plan.official], {
    input:JSON.stringify(wire), encoding:'utf8', timeout:20000, maxBuffer:1024*1024});
  if (result.status !== 0) throw Error('Official schema check failed: '+result.stderr);
  return JSON.parse(result.stdout);
}
async function rpc(secret, type, params, expected=200) {
  const response = await fetch(plan.base+'/chatkit', {method:'POST',
    signal:AbortSignal.timeout(45000),
    headers:{'Content-Type':'application/json',Authorization:'Bearer '+secret},
    body:JSON.stringify({type,params})});
  assert.equal(response.status, expected);
  if (response.headers.get('content-type')?.includes('text/event-stream')) {
    return (await response.text()).split(/\r?\n/).filter(line=>line.startsWith('data: '))
      .map(line=>JSON.parse(line.slice(6)));
  }
  return response.json();
}
const message = content => ({content,attachments:[],inference_options:{}});
const text = value => ({type:'input_text',text:value});
const answer = events => events.find(event=>event.type==='thread.item.done' &&
  event.item.type==='assistant_message').item.content.map(part=>part.text).join('').trim();
const officialRows = thread => observe('get',threadPath+'/items',threads+'/'+thread+'/items',
  owner.beta.chatkit.threads.listItems(thread,{order:'asc'}));
const session = (user, workflow) => observe('post','/chatkit/sessions','/chatkit/sessions',
  owner.beta.chatkit.sessions.create({user,workflow:{id:workflow}}));
const cancel = id => observe('post','/chatkit/sessions/{session_id}/cancel',
  '/chatkit/sessions/'+id+'/cancel',owner.beta.chatkit.sessions.cancel(id));
const vote = (reader,thread_id,item_ids,kind,status=200) =>
  rpc(reader.client_secret,'items.feedback',{thread_id,item_ids,kind},status);

async function exercise() {
  if (plan.delete_thread) {
    const thread = plan.delete_thread;
    const erased = await observe('delete',threadPath,threads+'/'+thread,
      owner.beta.chatkit.threads.delete(thread));
    assert.equal(erased.deleted,true);
    await assert.rejects(owner.beta.chatkit.threads.retrieve(thread),e=>e.status===404);
    return {deleted:thread,after_delete:404};
  }
  const caps = await owner.get('/capabilities');
  assert.equal(caps.chatkit.item_feedback,true);
  assert.equal(caps.chatkit.entity_mentions,true);
  const workflow = plan.previous?.workflow ?? (await observe('post','/agents','/agents',
    owner.beta.agents.create({model:plan.model,reasoning:{effort:'none'},
      instructions:'Answer with the requested integer only.'}))).id;
  const reader = await session('feedback-reader',workflow);
  const sibling = await session('other-reader',workflow);
  const tag = {type:'input_tag',id:'q4t-current-release',text:'rev'+plan.revision,
    group:'releases',interactive:true,data:{source:'project release'}};
  let thread = plan.previous?.thread;
  if (!thread) {
    const events = await rpc(reader.client_secret,'threads.create',{input:message([
      text('Return only the numeric revision shown in the following mention: '),tag])});
    thread = events.find(event=>event.type==='thread.created').thread.id;
    assert.equal(answer(events),String(plan.revision));
    const emitted = events.find(event=>event.type==='thread.item.done' && event.item.type==='user_message');
    assert.deepEqual(emitted.item.content.at(-1),tag);
  }
  const original = (await officialRows(thread)).data;
  assert.equal(original.length,2);
  const ids = original.map(row=>row.id);
  if (plan.previous) assert.deepEqual(ids,plan.previous.items);
  const sdkText = original[0].content.at(-1);
  assert.deepEqual(sdkText,text('@'+tag.text));
  const modelAnswer = original[1].content.map(part=>part.text).join('').trim();
  assert.equal(modelAnswer,String(plan.revision));
  const listed = await observe('get',threads,threads,owner.beta.chatkit.threads.list());
  assert.ok(listed.data.some(item=>item.id===thread));
  const detail = await observe('get',threadPath,threads+'/'+thread,owner.beta.chatkit.threads.retrieve(thread));
  assert.equal(detail.id,thread);
  const browser = await rpc(reader.client_secret,'items.list',{thread_id:thread,order:'asc'});
  assert.deepEqual(browser.data[0].content.at(-1),tag);
  const [firstVote, secondVote] = plan.previous ? ['negative','positive'] : ['positive','negative'];
  assert.deepEqual(await vote(reader,thread,ids,firstVote),{});
  assert.deepEqual(await vote(reader,thread,ids,firstVote),{});
  await rpc(reader.client_secret,'items.feedback',{item_ids:ids,kind:secondVote},400);
  await vote(reader,thread,[ids[0],'msg_missing'],secondVote,404);
  await vote(sibling,thread,ids,secondVote,404);
  await vote(reader,thread,ids,'unknown',400);
  assert.deepEqual(await vote(reader,thread,[ids[1]],secondVote),{});
  assert.deepEqual((await officialRows(thread)).data,original);
  await assert.rejects(peer.beta.chatkit.threads.retrieve(thread),e=>e.status===404);
  let continued;
  if (plan.previous) {
    const follow = await rpc(reader.client_secret,'threads.add_user_message',{thread_id:thread,
      input:message([text('Repeat only the same revision number from your earlier answer.')])});
    continued = answer(follow);
    assert.equal(continued,modelAnswer);
  }
  await cancel(reader.id);
  await vote(reader,thread,ids,'positive',401);
  return {sdk:version,workflow,thread,items:ids,expected_votes:[firstVote,secondVote],
    model_answer:modelAnswer,continued,entity_id:tag.id,browser_tag:true,sdk_mention:sdkText.text,
    sibling:404,peer:404,missing_item:404,revoked:401,repeat:200,public_rows_identical:true};
}
const result = await exercise();
process.stdout.write(JSON.stringify({...result,wire:receipt()}));
