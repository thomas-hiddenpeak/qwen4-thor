// Carry only public identifiers across profiles. Each replacement connection
// obtains a fresh session from the original project and workflow.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const client = key => new OpenAI({apiKey:key, baseURL:plan.base, maxRetries:0, timeout:45000});
const project = client(plan.key), peer = client(plan.other);
const sessions = project.beta.chatkit.sessions;
const source = readFileSync(new URL('../../docs/evidence/serve-scope-decision-20261120/decision.json', import.meta.url));
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const expected = String(JSON.parse(source).counts.required_inference_or_application_service);
const model = 'qwen3.8-flash-next';
const name = 'verify_scope_count';
const owner = 'handoff-reader';

async function request(grant, path, body, status=200) {
  const form = body instanceof FormData;
  const response = await fetch(plan.base + path, {method:'POST', signal:AbortSignal.timeout(45000),
    headers:{Authorization:'Bearer '+grant.client_secret,...(form?{}:{'Content-Type':'application/json'})},
    body:form?body:JSON.stringify(body)});
  if (response.status !== status) throw Error(path+' HTTP '+response.status);
  if (!response.headers.get('content-type')?.includes('text/event-stream')) return response.json();
  const events = (await response.text()).split(/\r?\n/).filter(l=>l.startsWith('data: '))
    .map(line=>JSON.parse(line.slice(6)));
  assert.ok(!events.some(event=>event.type==='error'), 'ChatKit stream failed');
  return events;
}
const rpc = (grant,type,params,status) => request(grant,'/chatkit',{type,params},status);
const session = (workflow,user) => sessions.create({workflow:{id:workflow},user,
  rate_limits:{max_requests_per_1_minute:40},chatkit_configuration:{
    history:{enabled:true},file_upload:{enabled:true,max_files:1,max_file_size:1}}});

async function prepare() {
  const workflow = await project.beta.agents.create({model,reasoning:{effort:'none'},
    instructions:'Read the count in the supplied project document and submit it to verify_scope_count. Once the tool returns the verified count, reply with that integer alone.',
    tools:[{type:'function',name,description:'Compare the supplied document count to the project scope record and return its verified value.',
      parameters:{type:'object',properties:{count:{type:'integer'}},required:['count'],additionalProperties:false}}]});
  const grant = await session(workflow.id,owner);
  const form = new FormData();
  form.set('file',new File([source],'decision.json',{type:'application/json'}));
  const file = await request(grant,'/chatkit/attachments',form);
  const bytes = Buffer.from(await (await project.files.content(file.id)).arrayBuffer());
  assert.equal(sha(bytes),sha(source));
  const events = await rpc(grant,'threads.create',{input:{attachments:[file.id],
    content:[{type:'input_text',text:'Read counts.required_inference_or_application_service in the attached JSON. Verify the count with the tool, then report its verified value.'}],
    inference_options:{tool_choice:{id:name}}}});
  const thread = events.find(e=>e.type==='thread.created').thread.id;
  const call = events.find(e=>e.type==='thread.item.done' && e.item.type==='client_tool_call').item;
  assert.equal(call.name,name);
  assert.equal(call.status,'pending');
  assert.deepEqual(call.arguments,{count:Number(expected)});
  await rpc(grant,'attachments.delete',{attachment_id:file.id});
  await assert.rejects(project.files.retrieve(file.id), e=>e.status===404);
  await assert.rejects(peer.beta.chatkit.threads.retrieve(thread), e=>e.status===404);
  return {workflow:workflow.id,thread,attachment:file.id,session:grant.id,call:call.call_id,
    proposed_count:call.arguments.count,bytes:bytes.length,sha256:sha(bytes),source_deleted:404};
}

async function restore() {
  const {thread,workflow,attachment,call} = plan.previous;
  const grant = await session(workflow,owner);
  const sibling = await session(workflow,'other-reader');
  await rpc(sibling,'threads.get_by_id',{thread_id:thread},404);
  await assert.rejects(peer.beta.chatkit.threads.retrieve(thread),e=>e.status===404);
  await assert.rejects(project.files.retrieve(attachment),e=>e.status===404);
  const before = await rpc(grant,'items.list',{thread_id:thread,order:'asc'});
  assert.equal(before.data.length,2);
  assert.equal(before.data[0].attachments[0].id,attachment);
  assert.equal(before.data[1].call_id,call);
  assert.equal(before.data[1].status,'pending');
  // Compare the original model-supplied argument to the local source of record.
  // A disagreement fails the handoff before any tool result can be returned.
  assert.deepEqual(before.data[1].arguments,{count:Number(expected)});
  assert.equal(before.data[1].arguments.count,plan.previous.proposed_count);
  const params = {thread_id:thread,result:{verified_count:before.data[1].arguments.count}};
  const events = await rpc(grant,'threads.add_client_tool_output',params);
  const replaced = events.find(e=>e.type==='thread.item.replaced').item;
  assert.equal(replaced.call_id,call);
  assert.deepEqual(replaced.output,params.result);
  const final = events.find(e=>e.type==='thread.item.done' && e.item.type==='assistant_message').item;
  const answer = final.content.map(part=>part.text).join('').trim();
  assert.equal(answer,expected);
  await rpc(grant,'threads.add_client_tool_output',params,409);
  const after = await (await project.beta.chatkit.threads.listItems(thread,{order:'asc'}).asResponse()).json();
  assert.equal(after.data.length,3);
  assert.equal(after.data[2].id,final.id);
  const healthy = await peer.chat.completions.create({model,temperature:0,max_tokens:16,
    messages:[{role:'user',content:'Return only the integer 6 times 7.'}],chat_template_kwargs:{enable_thinking:false}});
  const chat = healthy.choices[0].message.content.trim();
  assert.equal(chat,'42');
  return {thread,attachment,call,verified_count:replaced.output.verified_count,session:grant.id,other_user:404,repeat:409,answer,chat,items:after.data.length};
}

assert.ok(['primary','light'].includes(plan.phase));
const result = plan.phase==='primary' ? await prepare() : await restore();
process.stdout.write(JSON.stringify({phase:plan.phase,sdk:version,peer:404,...result}));
