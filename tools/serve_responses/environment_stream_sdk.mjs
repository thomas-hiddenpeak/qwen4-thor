// Observe the same workspace from two live SDK streams. Only the session
// that resumed inference may receive the associated turn identity.
import assert from 'node:assert/strict';
import {setTimeout as delay} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';
let input = '';
for await (const part of process.stdin) input += part;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
assert.equal(version, '7.47.0');
const connect = apiKey => new OpenAI({apiKey,baseURL:p.base,timeout:140000,maxRetries:0});
const owner=connect(p.key), peer=connect(p.other);
const {sessions,environments}=owner.beta.agents;
const {environment,session,sibling,idle_deadline}=p.handoff;
assert.notEqual(session,sibling);
await assert.rejects(peer.beta.agents.sessions.events.stream(session),e=>e.status===404);
const streams = [];
const wire = [];
try {
  for (const id of [session,sibling]) streams.push(await sessions.events.stream(id));
  const cursors = streams.map(s=>s[Symbol.asyncIterator]());
  assert.ok(Date.now()<idle_deadline*1000,'missed live subscription boundary');
  async function receive(index,status) {
    const id=[session,sibling][index];
    const next=await cursors[index].next();
    assert.equal(next.done,false);
    const event=next.value;
    assert.equal(event.type,'agent.session.environment.'+status);
    assert.equal(event.session_id,id);
    assert.deepEqual(event.environment,{id:environment,type:'openai_hosted',status,error:null});
    wire.push(event);
    return event;
  }
  const suspended=await Promise.all([receive(0,'suspended'),receive(1,'suspended')]);
  assert.ok(suspended.every(event=>event.turn_id===null));
  assert.equal((await environments.retrieve(environment)).status,'suspended');
  await sessions.events.create(session,{events:[{type:'agent.session.input.message',
    input:[{role:'user',content:[{type:'input_text',text:'Return only the integer 7 times 6.'}]}]}]});
  const connected=await Promise.all([receive(0,'connected'),receive(1,'connected')]);
  assert.equal(connected[1].turn_id,null);
  assert.equal(new Set(wire.map(e=>e.event_id)).size,4);
  const deadline=Date.now()+35000;
  while ((await sessions.retrieve(session)).status==='in_progress') {
    assert.ok(Date.now()<deadline);
    await delay(200);
  }
  const [turn]=(await sessions.turns.list(session)).data;
  assert.equal(turn.status,'completed');
  assert.equal(connected[0].turn_id,turn.id);
  assert.equal((await sessions.turns.list(sibling)).data.length,0);
  const {data}=await sessions.turns.items.list(turn.id,{session_id:session});
  const answer=data.filter(x=>x.role==='assistant').flatMap(x=>x.content).map(x=>x.text).join('').trim();
  assert.equal(answer,'42');
  const chat=await peer.chat.completions.create({model:p.model,temperature:0,max_tokens:16,
    messages:[{role:'user',content:'Return only the integer 17 plus 25.'}],chat_template_kwargs:{enable_thinking:false}});
  assert.equal(chat.choices[0].message.content.trim(),'42');
  process.stdout.write(JSON.stringify({sdk:version,peer:404,independent_chat:'42',answer,
    sibling_history:0,actor_turn:turn.id,changes:wire.length,wire}));
} finally {
  for (const stream of streams) stream.controller.abort();
}
