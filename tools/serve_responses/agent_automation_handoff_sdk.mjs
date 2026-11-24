// Leave a due invocation durably queued behind a real caller-owned function.
// A second profile or restored process completes that same function and wakeup.
import assert from 'node:assert/strict';
import {setTimeout as wait} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let raw = '';
for await (const chunk of process.stdin) {
  raw += chunk;
  assert.ok(raw.length <= 16384);
}
const plan = JSON.parse(raw);
const url = new URL(plan.base);
assert.ok(url.protocol==='https:' && url.hostname==='127.0.0.1' && url.pathname==='/v1');
assert.ok(Number.isSafeInteger(plan.revision));
const {OpenAI,version} = loadSdk(plan.sdk);
assert.equal(version,'7.47.0');
const options = {baseURL:url.href,maxRetries:0,timeout:45000};
const clients = [plan.key,plan.other].map(apiKey=>new OpenAI({...options,apiKey}));
const [{sessions,automations}, peer] = clients.map(c=>c.beta.agents);
const hidden = err=>err instanceof OpenAI.APIError && err.status===404;
async function until(read, ready) {
  const end=Date.now()+100000;
  do {
    const row=await read();
    if (ready(row)) return row;
    await wait(500);
  } while (Date.now()<end);
  throw Error('Automation handoff did not settle before its deadline');
}
const model='qwen3.8-flash-next';
const tools=[{type:'function',name:'read_release_record',
  description:'Return the actual project release number from the installed ledger.',
  parameters:{type:'object',properties:{},required:[],additionalProperties:false}}];
if (plan.phase==='prepare') {
  const opened=await sessions.create({agent:{model,tools,reasoning:{effort:'none'},
    instructions:'Call read_release_record only when explicitly asked to read the ledger. When given its result or a follow-up arithmetic question, answer with the requested integer only.'},
    environment:{type:'none'},input:'Call read_release_record now to read the release ledger. When it returns, answer only with its integer revision.'});
  const id=opened.id;
  const waiting=await until(()=>sessions.retrieve(id),s=>s.status!=='in_progress');
  assert.equal(waiting.status,'requires_action');
  assert.equal(waiting.required_actions.length,1);
  const [call]=waiting.required_actions;
  assert.equal(call.name,'read_release_record');
  const task=await automations.create({type:'once',name:'Release one-step handoff',
    run_at:Math.floor(Date.now()/1000)+3,target:{type:'existing_session',session_id:id,
      instructions:'Add one to the release revision from the last reply. Answer with the integer only.'}});
  const page=await until(()=>automations.invocations.list(task.id),p=>p.data.length>0);
  assert.equal(page.data.length,1);
  const [pending]=page.data;
  assert.equal(pending.status,'queued');
  assert.equal(pending.scheduled_at,task.run_at);
  assert.equal((await sessions.retrieve(id)).status,'requires_action');
  await assert.rejects(peer.automations.retrieve(task.id),hidden);
  process.stdout.write(JSON.stringify({phase:plan.phase, sdk:version,
    session:id,turn:call.turn_id,call:call.call_id,automation:task.id,
    invocation:pending.id,scheduled_at:task.run_at,queued:true,peer:404}));
} else {
  assert.equal(plan.phase,'restore');
  const old=plan.previous;
  const waiting=await sessions.retrieve(old.session);
  assert.equal(waiting.status,'requires_action');
  assert.equal(waiting.required_actions.length,1);
  const [call]=waiting.required_actions;
  assert.equal(call.turn_id,old.turn);
  assert.equal(call.call_id,old.call);
  const rows=await automations.invocations.list(old.automation);
  assert.deepEqual(rows.data.map(x=>[x.id,x.status]),[[old.invocation,'queued']]);
  const event={type:'agent.session.input.tool_result',turn_id:old.turn,call_id:old.call,
    success:true,output:JSON.stringify({revision:plan.revision})};
  const input={events:[event],'Idempotency-Key':'release-handoff-'+old.call};
  assert.equal((await sessions.events.create(old.session,input).asResponse()).status,202);
  const activity=await until(()=>automations.invocations.retrieve(old.invocation,
       {automation_id:old.automation}),x=>x.status!=='queued');
  assert.equal(activity.status,'completed',JSON.stringify(activity.error));
  assert.equal(activity.session_id,old.session);
  await until(()=>sessions.retrieve(old.session),s=>s.status==='idle');
  const turns=await sessions.turns.list(old.session,{order:'asc'});
  assert.equal(turns.data.length,2);
  assert.equal(turns.data[0].id,old.turn);
  const answers=[];
  for (const turn of turns.data) {
    assert.equal(turn.status,'completed');
    const items=await sessions.turns.items.list(turn.id,{session_id:old.session,order:'asc'});
    answers.push(items.data.filter(item=>item.role==='assistant').flatMap(item=>item.content)
                      .map(part=>part.text).join('').trim());
  }
  assert.deepEqual(answers,[String(plan.revision),String(plan.revision+1)]);
  const final=await automations.invocations.list(old.automation);
  assert.deepEqual(final.data.map(x=>x.id),[old.invocation]);
  await assert.rejects(peer.automations.invocations.retrieve(old.invocation,
    {automation_id:old.automation}),hidden);
  const chat=await clients[1].chat.completions.create({model,temperature:0,max_tokens:16,
    chat_template_kwargs:{enable_thinking:false},
    messages:[{role:'user',content:'What is 40 + 2? Answer with the integer.'}]});
  assert.equal(chat.choices[0].message.content.trim(),'42');
  assert.ok((await automations.delete(old.automation)).deleted);
  await sessions.delete(old.session);
  process.stdout.write(JSON.stringify({phase:plan.phase,sdk:version,previous:old,
    answers, chat:'42', peer:404, delivered:activity.id, turns:turns.data.map(t=>t.id),
    final_invocations:final.data.map(x=>x.id)}));
}
