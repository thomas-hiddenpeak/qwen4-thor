// Run project-owned app handlers for the same thread over SSE and the
// synchronous browser API, then compare the persisted management record.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const client = key => new OpenAI({apiKey: key, baseURL: plan.base, maxRetries: 0});
const owner = client(plan.key), peer = client(plan.other);
const release = JSON.parse(readFileSync(new URL(plan.widget_stream ? '../../docs/evidence/serve-chatkit-image-attachments-20261126/delivery.json' : '../../docs/evidence/serve-chatkit-actions-20261125/delivery.json',import.meta.url)));
const revision = release.publication.publication_revision;
const count = release.method_declarations.exact_success_declarations;
const model = plan.model;

const observed = [];
async function browser(grant, type, params, status = 200) {
  const res = await fetch(plan.base+'/chatkit', {method:'POST', signal:AbortSignal.timeout(45000),
    headers:{Authorization:'Bearer '+grant.client_secret,'Content-Type':'application/json'},
    body:JSON.stringify({type,params})});
  if (res.status !== status) {
    const info = await res.json();
    throw Error(type+' HTTP '+res.status+' '+info.error?.code+' '+info.error?.param);
  }
  if (status !== 200) return res.json();
  if (!res.headers.get('content-type')?.includes('text/event-stream')) {
    const body = await res.json();
    observed.push({type, json:body});
    return body;
  }
  const events = (await res.text()).split(/\r?\n/).filter(s=>s.startsWith('data: '))
    .map(s=>JSON.parse(s.slice(6)));
  observed.push({type, events});
  return events;
}
const source = {
  lookup: `const {action,state,state_variables,streaming} = actionContext;
const fields = {revision:state_variables.revision,methods:state_variables.methods};
if (!Object.hasOwn(fields,action.payload.field)) throw Error('unknown record');
const views = (state?.views ?? 0)+1;
const widget = {type:'Card',children:[{type:'Text',value:String(fields[action.payload.field])}]};
const reply = {state:{views},widget};
if (streaming) {
  reply.progress=['Reading release record'];
  reply.effects=[{name:'record_selected',data:{views}}];
}
text(reply);`,
  acknowledge: `progress('Reading the selected record');
    text({notices:[{level:'info',title:'Project record',message:'Review complete'}],
      title:actionContext.action.payload,remove_sender:true});`,
  cancel_probe: `progress('cancel-window');
    let value=1;
    for(let chunk=0;chunk<12;++chunk) {
      for(let i=0;i<80000;++i) value=Math.imul(value,33)+i|0;
      progress(String(chunk));
    }
    text({title:actionContext.action.payload+String(value)});`,
  bad_unicode: 'progress(String.fromCharCode(0xd800));text({})',
  interrupted: `progress('Checking before commit');text({title:17,remove_sender:true});`,
  inspect: "text({widget:{type:'Basic',children:[{type:'Text',value:String(actionContext.state.views)}]}});",
  remember: "text({state:{...actionContext.state,note:actionContext.action.payload}});",
  broken: "text({state:{views:100000},widget:{type:'Text'}});",
  echo_note: "text({widget:{type:'Card',children:[{type:'Text',value:actionContext.state.note}]}});",
};
if (plan.widget_stream) {
  source.cards = `const card = value => ({type:'Card',children:[{type:'Box',children:[
      {type:'Markdown',id:'summary',value}]}]});
    streamWidget(card('发行')); progress('Reading version');
    streamWidget(card('发行 '+actionContext.state_variables.revision));
    streamWidget(card('已核验')); text({state:{...actionContext.state,reviewed:true}});`;
  source.replace_component = `const {target, component} = actionContext.action.payload;
    streamWidgetComponent(target, component);`;
  source.component_failure = `streamWidgetComponent('release', {type:'Text',id:'attempt',value:'pending'});
    streamWidgetComponent('missing', {type:'Text',value:'invalid'});`;
  source.card_failure = `streamWidget({type:'Card',children:[]});text({title:2})`;
  source.card_cancel = `streamWidget({type:'Card',children:[]});
    let checksum=1;
    for(let i=0;i<24;i++){
      for(let j=0;j<80000;j++) checksum=Math.imul(checksum,33)+j|0;
      progress(String(checksum));
    }
    text({title:'card committed'});`;
}
const file = await owner.files.create({purpose:'user_data',
  file:new File([JSON.stringify({actions:source})],'release-actions.json',{type:'application/json'})});
const deploy = (c, fileId) => c.beta.agents.create({model, reasoning:{effort:'none'},
  metadata:{q4t_chatkit_actions_file:fileId}});
const workflow = await deploy(owner,file.id);
const session = (c, workflowId, user) => c.beta.chatkit.sessions.create({workflow:{id:workflowId,
  state_variables:{revision,methods:count}},user,rate_limits:{max_requests_per_1_minute:60}});
const foreignWorkflow = await deploy(peer,file.id);
await assert.rejects(session(peer,foreignWorkflow.id,'reader'), e=>e.status===404);
const first = await session(owner,workflow.id,'reader'), reader=await session(owner,workflow.id,'reader');
const sibling=await session(owner,workflow.id,'sibling');
const initial=await browser(first,'threads.create',{input:{attachments:[],inference_options:{},
  content:[{type:'input_text',text:'Reply only with the integer 2 plus 3.'}]}});
const thread=initial.find(e=>e.type==='thread.created').thread.id;
const user=initial.find(e=>e.type==='thread.item.done' && e.item.type==='user_message').item;
assert.equal(initial.find(e=>e.type==='thread.item.done' && e.item.type==='assistant_message')
  .item.content.map(c=>c.text).join('').trim(),'5');
await owner.files.delete(file.id);
await assert.rejects(owner.files.retrieve(file.id),e=>e.status===404);
await assert.rejects(session(owner,workflow.id,'new'),e=>e.status===404);

const act = (g,stream,type,payload,item_id,status=200) => browser(g,
  stream?'threads.custom_action':'threads.sync_custom_action',
  {thread_id:thread,item_id,action:{type,payload}},status);
await act(sibling,false,'lookup',{field:'revision'},null,404);
await assert.rejects(peer.beta.chatkit.threads.retrieve(thread),e=>e.status===404);
await act(first,false,'not_deployed',{},null,404);
const wrong = await act(first,true,'lookup',{field:'revision'},user.id);
assert.deepEqual(wrong,[{type:'error',code:'stream.error',allow_retry:false}]);
await act(first,false,'lookup',{field:'revision'},user.id,400);
await act(first,true,'lookup',{field:'revision'},'item_absent',404);
const created = await act(first,true,'lookup',{field:'revision'},null);
assert.deepEqual(created.map(e=>e.type),['stream_options','progress_update','client_effect','thread.item.added','thread.item.done']);
assert.equal(created[2].data.views,1);
const widget = created.at(-1).item;
assert.equal(widget.widget.children[0].value,String(revision));
const changed = await act(reader,false,'lookup',{field:'methods'},widget.id);
assert.equal(changed.updated_item.id,widget.id);
assert.equal(changed.updated_item.widget.children[0].value,String(count));
await act(reader,false,'broken',{},widget.id,502);
const state=await act(reader,true,'inspect',{},widget.id);
assert.deepEqual(state.map(e=>e.type),['stream_options','thread.item.replaced']);
assert.equal(state[1].item.widget.children[0].value,'2');
const note="reader's \"release report\"\n二次复核";
const empty=await act(reader,false,'remember',note,widget.id);
assert.deepEqual(empty,{updated_item:null});
const echoed=await act(first,false,'echo_note',null,widget.id);
assert.equal(echoed.updated_item.widget.children[0].value,note);
const sdkPage=await (await owner.beta.chatkit.threads.listItems(thread,{order:'asc'}).asResponse()).json();
const history=await browser(reader,'items.list',{thread_id:thread,order:'asc'});
assert.equal(sdkPage.data.length,3);
assert.equal(sdkPage.data[2].type,'chatkit.widget');
assert.equal(sdkPage.data[2].id,widget.id);
assert.deepEqual(JSON.parse(sdkPage.data[2].widget),echoed.updated_item.widget);
assert.deepEqual(history.data[2],echoed.updated_item);
const snapshot = () => browser(reader,'threads.get_by_id',{thread_id:thread});
await act(reader,true,'bad_unicode',{},widget.id,502);
// The same handler must complete and change the title when nobody disconnects.
const control = await act(reader,true,'cancel_probe','completed-',widget.id);
const controlTitle = control.at(-1).thread.title;
assert.ok(controlTitle.startsWith('completed-'));
const controller = new AbortController();
const flight = await fetch(plan.base+'/chatkit',{method:'POST',
  signal:AbortSignal.any([controller.signal,AbortSignal.timeout(10000)]),
  headers:{Authorization:'Bearer '+reader.client_secret,'Content-Type':'application/json'},
  body:JSON.stringify({type:'threads.custom_action',params:{thread_id:thread,item_id:widget.id,
    action:{type:'cancel_probe',payload:'cancelled-'}}})});
assert.equal(flight.status,200);
const chunks = flight.body.getReader();
let prefix='';
const decoder = new TextDecoder();
for (;;) {
  const chunk=await chunks.read();
  assert.equal(chunk.done,false,'handler ended before cancellation');
  prefix+=decoder.decode(chunk.value,{stream:true});
  if(prefix.includes('cancel-window')) break;
}
controller.abort();
await chunks.cancel().catch(()=>{});
// The worker and its parent have a two-second transaction deadline.
await new Promise(resolve=>setTimeout(resolve,2100));
assert.equal((await snapshot()).title,controlTitle);
const before = await snapshot();
const partial = await act(reader,true,'interrupted',{},widget.id);
assert.deepEqual(partial.map(e=>e.type),['stream_options','progress_update','error']);
assert.equal(partial[0].stream_options.allow_cancel,true);
assert.equal(partial[1].text,'Checking before commit');
assert.deepEqual(await snapshot(),before);
const title = 'Verified project release '+revision;
const finished = await act(reader,true,'acknowledge',title,widget.id);
assert.deepEqual(finished.map(e=>e.type),
  ['stream_options','progress_update','notice','thread.item.removed','thread.updated']);
assert.equal(finished[1].text,'Reading the selected record');
assert.equal(finished[2].level,'info');
assert.equal(finished[3].item_id,widget.id);
const committed = await snapshot();
assert.equal(committed.title,title);
assert.deepEqual(finished[4].thread,committed);
assert.deepEqual(committed.items.data.map(item=>item.id),before.items.data.slice(0,-1).map(item=>item.id));
const managed = await owner.beta.chatkit.threads.retrieve(thread);
assert.equal(managed.title,title);
const visible = await browser(reader,'threads.list',{});
assert.equal(visible.data.find(t=>t.id===thread).title,title);
await act(reader,true,'acknowledge',title,widget.id,404);
let widgetStream;
if (plan.widget_stream) {
  const frames = await act(reader,true,'cards',{},null);
  assert.deepEqual(frames.map(e=>e.type), ['stream_options','thread.item.added',
    'progress_update','thread.item.updated','thread.item.updated','thread.item.done']);
  const id = frames[1].item.id;
  assert.equal(frames[3].item_id,id);
  assert.equal(frames[4].item_id,id);
  assert.deepEqual(frames[3].update,{type:'widget.streaming_text.value_delta',
    component_id:'summary',delta:' '+revision,done:true});
  assert.equal(frames[4].update.type,'widget.root.updated');
  const last = frames.at(-1).item;
  assert.equal(last.id,id);
  assert.deepEqual(last.widget,frames[4].update.widget);
  const beforeFailure = await snapshot();
  assert.deepEqual(beforeFailure.items.data.at(-1),last);
  const management = await (await owner.beta.chatkit.threads.listItems(thread,{order:'asc'}).asResponse()).json();
  assert.equal(management.data.at(-1).id,id);
  assert.deepEqual(JSON.parse(management.data.at(-1).widget),last.widget);
  await act(sibling,true,'cards',{},id,404);
  const failed = await act(reader,true,'card_failure',{},id);
  assert.deepEqual(failed.map(e=>e.type),['stream_options','thread.item.updated','error']);
  assert.deepEqual(await snapshot(),beforeFailure);
  const abort = new AbortController();
  const res = await fetch(plan.base+'/chatkit',{method:'POST',
    headers:{Authorization:'Bearer '+reader.client_secret,'Content-Type':'application/json'},
    signal:AbortSignal.any([abort.signal,AbortSignal.timeout(10000)]),
    body:JSON.stringify({type:'threads.custom_action',params:{thread_id:thread,
      action:{type:'card_cancel',payload:null}}})});
  assert.equal(res.status,200);
  const body=res.body.getReader(), decoder=new TextDecoder();
  let wire='';
  while(!wire.includes('thread.item.added')) {
    const chunk=await body.read();
    assert.ok(!chunk.done,'expected a provisional widget while the worker was running');
    wire+=decoder.decode(chunk.value,{stream:true});
  }
  assert.ok(!wire.includes('thread.item.done'));
  abort.abort();
  await body.cancel().catch(()=>{});
  await new Promise(resolve=>setTimeout(resolve,2100));
  assert.deepEqual(await snapshot(),beforeFailure);
  const branch = {type:'Box',id:'release',children:[
    {type:'Text',id:'revision',value:String(revision)},
    {type:'Text',id:'status',value:'待复核'}]};
  async function replace(target,component) {
    const events = await act(reader,true,'replace_component',{target,component},id);
    assert.deepEqual(events.map(e=>e.type),
      ['stream_options','thread.item.updated','thread.item.replaced']);
    assert.equal(events[1].item_id,id);
    assert.deepEqual(events[1].update,
      {type:'widget.component.updated',component_id:target,component});
    assert.equal(events[2].item.id,id);
    return events;
  }
  const inserted = await replace('summary',branch);
  const leaf = {type:'Text',value:'复核通过'};
  const changed = await replace('status',leaf);
  const final = structuredClone(last);
  const expected = structuredClone(branch);
  expected.children[1] = leaf;
  final.widget.children[0].children[0] = expected;
  assert.deepEqual(changed[2].item,final);
  const durable = await snapshot();
  assert.deepEqual(durable.items.data.at(-1),final);
  const page = await (await owner.beta.chatkit.threads.listItems(thread,{order:'asc'}).asResponse()).json();
  assert.equal(page.data.at(-1).id,id);
  assert.deepEqual(JSON.parse(page.data.at(-1).widget),final.widget);
  await act(sibling,true,'replace_component',{target:'release',component:leaf},id,404);
  await act(reader,true,'replace_component',{target:'status',component:leaf},id,502);
  const rolledBack = await act(reader,true,'component_failure',{},id);
  assert.deepEqual(rolledBack.map(e=>e.type),['stream_options','thread.item.updated','error']);
  assert.deepEqual(await snapshot(),durable);
  widgetStream={frames,failed,management,client_cancelled_after_added:true,
    component:{inserted,changed,rolledBack,final,management:page.data.at(-1),sibling:404,missing:502}};
}
await owner.beta.chatkit.sessions.cancel(reader.id);
await act(reader,false,'inspect',{},widget.id,401);
const health=await peer.chat.completions.create({model,temperature:0,max_tokens:16,
  messages:[{role:'user',content:'Return only the integer 6 times 7.'}],chat_template_kwargs:{enable_thinking:false}});
assert.equal(health.choices[0].message.content.trim(),'42');
process.stdout.write(JSON.stringify({sdk:version,thread,widget:widget.id,workflow:workflow.id,revision,
  methods:count,independent_chat:'42',foreign:404,deleted:404,revoked:401,invalid_output:502,
  cancelled_after_progress:true, completed_control:controlTitle, observed, events:[...wrong,...created,...state,...partial,...finished],sync:[changed,empty,echoed],management:sdkPage,
  persisted_views:2,updated_note:note, widgetStream}));
