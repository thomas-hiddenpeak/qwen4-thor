// Carry a live browser action and its pending model question through a real
// profile or process change. Only the identifiers cross the client boundary.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const open = apiKey => new OpenAI({apiKey, baseURL:plan.base, maxRetries:0});
const owner = open(plan.key), peer = open(plan.other);
const release = JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-chatkit-actions-20261125/delivery.json', import.meta.url)));
const revision = String(release.publication.publication_revision);
const session = (id,user) => owner.beta.chatkit.sessions.create({
  workflow:{id,state_variables:{revision}},user,rate_limits:{max_requests_per_1_minute:50}});

async function request(grant, type, params, expected=200) {
  const response = await fetch(plan.base+'/chatkit',{method:'POST',signal:AbortSignal.timeout(45000),
    headers:{Authorization:'Bearer '+grant.client_secret,'Content-Type':'application/json'},
    body:JSON.stringify({type,params})});
  assert.equal(response.status,expected,type);
  if (!response.headers.get('content-type')?.includes('text/event-stream')) return response.json();
  return (await response.text()).split(/\r?\n/).filter(line=>line.startsWith('data: '))
    .map(line=>JSON.parse(line.slice(6)));
}
const action = (grant, thread_id, type, item_id=null, status=200) =>
  request(grant,'threads.custom_action',{thread_id,item_id,action:{type}},status);

async function prepare() {
  const actions = {
    show:`progress('Reading project record'); const r=actionContext.state_variables.revision;
      text({state:{revision:r},title:'Pending '+r,widget:{type:'Card',children:[{type:'Text',value:r}]}});`,
    invalid:"progress('Checking');text({remove_sender:true,title:7});",
    finish:`progress('Closing the review');text({notices:[{level:'info',message:'Review saved'}],
      remove_sender:true,title:'Verified '+actionContext.state.revision});`};
  const file=await owner.files.create({purpose:'user_data',file:new File([JSON.stringify({actions})],
    'release-review.json',{type:'application/json'})});
  const name='ask_record';
  const app=await owner.beta.agents.create({model:plan.model,reasoning:{effort:'none'},
    metadata:{q4t_chatkit_actions_file:file.id,q4t_chatkit_structured_input_tool:name},
    instructions:'Use ask_record once. Ask for the release number with a freeform question with id record. After submission reply only with the number.',
    tools:[{type:'function',name,description:'Ask for the release number with one freeform question named record.',
      parameters:{type:'object',additionalProperties:false,required:['inputs'],properties:{inputs:{
        type:'array',minItems:1,maxItems:1,items:{type:'object',additionalProperties:false,
          properties:{id:{type:'string',enum:['record']},type:{type:'string',enum:['freeform']},question:{type:'string'}},
          required:['id','type','question']}}}}}]});
  const grant=await session(app.id,'review-reader');
  const opened=await request(grant,'threads.create',{input:{attachments:[],inference_options:{tool_choice:{id:name}},
    content:[{type:'input_text',text:'Ask me for the release number. Use ask_record with the freeform id record.'}]}});
  const thread=opened.find(e=>e.type==='thread.created').thread.id;
  const form=opened.find(e=>e.type==='thread.item.done' && e.item.type==='structured_input').item;
  assert.equal(form.status,'pending');
  const shown=await action(grant,thread,'show');
  const widget=shown.find(e=>e.type==='thread.item.done').item;
  assert.equal(widget.widget.children[0].value,revision);
  const saved=await request(grant,'threads.get_by_id',{thread_id:thread});
  assert.deepEqual(shown.at(-1).thread,saved);
  assert.equal(saved.title,'Pending '+revision);
  const items=await owner.beta.chatkit.threads.listItems(thread,{order:'asc'});
  assert.equal(items.data[1].id,form.id);
  assert.equal(items.data[1].status,'in_progress');
  return {workflow:app.id,file:file.id,thread,form:form.id,widget:widget.id,call:items.data[1].call_id,
    user:saved.items.data[0].id};
}
async function restore(source) {
  const {workflow,file,thread,form,widget,user,call}=source;
  const reader=await session(workflow,'review-reader'), other=await session(workflow,'another-reader');
  await action(other,thread,'finish',widget,404);
  await assert.rejects(peer.beta.chatkit.threads.retrieve(thread),e=>e.status===404);
  await owner.files.delete(file);
  const get=()=>request(reader,'threads.get_by_id',{thread_id:thread});
  const before=await get();
  assert.equal(before.title,'Pending '+revision);
  assert.deepEqual(before.items.data.map(i=>i.id),[user,form,widget]);
  const failed=await action(reader,thread,'invalid',widget);
  assert.deepEqual(failed.map(e=>e.type),['stream_options','progress_update','error']);
  assert.deepEqual(await get(),before);
  const done=await action(reader,thread,'finish',widget);
  const kinds=done.map(e=>e.type);
  assert.deepEqual(kinds,['stream_options','progress_update','notice','thread.item.removed','thread.updated']);
  assert.equal(done[0].stream_options.allow_cancel,true);
  assert.equal(done[3].item_id,widget);
  const saved=await get();
  assert.equal(saved.title,'Verified '+revision);
  assert.deepEqual(done[4].thread,saved);
  assert.deepEqual(saved.items.data.map(i=>i.id),[user,form]);
  await action(reader,thread,'finish',widget,404);
  const reply=await request(reader,'threads.add_structured_input',{thread_id:thread,item_id:form,
    input:{answers:{record:{values:[revision]}}}});
  const submitted=reply.find(e=>e.type==='thread.item.replaced').item;
  assert.equal(submitted.id,form);
  const message=reply.find(e=>e.type==='thread.item.done' && e.item.type==='assistant_message').item;
  const answer=message.content.map(c=>c.text).join('').trim();
  assert.equal(answer,revision);
  const rows=(await owner.beta.chatkit.threads.listItems(thread,{order:'asc'})).data;
  assert.deepEqual(rows.map(i=>i.id),[user,form,message.id]);
  assert.equal(rows[1].call_id,call);
  assert.equal(rows[1].status,'completed');
  await request(reader,'threads.add_structured_input',{thread_id:thread,item_id:form,input:{}},409);
  return {answer,title:saved.title,removed:done[3].item_id,answered_form:submitted.id,call:rows[1].call_id,
    ids:rows.map(i=>i.id),events:kinds,late_error:failed.at(-1).type,peer:404,missing:404,duplicate:409};
}
assert.ok(['primary','light'].includes(plan.phase));
const source=plan.phase==='primary'?await prepare():plan.previous.source;
const recovered=plan.phase==='light'?await restore(source):undefined;
const fresh=await peer.chat.completions.create({model:plan.model,temperature:0,max_tokens:16,
  chat_template_kwargs:{enable_thinking:false},messages:[{role:'user',content:'What is 17 plus 25? Reply only with the number.'}]});
const chat=fresh.choices[0].message.content.trim();
assert.equal(chat,'42');
process.stdout.write(JSON.stringify({sdk:version,phase:plan.phase,source,recovered,revision,chat}));
