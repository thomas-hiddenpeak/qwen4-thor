// Persist a real external tool wait in primary, then complete that same turn
// and recover the shared release artifact through the light capacity.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {setTimeout as delay} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';
import {releaseAgent} from './agent_webhook_turns.mjs';
let input='';
for await (const part of process.stdin) input+=part;
const p=JSON.parse(input);
const {OpenAI,version}=loadSdk(p.sdk);
assert.equal(version,'7.47.0');
const connect=key=>new OpenAI({apiKey:key,baseURL:p.base,timeout:35000,maxRetries:0});
const owner=connect(p.key), peer=connect(p.other);
const {sessions,environments}=owner.beta.agents;
const model='qwen3.8-flash-next';
const bytes=readFileSync(new URL('../../docs/evidence/serve-webhooks-20261124/delivery.json',import.meta.url));
const digest=createHash('sha256').update(bytes).digest('hex');
const revision=JSON.parse(bytes).publication.publication_revision;
assert.equal(p.revision,revision);
const path='/workspace/outputs/published.json';
async function settled(id) {
  const end=Date.now()+40000;
  let state=await sessions.retrieve(id);
  while (state.status==='in_progress') {
    assert.ok(Date.now()<end,'session still in progress');
    await delay(200);
    state=await sessions.retrieve(id);
  }
  return state;
}
async function waitForTool(id) {
  const state=await settled(id);
  assert.equal(state.status,'requires_action');
  assert.equal(state.required_actions.length,1);
  const [action]=state.required_actions;
  assert.equal(action.name,'read_release_record');
  assert.deepEqual(action.arguments,{});
  return action;
}
if (p.phase==='prepare') {
  const workspace=await environments.create({environment:{type:'openai_hosted'}});
  const environment={type:'openai_hosted',environment_id:workspace.id};
  const agent={model,reasoning:{effort:'none'}};
  const first=await sessions.create({agent:{...agent,...releaseAgent},environment});
  const second=await sessions.create({agent,environment});
  const uploaded=await environments.files.create(workspace.id,{type:'inline',path,data:bytes.toString('base64')});
  assert.equal(uploaded.path,path);
  assert.equal(uploaded.environment_id,workspace.id);
  for (const s of [first,second]) {
    const current=await sessions.retrieve(s.id);
    assert.equal(current.environment.files.find(x=>x.path===path).size_bytes,bytes.length);
  }
  await assert.rejects(peer.beta.agents.sessions.retrieve(first.id),e=>e.status===404);
  await sessions.events.create(first.id,{events:[{type:'agent.session.input.message',input:[{role:'user',
    content:[{type:'input_text',text:'Call read_release_record now with {}. After it returns, give me the actual published revision.'}]}]}]});
  const action=await waitForTool(first.id);
  process.stdout.write(JSON.stringify({phase:p.phase,sdk:version,peer:404,environment:workspace.id,
    session:first.id,sibling:second.id,file:uploaded.path,turn:action.turn_id,call:action.call_id,
    bytes:bytes.length,sha256:digest}));
} else {
  assert.equal(p.phase,'restore');
  const previous=p.previous;
  const state=await environments.retrieve(previous.environment);
  assert.equal(state.status,'connected');
  assert.equal((await sessions.turns.list(previous.sibling)).data.length,0);
  const action=await waitForTool(previous.session);
  assert.equal(action.turn_id,previous.turn);
  assert.equal(action.call_id,previous.call);
  assert.equal((await sessions.delete(previous.sibling)).deleted,true);
  await sessions.events.create(previous.session,{events:[{type:'agent.session.input.tool_result',
    turn_id:action.turn_id,call_id:action.call_id,success:true,
    output:JSON.stringify({publication_revision:revision})}]});
  assert.equal((await settled(previous.session)).status,'idle');
  const {data:turns}=await sessions.turns.list(previous.session);
  assert.equal(turns.length,1);
  assert.equal(turns[0].id,previous.turn);
  assert.equal(turns[0].status,'completed');
  const items=(await sessions.turns.items.list(previous.turn,{session_id:previous.session})).data;
  const answer=items.filter(i=>i.role==='assistant').flatMap(i=>i.content).map(x=>x.text).join('').trim();
  assert.match(answer,new RegExp('\\b'+revision+'\\b'));
  const [artifact]=(await sessions.artifacts.list(previous.session)).data;
  assert.equal(artifact.path,path);
  const content=await sessions.artifacts.content(artifact.id,{session_id:previous.session});
  assert.deepEqual(Buffer.from(await content.arrayBuffer()),bytes);
  await assert.rejects(peer.beta.agents.environments.retrieve(previous.environment),e=>e.status===404);
  const chat=await peer.chat.completions.create({model,temperature:0,max_tokens:16,
    messages:[{role:'user',content:'Return only the integer 6 times 7.'}],chat_template_kwargs:{enable_thinking:false}});
  assert.equal(chat.choices[0].message.content.trim(),'42');
  process.stdout.write(JSON.stringify({phase:p.phase,sdk:version,peer:404,previous,answer,
    artifact:artifact.id,bytes:bytes.length,sha256:digest,chat:'42',turns:[turns[0].id],call:action.call_id}));
}
