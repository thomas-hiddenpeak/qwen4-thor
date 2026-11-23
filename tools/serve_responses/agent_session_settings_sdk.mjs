// Check a live update, owner-filtered list, and immutable original model turn.
import assert from 'node:assert/strict';
import { loadSdk } from '../serve_protocol/sdk.mjs';
let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.ok(base.protocol === 'https:' && base.hostname === '127.0.0.1' && base.pathname === '/v1');
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const options = { baseURL:base.href, timeout:35000, maxRetries:0 };
const ses = new OpenAI({...options,apiKey:plan.key}).beta.agents.sessions;
const peer = new OpenAI({...options,apiKey:plan.other}).beta.agents.sessions;
const initial = await ses.create({ agent:{ model:'qwen3.8-flash-next',reasoning:{effort:'none'} },
  environment:{type:'none'}, metadata:{team:'original'}, input:'Reply with only the number 42.' });
const id = initial.id;
const patch = { metadata:{team:'release',project:'q4t'}, review:'auto_review',
  spend_control:{limit:35}, agent:{reasoning:{effort:'low'},service_tier:'default'} };
const saved = await ses.update(id,patch);
assert.equal(saved.agent.reasoning.effort,'low');
assert.equal(saved.review,'auto_review');
assert.equal(saved.spend_control.limit,35);
const request = {agent_id:initial.agent.id, metadata:{team:'release',project:'q4t'},limit:1};
assert.deepEqual((await ses.list(request)).data.map(r=>r.id),[id]);
assert.equal((await ses.list({metadata:{team:'original'}})).data.length,0);
assert.equal((await peer.list(request)).data.length,0);
await assert.rejects(peer.update(id,patch),e=>e instanceof OpenAI.APIError && e.status===404);
const until = Date.now()+45000;
let session = saved;
while (session.status==='in_progress' && Date.now()<until) {
  await new Promise(r=>setTimeout(r,300));
  session=await ses.retrieve(id);
}
assert.equal(session.status,'idle');
const items=(await ses.items.list(id,{order:'asc'})).data;
const answer=items.filter(i=>i.role==='assistant').flatMap(i=>i.content).map(p=>p.text).join('');
assert.match(answer,/\b42\b/);
const cleared=await ses.update(id,{metadata:null,spend_control:{limit:null},agent:{tools:null}});
assert.deepEqual(cleared.metadata,{});
assert.ok(!('spend_control' in cleared));
assert.equal((await ses.list(request)).data.length,0);
assert.equal((await ses.delete(id)).deleted,true);
process.stdout.write(JSON.stringify({sdk:version,answer,matched:1,peer_matches:0,peer_update:404,
  review:saved.review,effort:saved.agent.reasoning.effort,limit:saved.spend_control.limit,cleared:true})+'\n');
