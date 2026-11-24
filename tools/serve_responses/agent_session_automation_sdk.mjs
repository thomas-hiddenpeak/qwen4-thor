// Exercise the model-owned wakeup and its subsequent turn over native TLS.
import assert from 'node:assert/strict';
import {setTimeout as wait} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.ok(base.protocol === 'https:' && base.hostname === '127.0.0.1' &&
          base.pathname === '/v1' && !base.username && !base.password);
assert.ok(Number.isSafeInteger(plan.revision));
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const options = {baseURL: base.href, maxRetries: 0, timeout: 80000};
const owner = new OpenAI({...options, apiKey: plan.key});
const other = new OpenAI({...options, apiKey: plan.other});
const {automations, sessions} = owner.beta.agents;
const hidden = e => e instanceof OpenAI.APIError && e.status === 404;
const wire = {};
const model = 'qwen3.8-flash-next';
const text = 'Return only the integer ' + (plan.revision + 1) + '.';
const args = {action:'create', type:'once', name:'Release follow-up',
              delay_seconds:16, instructions:text};
const stream = (await sessions.create({
  stream:true, environment:{type:'none'},
  agent:{model, reasoning:{effort:'none'}, tools:[{type:'automations'}],
         instructions:'Use manage_automation only when the user explicitly asks to schedule a wakeup. After a tool result, answer the user. Never reschedule a delivered reminder.'},
  input:'Schedule a one-time release follow-up in this session. Call manage_automation exactly once with ' +
        JSON.stringify(args) + '. After the tool has returned, respond only with ' +
        plan.revision + '.'
})).withResultCollection();
const events = [];
for await (const event of stream) {
  assert.notEqual(event.type, 'agent.session.requires_action');
  if (event.type === 'error') throw Error(event.error.message);
  events.push({type:event.type, item_type:event.item?.type, call_id:event.item?.call_id});
}
const result = await stream.finalResult();
const id = result.session_id;
assert.equal(result.turn.status,'completed');
assert.equal(result.output_text.trim(),String(plan.revision));
wire.turn = result.turn;
const first = await sessions.turns.items.list(result.turn.id,{session_id:id,order:'asc'});
wire.items = first.data;
const calls = first.data.filter(row=>row.type==='function_call');
const receipts = first.data.filter(row=>row.type==='function_call_output');
assert.equal(calls.length,1);
assert.equal(receipts.length,1);
const [call] = calls;
assert.equal(call.name,'manage_automation');
assert.equal(call.status,'completed');
assert.equal(call.call_id,receipts[0].call_id);
const schedule = JSON.parse(receipts[0].output);
assert.ok(schedule.id.startsWith('automation_'));
assert.ok(events.some(e=>e.type==='agent.session.turn.item.done' && e.call_id===call.call_id));
assert.ok(!events.some(e=>e.type==='agent.session.turn.item.added' &&
                            e.item_type==='function_call' && e.call_id===call.call_id));
wire.automation = await automations.retrieve(schedule.id);
assert.deepEqual(wire.automation.created_by,
                 {type:'agent', session_id:id, tool_call_id:call.call_id});
assert.equal(wire.automation.target.type,'existing_session');
assert.equal(wire.automation.target.session_id,id);
assert.equal(wire.automation.target.instructions,text);
const list = await automations.list({session_id:id});
assert.deepEqual(list.data.map(row=>row.id),[schedule.id]);
await assert.rejects(other.beta.agents.automations.retrieve(schedule.id),hidden);
const deadline = Date.now() + 55000;
do {
  const invocations = await automations.invocations.list(schedule.id);
  if (invocations.data.length > 0) {
    assert.equal(invocations.data.length,1);
    const [invocation] = invocations.data;
    if (invocation.status !== 'queued') {
      assert.equal(invocation.status,'completed',JSON.stringify(invocation.error));
      assert.equal(invocation.session_id,id);
      wire.invocation = invocation;
      const state = await sessions.retrieve(id);
      const turns = await sessions.turns.list(id,{order:'asc'});
      if (state.status === 'idle' && turns.data.length===2) {
        wire.delivered_turn = turns.data[1];
        assert.equal(wire.delivered_turn.status,'completed');
        const items = await sessions.turns.items.list(wire.delivered_turn.id,{session_id:id,order:'asc'});
        const answer = items.data.filter(row=>row.role==='assistant').flatMap(row=>row.content)
                           .map(row=>row.text).join('').trim();
        assert.equal(answer,String(plan.revision+1));
        assert.ok(!items.data.some(row=>row.type==='function_call'));
        wire.delivered_items = items.data;
        break;
      }
    }
  }
  await wait(400);
} while (Date.now()<deadline);
assert.ok(wire.delivered_items);
wire.finished = await automations.retrieve(schedule.id);
assert.equal(wire.finished.status,'completed');
const chat = await other.chat.completions.create({model,temperature:0,max_tokens:24,
  chat_template_kwargs:{enable_thinking:false},
  messages:[{role:'user',content:'What is 40 + 2? Reply only with the integer.'}]});
const independent = chat.choices[0].message.content.trim();
assert.equal(independent,'42');
assert.ok((await automations.delete(schedule.id)).deleted);
await sessions.delete(id);
process.stdout.write(JSON.stringify({sdk:version,manual:result.output_text.trim(),
  scheduled:String(plan.revision+1), independent_chat:independent,
  release_revision:plan.revision, foreign_status:404,
  session:id, call_id:call.call_id, automation:schedule.id,
  sse_events:events.length, wire}));
