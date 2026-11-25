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
const release = JSON.parse(readFileSync(new URL('../../docs/evidence/serve-chatkit-retry-20261125/delivery.json',import.meta.url)));
const revision = release.publication.publication_revision;
const count = release.method_declarations.exact_success_declarations;
const model = plan.model;

async function browser(grant, type, params, status = 200) {
  const res = await fetch(plan.base+'/chatkit', {method:'POST', signal:AbortSignal.timeout(45000),
    headers:{Authorization:'Bearer '+grant.client_secret,'Content-Type':'application/json'},
    body:JSON.stringify({type,params})});
  if (res.status !== status) {
    const info = await res.json();
    throw Error(type+' HTTP '+res.status+' '+info.error?.code+' '+info.error?.param);
  }
  if (status !== 200 || !res.headers.get('content-type')?.includes('text/event-stream'))
    return res.json();
  return (await res.text()).split(/\r?\n/).filter(s=>s.startsWith('data: '))
    .map(s=>JSON.parse(s.slice(6)));
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
  inspect: "text({widget:{type:'Basic',children:[{type:'Text',value:String(actionContext.state.views)}]}});",
  remember: "text({state:{...actionContext.state,note:actionContext.action.payload}});",
  broken: "text({state:{views:100000},widget:{type:'Text'}});",
  echo_note: "text({widget:{type:'Card',children:[{type:'Text',value:actionContext.state.note}]}});",
};
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
assert.deepEqual(created.map(e=>e.type),['progress_update','client_effect','thread.item.added','thread.item.done']);
assert.equal(created[1].data.views,1);
const widget = created.at(-1).item;
assert.equal(widget.widget.children[0].value,String(revision));
const changed = await act(reader,false,'lookup',{field:'methods'},widget.id);
assert.equal(changed.updated_item.id,widget.id);
assert.equal(changed.updated_item.widget.children[0].value,String(count));
await act(reader,false,'broken',{},widget.id,502);
const state=await act(reader,true,'inspect',{},widget.id);
assert.deepEqual(state.map(e=>e.type),['thread.item.replaced']);
assert.equal(state[0].item.widget.children[0].value,'2');
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
await owner.beta.chatkit.sessions.cancel(reader.id);
await act(reader,false,'inspect',{},widget.id,401);
const health=await peer.chat.completions.create({model,temperature:0,max_tokens:16,
  messages:[{role:'user',content:'Return only the integer 6 times 7.'}],chat_template_kwargs:{enable_thinking:false}});
assert.equal(health.choices[0].message.content.trim(),'42');
process.stdout.write(JSON.stringify({sdk:version,thread,widget:widget.id,workflow:workflow.id,revision,
  methods:count,independent_chat:'42',foreign:404,deleted:404,revoked:401,invalid_output:502,
  events:[...wrong,...created,...state],sync:[changed,empty,echoed],management:sdkPage,
  persisted_views:2,updated_note:note}));
