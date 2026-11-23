import assert from 'node:assert/strict';
import {loadSdk} from '../serve_protocol/sdk.mjs';
let input = '';
for await (const bytes of process.stdin) input += bytes;
const plan = JSON.parse(input);
const {OpenAI,version}=loadSdk(plan.sdk);
assert.equal(version,'7.47.0');
const options={baseURL:plan.base, maxRetries:0, timeout:30000};
const a=new OpenAI({...options,apiKey:plan.key});
const b=new OpenAI({...options,apiKey:plan.other});
const sessions=a.beta.agents.sessions;
const project=await a.beta.agents.create({model:'qwen3.8-flash-next',
  name:'release lookup', instructions:'Answer directly using the given project record.'});
const session=await sessions.create({agent_id:project.id,environment:{type:'none'},
  agent:{reasoning:{effort:'none'}},input:'The project release record says the deployment revision is 442. Return only that revision number.'});
assert.equal(session.object,'agent.session');
const expect=(n)=>e=>e instanceof OpenAI.APIError && e.status===n;
await assert.rejects(b.beta.agents.sessions.retrieve(session.id),expect(404));
let result=session;
const until=Date.now()+75000;
while(result.status==='in_progress' && Date.now()<until) {
  await new Promise(r=>setTimeout(r,400));
  result=await sessions.retrieve(session.id);
}
assert.equal(result.status,'idle');
assert.ok(result.usage.total_tokens>0);
const turns=await sessions.turns.list(session.id);
assert.equal(turns.data.length,1);
const turn=await sessions.turns.retrieve(turns.data[0].id,{session_id:session.id});
assert.equal(turn.status,'completed');
const items=await sessions.items.list(session.id,{order:'asc'});
const page=await sessions.turns.items.list(turn.id,{session_id:session.id,limit:1,order:'asc'});
const next=await page.getNextPage();
assert.equal(page.data[0].role,'user');
assert.equal(next.data[0].id,items.data[1].id);
const answer=items.data.filter(x=>x.role==='assistant').flatMap(x=>x.content).map(x=>x.text).join('').trim();
assert.match(answer,/\b442\b/);
await assert.rejects(b.beta.agents.sessions.turns.retrieve(turn.id,{session_id:session.id}),expect(404));
const deleted=await sessions.delete(session.id);
assert.equal(deleted.deleted,true);
await assert.rejects(sessions.retrieve(session.id),expect(404));
await a.beta.agents.delete(project.id);
process.stdout.write(JSON.stringify({sdk:version,answer,usage:result.usage,turn:turn.status,
  foreign_status:404,deleted:true,items:items.data.length,pagination:true}));
