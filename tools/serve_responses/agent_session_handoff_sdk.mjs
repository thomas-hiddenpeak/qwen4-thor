// Retain one live tool call across two independently started model capacities.
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { setTimeout as delay } from 'node:timers/promises';
import { loadSdk } from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.ok(base.protocol === 'https:' && base.hostname === '127.0.0.1' &&
          base.pathname === '/v1' && !base.username && !base.password);
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const options = { baseURL: base.href, maxRetries: 0, timeout: 60000 };
const agents = new OpenAI({ ...options, apiKey: plan.key }).beta.agents;
const other = new OpenAI({ ...options, apiKey: plan.other }).beta.agents;
const sessions = agents.sessions;
const peer = other.sessions;
const denied = status => e => e instanceof OpenAI.APIError && e.status === status;
const tool = 'read_release_record';
const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const spans = trace => trace.otlp.resourceSpans.flatMap(resource =>
  resource.scopeSpans.flatMap(scope => scope.spans));
async function settled(id) {
  const until = Date.now() + 65000;
  do {
    const state = await sessions.retrieve(id);
    if (state.status !== 'in_progress') return state;
    await delay(250);
  } while (Date.now() < until);
  throw Error('Timed out waiting for the model turn');
}

async function primary() {
  const { id } = await sessions.create({
    environment: { type: 'none' },
    agent: { model: 'qwen3.8-flash-next', reasoning: { effort: 'none' },
      instructions: 'Call read_release_record for a release query. After receiving the result, answer with the revision number.',
      tools: [{ type: 'function', name: tool, description: 'Read the q4t publication receipt.',
        parameters: { type: 'object', properties: {}, required: [], additionalProperties: false } }] },
    input: 'Call read_release_record now with {} to obtain the actual publication revision. After the tool returns, reply with the number.',
  });
  const waiting = await settled(id);
  assert.equal(waiting.status, 'requires_action');
  assert.equal(waiting.required_actions.length, 1);
  const [action] = waiting.required_actions;
  assert.equal(action.type, 'function_call');
  assert.equal(action.name, tool);
  assert.deepEqual(action.arguments, {});
  assert.equal((await sessions.turns.retrieve(action.turn_id, { session_id: id })).status, 'waiting');
  await assert.rejects(peer.retrieve(id), denied(404));
  const result = { id, action, status: waiting.status, foreign: 404 };
  if (plan.environment_templates) {
    const secret = randomBytes(24).toString('hex');
    const template = await agents.environments.templates.create({name:'Qualification project',
      env:{PROJECT_TOKEN:secret},setup_commands:[{command:'echo '+secret}],
      files:[{type:'inline',path:'/workspace/record.txt',data:'cTR0'}],
      network:{access:'restricted',blocked_domains:['private.example']}});
    assert.ok(!JSON.stringify(template).includes(secret));
    assert.deepEqual(template.files,[{type:'inline',path:'/workspace/record.txt',size_bytes:3}]);
    const [trace] = (await sessions.traces.list(id)).data;
    assert.equal(trace.id,action.turn_id);
    assert.equal(spans(trace).length,1);
    await assert.rejects(other.environments.templates.retrieve(template.id),denied(404));
    result.template_trace = {template_id:template.id,template_hash:digest(template),
      waiting_step:digest(spans(trace)[0]), trace_id:spans(trace)[0].traceId};
  }
  return result;
}

async function light() {
  const { id, action } = plan.prior;
  const restored = await sessions.retrieve(id);
  assert.equal(restored.status, 'requires_action');
  assert.deepEqual(restored.required_actions, [action]);
  const handoff = plan.prior.template_trace;
  if (plan.environment_templates) {
    const saved = await agents.environments.templates.retrieve(handoff.template_id);
    assert.equal(digest(saved),handoff.template_hash);
    const [trace] = (await sessions.traces.list(id)).data;
    assert.equal(spans(trace).length,1);
    assert.equal(digest(spans(trace)[0]),handoff.waiting_step);
    const update = await agents.environments.templates.update(saved.id,{name:'Resumed project'});
    assert.deepEqual(update.files,saved.files);
    assert.equal(update.name,'Resumed project');
    await assert.rejects(other.environments.templates.delete(saved.id),denied(404));
    assert.equal((await agents.environments.templates.delete(saved.id)).deleted,true);
  }
  await assert.rejects(peer.events.stream(id), denied(404));
  const record = new URL('../../docs/evidence/serve-agent-registration-20261123/delivery.json', import.meta.url);
  const revision = JSON.parse(readFileSync(record, 'utf8')).publication.publication_revision;
  const output = JSON.stringify({ publication_revision: revision });
  const event = { type: 'agent.session.input.tool_result', turn_id: action.turn_id,
    call_id: action.call_id, success: true, output };
  const request = { events: [event], 'Idempotency-Key': 'qualification-release-record' };
  await assert.rejects(peer.events.create(id, request), denied(404));
  const stream = await sessions.events.stream(id);
  assert.equal((await sessions.events.create(id, request).asResponse()).status, 202);
  assert.equal((await sessions.events.create(id, request).asResponse()).status, 202);
  let completed = 0, idle = 0, text = '';
  for await (const e of stream) {
    assert.notEqual(e.type, 'error');
    if (e.type === 'agent.session.turn.item.added') assert.notEqual(e.item.type, 'function_call');
    if (e.type === 'agent.session.turn.output_text.delta') text += e.delta;
    if (e.type === 'agent.session.turn.completed') ++completed;
    // GET /events stays open for later turns. Stop this subscriber at the
    // first logical terminal before starting a separate follow-up subscription.
    if (e.type === 'agent.session.idle') {
      ++idle;
      break;
    }
  }
  assert.equal(completed, 1);
  assert.equal(idle, 1);
  assert.match(text, new RegExp('\\b' + revision + '\\b'));
  const turns = (await sessions.turns.list(id)).data;
  assert.equal(turns.length, 1);
  assert.equal(turns[0].id, action.turn_id);
  const items = (await sessions.turns.items.list(action.turn_id, { session_id: id, order: 'asc' })).data;
  const calls = items.filter(i => i.type === 'function_call');
  const results = items.filter(i => i.type === 'function_call_output');
  assert.equal(calls.length, 1);
  assert.equal(results.length, 1);
  assert.equal(calls[0].call_id, action.call_id);
  assert.equal(results[0].call_id, action.call_id);
  assert.equal(results[0].output, output);
  const follow = () => sessions.stream(id, {
    input: 'Repeat only the revision number from the tool result. Do not call a tool.',
    toolHandlers: { [tool]() { throw Error('Unexpected tool dispatch during follow-up'); } },
  }).finalResult();
  // The profile deliberately keeps the original 3 RPM limit. Beta, hosted
  // agent and this continuation exhaust the light client's first window.
  let failedTurn;
  await assert.rejects(follow(), err => {
    failedTurn = err.turn;
    return err.reason === 'failed' && failedTurn?.error?.code === 'rate_limit_exceeded';
  });
  const retained = await sessions.turns.retrieve(failedTurn.id, { session_id: id });
  assert.equal(retained.error.code, 'rate_limit_exceeded');
  assert.equal((await sessions.retrieve(id)).status, 'idle');
  await delay(60050);
  const final = await follow();
  assert.notEqual(final.turn_id, failedTurn.id);
  assert.notEqual(final.turn_id, action.turn_id);
  assert.match(final.output_text, new RegExp('\\b' + revision + '\\b'));
  let traceResult;
  if (plan.environment_templates) {
    const page = await sessions.traces.list(id,{order:'asc',limit:1});
    assert.equal(page.data[0].id,action.turn_id);
    const completedSpans = spans(page.data[0]);
    assert.equal(completedSpans.length,3);
    assert.equal(digest(completedSpans[0]),handoff.waiting_step);
    const root = completedSpans.find(span => !span.parentSpanId);
    assert.equal(root.traceId,handoff.trace_id);
    assert.ok(completedSpans.every(span => span===root || span.parentSpanId===root.spanId));
    const next = await page.getNextPage();
    assert.equal(next.data[0].id,failedTurn.id);
    assert.equal(spans(next.data[0]).at(-1).status.code,2);
    await assert.rejects(peer.traces.list(id),denied(404));
    traceResult = {...handoff,closed_spans:3,foreign:404,template_deleted:true};
  }
  assert.equal((await sessions.delete(id)).deleted, true);
  return { id, action, completed, idle, template_trace:traceResult,
    answer: text.trim(), next_answer: final.output_text.trim(),
    next_turn: final.turn_id, rejected_turn: failedTurn.id, quota_error: retained.error.code,
    revision, result_count: results.length, replay: 202, foreign: 404 };
}

assert.ok(['primary', 'light'].includes(plan.profile));
const row = plan.profile === 'primary' ? await primary() : await light();
process.stdout.write(JSON.stringify({ sdk: version, ...row }) + '\n');
