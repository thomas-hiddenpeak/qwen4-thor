// Drive two model steps through signed notifications without polling the session.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {setTimeout as delay} from 'node:timers/promises';

export const releaseAgent = {
  instructions:'Use read_release_record for the actual current q4t publication revision. ' +
               'After the tool returns, answer with the revision number from that result.',
  tools:[{type:'function',name:'read_release_record',
    description:'Read the current q4t publication receipt.',
    parameters:{type:'object',properties:{},required:[],additionalProperties:false}}],
};
export const turnTypes = ['agent.session.idle','agent.session.in_progress',
                          'agent.session.action_required'];

export async function verifyTurns(sessions, session, observed, failure, cancel = false) {
  const id = session.id;
  const expectedData = {id,environment_type:'openai_hosted',environment_id:session.environment.id};
  async function notice(type, count) {
    const deadline = Date.now() + 50000;
    do {
      if (failure()) throw failure();
      const found = observed.filter(e => e.type === type && e.data.id === id);
      if (found.length >= count) return found[count - 1];
      await delay(150);
    } while (Date.now() < deadline);
    throw new Error('Timed out waiting for ' + type + ' occurrence ' + count);
  }
  assert.deepEqual((await notice('agent.session.idle',1)).data, expectedData);
  await sessions.events.create(id,{events:[{type:'agent.session.input.message',
    input:[{role:'user',content:[{type:'input_text',
      text:'Call read_release_record now with {}. After it returns, give me the actual published revision.'}]}]}]});
  assert.deepEqual((await notice('agent.session.in_progress',1)).data, expectedData);
  const waiting = await notice('agent.session.action_required',1);
  assert.deepEqual(waiting.data, {id,required_action:{type:'function_call'}});
  const state = await sessions.retrieve(id);
  assert.equal(state.status, 'requires_action');
  assert.equal(state.required_actions.length, 1);
  const [action] = state.required_actions;
  assert.equal(action.name, 'read_release_record');
  assert.deepEqual(action.arguments, {});
  if (cancel) {
    const request = {events:[{type:'agent.session.input.cancel'}],
      'Idempotency-Key':'cancel-waiting-lookup'};
    for (let i = 0; i < 2; ++i)
      assert.equal((await sessions.events.create(id, request).asResponse()).status, 202);
    assert.deepEqual((await notice('agent.session.idle',2)).data, expectedData);
    assert.equal((await sessions.retrieve(id)).status, 'idle');
    assert.equal((await sessions.turns.list(id)).data[0].status, 'cancelled');
    return {cancelled:true, replay:202, action:action.name};
  }
  const {publication} = JSON.parse(readFileSync(new URL(
    '../../docs/evidence/serve-webhooks-20261124/delivery.json', import.meta.url)));
  const output = JSON.stringify({publication_revision:publication.publication_revision});
  const result = {events:[{type:'agent.session.input.tool_result',
    call_id:action.call_id,turn_id:action.turn_id,success:true,output}],
    'Idempotency-Key':'webhook-release-lookup'};
  for (let i = 0; i < 2; ++i) {
    assert.equal((await sessions.events.create(id,result).asResponse()).status, 202);
  }
  const progress = await notice('agent.session.in_progress',2);
  assert.deepEqual(progress.data, expectedData);
  const finish = await notice('agent.session.idle',2);
  assert.deepEqual(finish.data, expectedData);
  assert.equal((await sessions.retrieve(id)).status, 'idle');
  const items = (await sessions.turns.items.list(action.turn_id,{session_id:id,order:'asc'})).data;
  const answer = items.filter(i=>i.role==='assistant').flatMap(i=>i.content).map(p=>p.text).join('').trim();
  assert.ok(answer.includes(String(publication.publication_revision)), 'Missing publication revision');
  assert.equal(items.filter(i=>i.type==='function_call_output').length, 1);
  assert.equal(new Set(observed.map(e=>e.id)).size, observed.length);
  return {answer, revision:publication.publication_revision, replay:202, action:action.name};
}
