import assert from 'node:assert/strict';
import {setTimeout as wait} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
assert.ok(Number.isSafeInteger(plan.revision));
const options = {baseURL: plan.base, maxRetries: 0, timeout: 30000};
const owner = new OpenAI({...options, apiKey: plan.key});
const other = new OpenAI({...options, apiKey: plan.other});
const agents = owner.beta.agents;
const sessions = agents.sessions;
const autos = agents.automations;
const foreign = other.beta.agents.automations;
const wire = {};
const hidden = error => error instanceof OpenAI.APIError && error.status === 404;
async function until(get, done) {
  const deadline = Date.now() + 60000;
  do {
    const row = await get();
    if (done(row)) return row;
    await wait(600);
  } while (Date.now() < deadline);
  throw new Error('Timed out waiting for scoped delivery');
}
async function delivered(automation, id) {
  const result = await until(() => autos.invocations.retrieve(id, {automation_id: automation}),
                             row => row.status !== 'queued');
  assert.equal(result.status, 'completed', JSON.stringify(result.error));
  assert.ok(result.session_id);
  return result;
}
async function answer(sessionId, count, number) {
  await until(() => sessions.retrieve(sessionId), row => row.status === 'idle');
  const turns = await sessions.turns.list(sessionId, {order: 'asc'});
  assert.equal(turns.data.length, count);
  const turn = turns.data.at(-1);
  assert.equal(turn.status, 'completed');
  const items = await sessions.turns.items.list(turn.id, {session_id:sessionId, order:'asc'});
  const text = items.data.filter(x => x.role === 'assistant')
      .flatMap(x => x.content).map(x => x.text ?? '').join('').trim();
  assert.equal(text, String(number));
  return {turn_id:turn.id, answer:text, items:items.data};
}
wire.agent = await agents.create({name:'Release reader', model:'qwen3.8-flash-next',
  reasoning:{effort:'none'}, instructions:'Answer with the requested integer alone.'});
const instructions = 'The project release ledger records revision ' + plan.revision +
                     '. Return only that revision number.';
wire.periodic = await autos.create({type:'periodic', name:'Read the release ledger',
  expression:'0 0 1 1 *', timezone:'UTC', target:{type:'new_session',
    environment:{type:'none'}, agent_id:wire.agent.id,
    input:[{role:'user', content:[{type:'input_text', text:instructions}]}]}});
const periodic = wire.periodic.id;
wire.paused = await autos.pause(periodic);
assert.equal(wire.paused.status,'paused');
wire.updated = await autos.update(periodic,{metadata:{fixture:'manual release lookup'}});
assert.equal(wire.updated.metadata.fixture,'manual release lookup');
wire.manual = await autos.invocations.create(periodic);
assert.equal(wire.manual.status,'queued');
assert.equal(wire.manual.source.type,'manual');
await assert.rejects(foreign.retrieve(periodic),hidden);
wire.manual_result = await delivered(periodic,wire.manual.id);
const sessionId = wire.manual_result.session_id;
wire.first_turn = await answer(sessionId,1,plan.revision);
wire.resumed = await autos.resume(periodic);
assert.equal(wire.resumed.status,'active');
wire.once = await autos.create({type:'once', name:'Advance the release',
  run_at:Math.floor(Date.now()/1000)+6, target:{type:'existing_session',
    session_id:sessionId,
    instructions:'Add one to the release revision in your previous answer and return only the integer.'}});
const once = wire.once.id;
const filtered = await autos.list({session_id:sessionId});
wire.filtered = filtered.data;
assert.deepEqual(wire.filtered.map(entry=>entry.id),[once]);
const found = await until(() => autos.invocations.list(once), row => row.data.length > 0);
assert.equal(found.data.length,1);
wire.scheduled = await delivered(once,found.data[0].id);
assert.equal(wire.scheduled.scheduled_at,wire.once.run_at);
assert.equal(wire.scheduled.source.automation.id,once);
assert.equal(wire.scheduled.session_id,sessionId);
wire.second_turn = await answer(sessionId,2,plan.revision+1);
wire.completed = await autos.retrieve(once);
assert.equal(wire.completed.status,'completed');
await assert.rejects(foreign.invocations.retrieve(wire.scheduled.id,{automation_id:once}),hidden);
await assert.rejects(autos.invocations.retrieve(wire.scheduled.id,{automation_id:periodic}),hidden);
const firstPage = await autos.list({limit:1});
assert.ok(firstPage.has_more);
const next = await firstPage.getNextPage();
wire.pages = [firstPage.data, next.data];
assert.notEqual(next.data[0].id,firstPage.data[0].id);
wire.deleted = await autos.delete(once);
assert.equal(wire.deleted.deleted,true);
await assert.rejects(autos.retrieve(once),hidden);
assert.equal((await sessions.retrieve(sessionId)).id,sessionId);
await autos.delete(periodic);
await sessions.delete(sessionId);
await agents.delete(wire.agent.id);
const chat = await other.chat.completions.create({model:'qwen3.8-flash-next',
  messages:[{role:'user', content:'What is 40 + 2? Reply only with the integer.'}],
  temperature:0,max_tokens:24,chat_template_kwargs:{enable_thinking:false}});
const independent = chat.choices[0].message.content.trim();
assert.equal(independent,'42');
process.stdout.write(JSON.stringify({sdk:version, manual:wire.first_turn.answer,
  scheduled:wire.second_turn.answer, independent_chat:independent,
  foreign_status:404, cross_parent_status:404, release_revision:plan.revision, wire}));
