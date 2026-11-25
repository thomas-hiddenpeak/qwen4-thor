// Hand off one pending form and an intervening widget through the original
// thread. The second profile uses a freshly issued owner grant.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input='';
for await (const chunk of process.stdin) input+=chunk;
const plan=JSON.parse(input);
const {OpenAI,version}=loadSdk(plan.sdk);
const client=key=>new OpenAI({apiKey:key,baseURL:plan.base,maxRetries:0});
const owner=client(plan.key),peer=client(plan.other);
const release=JSON.parse(readFileSync(new URL('../../docs/evidence/serve-chatkit-retry-20261125/delivery.json',import.meta.url)));
const expected=String(release.publication.publication_revision);
const session=(app,user)=>owner.beta.chatkit.sessions.create({workflow:{id:app,state_variables:{revision:expected}},
  user,rate_limits:{max_requests_per_1_minute:50}});
async function rpc(grant,type,params,status=200) {
  const res=await fetch(plan.base+'/chatkit',{method:'POST',signal:AbortSignal.timeout(45000),
    headers:{Authorization:'Bearer '+grant.client_secret,'Content-Type':'application/json'},
    body:JSON.stringify({type,params})});
  if(res.status!==status) {
    const error=await res.json();
    throw Error(type+' HTTP '+res.status+' '+error.error?.code+' '+error.error?.param);
  }
  if(!res.headers.get('content-type')?.includes('text/event-stream')) return res.json();
  const events=(await res.text()).split(/\r?\n/).filter(s=>s.startsWith('data: ')).map(s=>JSON.parse(s.slice(6)));
  assert.ok(!events.some(e=>e.type==='error'));
  return events;
}
const action=(grant,thread,stream,name,id=null)=>rpc(grant,
  stream?'threads.custom_action':'threads.sync_custom_action',
  {thread_id:thread, item_id:id, action:{type:name}});
async function primary() {
  const handlers={show:`text({state:{viewed:actionContext.state_variables.revision},
    widget:{type:'Card',children:[{type:'Text',value:actionContext.state_variables.revision}]}});`,
    verify:`if (!actionContext.sender) throw Error('missing sender');
    text({widget:{type:'Card',children:[{type:'Text',value:'verified '+actionContext.state.viewed}]}});`};
  const file=await owner.files.create({file:new File([JSON.stringify({actions:handlers})],
    'handoff-actions.json',{type:'application/json'}),purpose:'user_data'});
  const tool='ask_record';
  const agent=await owner.beta.agents.create({model:plan.model,reasoning:{effort:'none'},
    metadata:{q4t_chatkit_actions_file:file.id,q4t_chatkit_structured_input_tool:tool},
    instructions:'Use ask_record to ask the user for a numeric record number. After receiving the form answer return only that exact number.',
    tools:[{type:'function',name:tool,description:'Show one freeform question with id record asking for the record number.',
      parameters:{type:'object',additionalProperties:false,required:['inputs'],properties:{inputs:{
        type:'array',minItems:1,maxItems:1,items:{type:'object',additionalProperties:false,
          properties:{id:{type:'string',enum:['record']},type:{type:'string',enum:['freeform']},question:{type:'string'}},
          required:['id','type','question']}}}}}]});
  const grant=await session(agent.id,'record-reader');
  const events=await rpc(grant,'threads.create',{input:{attachments:[],
    content:[{type:'input_text',text:'Request my record number using a form.'}],inference_options:{tool_choice:{id:tool}}}});
  const thread=events.find(e=>e.type==='thread.created').thread.id;
  const form=events.find(e=>e.type==='thread.item.done' && e.item.type==='structured_input').item;
  assert.equal(form.status,'pending');
  const stream=await action(grant,thread,true,'show');
  const widget=stream.find(e=>e.type==='thread.item.done').item;
  assert.equal(widget.widget.children[0].value,expected);
  const rows=(await rpc(grant,'items.list',{thread_id:thread,order:'asc'})).data;
  assert.deepEqual(rows.map(i=>i.type),['user_message','structured_input','widget']);
  assert.equal(rows[1].id,form.id);
  const management=await owner.beta.chatkit.threads.listItems(thread,{order:'asc'});
  const call=management.data[1];assert.equal(call.status,'in_progress');
  await assert.rejects(peer.beta.chatkit.threads.retrieve(thread),e=>e.status===404);
  return {workflow:agent.id,file:file.id,thread,form:form.id,widget:widget.id,call:call.call_id,user:rows[0].id};
}
async function light(source) {
  const {workflow,file,thread,form,widget,call,user}=source;
  const reader=await session(workflow,'record-reader'),sibling=await session(workflow,'other');
  await rpc(sibling,'threads.sync_custom_action',{thread_id:thread,item_id:widget,action:{type:'verify'}},404);
  await owner.files.delete(file);
  await assert.rejects(owner.files.retrieve(file),e=>e.status===404);
  const changed=await action(reader,thread,false,'verify',widget);
  assert.equal(changed.updated_item.id,widget);
  assert.equal(changed.updated_item.widget.children[0].value,'verified '+expected);
  await rpc(reader,'threads.add_user_message',{thread_id:thread,input:{attachments:[],inference_options:{},
    content:[{type:'input_text',text:'unexpected new turn'}]}},409);
  await rpc(reader,'threads.add_client_tool_output',{thread_id:thread,result:'uncorrelated'},409);
  const reply=await rpc(reader,'threads.add_structured_input',{thread_id:thread,item_id:form,
    input:{answers:{record:{values:[expected]}}}});
  const replaced=reply.find(e=>e.type==='thread.item.replaced').item;
  assert.equal(replaced.id,form);
  const answer=reply.find(e=>e.type==='thread.item.done' && e.item.type==='assistant_message')
    .item.content.map(part=>part.text).join('').trim();
  assert.equal(answer,expected);
  const rows=(await rpc(reader,'items.list',{thread_id:thread,order:'asc'})).data;
  assert.deepEqual(rows.map(i=>i.type),['user_message','structured_input','widget','assistant_message']);
  assert.deepEqual(rows.slice(0,3).map(i=>i.id),[user,form,widget]);
  assert.equal(rows[1].status,'answered');
  const sdkRows=(await owner.beta.chatkit.threads.listItems(thread,{order:'asc'})).data;
  assert.equal(sdkRows[1].call_id,call);assert.equal(sdkRows[1].status,'completed');
  assert.equal(sdkRows[2].id,widget);
  await rpc(reader,'threads.add_structured_input',{thread_id:thread,item_id:form,input:{}},409);
  return {answer,widget_label:changed.updated_item.widget.children[0].value,answered_form:replaced.id,
    ids:rows.map(i=>i.id),call:sdkRows[1].call_id,foreign:404,deleted:404,duplicate:409,new_turn:409,untyped:409};
}
assert.ok(['primary','light','both'].includes(plan.phase));
const source=plan.phase==='light'?plan.previous.source:await primary();
const recovered=plan.phase==='primary'?undefined:await light(source);
const health=await peer.chat.completions.create({model:plan.model,temperature:0,max_tokens:16,
  messages:[{role:'user',content:'Return only the integer 6 times 7.'}],chat_template_kwargs:{enable_thinking:false}});
assert.equal(health.choices[0].message.content.trim(),'42');
process.stdout.write(JSON.stringify({sdk:version,phase:plan.phase,source,recovered,revision:expected,chat:'42'}));
