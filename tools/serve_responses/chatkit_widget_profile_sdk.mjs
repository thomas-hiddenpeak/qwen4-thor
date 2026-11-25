// Resume a release review on the same durable widget after a profile change.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let raw = '';
for await (const chunk of process.stdin) raw += chunk;
const plan = JSON.parse(raw);
const {OpenAI, version} = loadSdk(plan.sdk);
const client = apiKey => new OpenAI({apiKey, baseURL:plan.base, maxRetries:0});
const owner = client(plan.key), peer = client(plan.other);
const release = JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-chatkit-image-attachments-20261126/delivery.json', import.meta.url)));
const revision = String(release.publication.publication_revision);
const missing = e => e.status === 404;

async function browser(grant, type, params, status=200) {
  const res = await fetch(plan.base+'/chatkit', {method:'POST', signal:AbortSignal.timeout(55000),
    headers:{Authorization:'Bearer '+grant.client_secret,'Content-Type':'application/json'},
    body:JSON.stringify({type,params})});
  assert.equal(res.status,status,type);
  if (!res.headers.get('content-type')?.includes('text/event-stream')) return res.json();
  return (await res.text()).split(/\r?\n/).filter(line=>line.startsWith('data: '))
    .map(line=>JSON.parse(line.slice(6)));
}
const session = (id,user) => owner.beta.chatkit.sessions.create({user,workflow:{
  id,state_variables:{revision}},rate_limits:{max_requests_per_1_minute:60}});
const action = (grant,thread,item_id,type,status) => browser(grant,'threads.custom_action',
  {thread_id:thread,item_id,action:{type}},status);

let source, grant;
if (plan.phase === 'primary') {
  const actions = {
    open: `const version = actionContext.state_variables.revision;
      const card = value => ({type:'Card',children:[{type:'Box',children:[
        {type:'Text',id:'version',value},{type:'Text',id:'review',value:'待复核'}]}]});
      streamWidget(card('rev')); streamWidget(card('rev'+version));
      text({state:{revision:version,reviewed:false}});`,
    close: `const state=actionContext.state;
      if (state.reviewed || state.revision!==actionContext.state_variables.revision) throw Error('invalid state');
      streamWidgetComponent('review',{type:'Text',id:'review',value:'已复核'});
      text({state:{...state,reviewed:true}});`
  };
  const file = await owner.files.create({purpose:'user_data',file:new File(
    [JSON.stringify({actions})],'release-review.json',{type:'application/json'})});
  const app = await owner.beta.agents.create({model:plan.model,reasoning:{effort:'none'},
    metadata:{q4t_chatkit_actions_file:file.id}});
  grant = await session(app.id,'record-reader');
  const opened = await browser(grant,'threads.create',{input:{attachments:[],
    inference_options:{},content:[{type:'input_text',text:'Return only the integer 2 plus 3.'}]}});
  const thread = opened.find(e=>e.type==='thread.created').thread.id;
  const user = opened.find(e=>e.type==='thread.item.done' && e.item.type==='user_message').item.id;
  const answer = opened.find(e=>e.type==='thread.item.done' && e.item.type==='assistant_message').item;
  assert.equal(answer.content.map(c=>c.text).join('').trim(),'5');
  const frames = await action(grant,thread,null,'open');
  assert.deepEqual(frames.map(e=>e.type),['stream_options','thread.item.added','thread.item.updated','thread.item.done']);
  const widget = frames.at(-1).item.id;
  assert.equal(frames[1].item.id,widget);
  assert.equal(frames[2].item_id,widget);
  assert.deepEqual(frames[2].update,{type:'widget.streaming_text.value_delta',component_id:'version',delta:revision,done:true});
  source={workflow:app.id,file:file.id,thread,user,answer:answer.id,widget};
} else {
  assert.equal(plan.phase,'light');
  source=plan.previous.source;
  grant=await session(source.workflow,'record-reader');
}
const expected = status => ({type:'Card',children:[{type:'Box',children:[
  {type:'Text',id:'version',value:'rev'+revision},{type:'Text',id:'review',value:status}]}]});
async function state(status) {
  const page=await browser(grant,'items.list',{thread_id:source.thread,order:'asc'});
  assert.deepEqual(page.data.map(i=>i.id),[source.user,source.answer,source.widget]);
  const item=page.data.at(-1);
  assert.deepEqual(item.widget,expected(status));
  const managed=await owner.beta.chatkit.threads.listItems(source.thread,{order:'asc'});
  assert.deepEqual(managed.data.map(i=>i.id),page.data.map(i=>i.id));
  assert.deepEqual(JSON.parse(managed.data.at(-1).widget),item.widget);
  return item.widget.children[0].children.map(c=>c.value);
}
const before=await state('待复核');
const sibling=await session(source.workflow,'other-reader');
await action(sibling,source.thread,source.widget,'close',404);
await assert.rejects(peer.beta.chatkit.threads.retrieve(source.thread),missing);
let recovered;
if (plan.phase==='light') {
  await owner.files.delete(source.file);
  await assert.rejects(owner.files.retrieve(source.file),missing);
  const changed=await action(grant,source.thread,source.widget,'close');
  assert.deepEqual(changed.map(e=>e.type),['stream_options','thread.item.updated','thread.item.replaced']);
  const update=changed[1].update;
  assert.equal(changed[1].item_id,source.widget);
  assert.deepEqual(update,{type:'widget.component.updated',component_id:'review',component:expected('已复核').children[0].children[1]});
  assert.equal(changed[2].item.id,source.widget);
  assert.deepEqual(changed[2].item.widget,expected('已复核'));
  await action(grant,source.thread,source.widget,'close',502);
  recovered={item:source.widget,after:await state('已复核'),component:update.component_id,duplicate:502,deleted:404};
}
const chat=await peer.chat.completions.create({model:plan.model,temperature:0,max_tokens:32,
  messages:[{role:'user',content:'Compute 17 + 25. Return only the integer.'}],
  chat_template_kwargs:{enable_thinking:false}});
process.stdout.write(JSON.stringify({phase:plan.phase,sdk:version,revision,source,before,sibling:404,peer:404,
  chat:chat.choices[0].message.content.trim(),...(recovered?{recovered}:{})}));
