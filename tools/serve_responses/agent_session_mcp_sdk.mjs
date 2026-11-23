// One actual Agent MCP call and its project citation, through two TLS peers.
import assert from 'node:assert/strict';
import { loadSdk } from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base), target = new URL(plan.url);
assert.ok(base.protocol === 'https:' && base.hostname === '127.0.0.1' && base.pathname === '/v1');
assert.ok(target.protocol === 'https:' && target.hostname === 'localhost' && target.pathname === '/mcp');
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const client = key => new OpenAI({baseURL: base.href, apiKey: key, timeout: 75000, maxRetries: 0});
const owner = client(plan.key), other = client(plan.other);
const vaults = owner.beta.agents.vaults, sessions = owner.beta.agents.sessions;
const capability = (await owner.get('/capabilities')).agent_sessions;
assert.deepEqual(capability.mcp, {connection_origin:'service',transport:'http',
  vault_auth:['static_bearer','mcp_oauth_access_token']});
const vault = await vaults.create({name: 'project records'});
const credentials = vaults.credentials;
const auth = plan.refresh ? {type:'mcp_oauth', mcp_server_url:target.href,
  access_token:'expired-fixture', expires_at:'2000-01-01T00:00:00Z', refresh:plan.refresh}
  : {type:'static_bearer', mcp_server_url:target.href, token:plan.secret};
const credential = await credentials.create(vault.id, {name: 'read records', auth});
const transport = {type:'http', server_url:target.href, headers:{'x-q4t-project':plan.header}};
const tool = {type:'mcp', server_label:'project', transport, required:true,
  allowed_tools:['read_project_record'], request_metadata:{project:'q4t'}};
const req = t => ({agent:{model:'qwen3.8-flash-next', reasoning:{effort:'none'},
  instructions:'Always call the project MCP tool read_project_record exactly once to answer document questions. After it returns, output only the exact source citation.',
  tools:[t]}, vault_ids:[vault.id], environment:{type:'none'},
  input:'Call read_project_record now with {"path":"docs/MODEL.md","start_line":1,"line_count":3}. Return its exact citation.'});
const status = code => e => e instanceof OpenAI.APIError && e.status === code;
const failure = async (make, code) => {
  // SDK create returns an AgentSessionCreatePromise that is also an async iterable.
  await assert.rejects(async () => await make(), status(code));
};
await failure(() => other.beta.agents.sessions.create(req(tool)), 404);
await failure(() => sessions.create(req({...tool, credential_id:credential.id,
  transport:{...transport, server_url:target.href+'?other=1'}})), 404);
const duplicate = await credentials.create(vault.id, {name:'duplicate',auth});
await failure(() => sessions.create(req(tool)), 409);
await credentials.delete(duplicate.id, {vault_id:vault.id});
if (plan.retry_after !== undefined) {
  assert.ok(plan.refresh && Number.isInteger(plan.retry_after) &&
            plan.retry_after > 0 && plan.retry_after <= 8);
  await failure(() => sessions.create(req(tool)), 503);
  await failure(() => sessions.create(req(tool)), 503);
  await new Promise(resolve => setTimeout(resolve, plan.retry_after * 1000 + 100));
}
const stream = (await sessions.create({...req(tool), stream:true})).withResultCollection();
const events = [];
for await (const event of stream) {
  assert.ok(events.length < 256 && !JSON.stringify(event).includes(plan.secret) &&
            !JSON.stringify(event).includes(plan.header));
  events.push(event);
}
const result = await stream.finalResult();
assert.equal(result.turn.status, 'completed');
assert.equal(events.at(-1).type, 'agent.session.idle');
const id = result.session_id;
const view = await sessions.retrieve(id);
assert.ok(!JSON.stringify(view).includes(plan.header));
const items = (await sessions.items.list(id, {order:'asc'})).data;
assert.ok(!JSON.stringify(items).includes(plan.secret));
assert.equal(items.filter(row=>row.type==='mcp_list_tools').length, 0);
const calls = items.filter(row=>row.type==='mcp_call');
assert.equal(calls.length, 1);
const call = calls[0];
assert.deepEqual(call.arguments, {path:'docs/MODEL.md',start_line:1,line_count:3});
assert.equal(call.status,'completed');
assert.equal(call.error, null);
assert.equal(call.output.structuredContent.source.citation, plan.citation);
assert.ok(result.output_text.includes(plan.citation), 'reply must cite actual source');
const done = events.filter(event=>event.type==='agent.session.turn.item.done' && event.item.id===call.id);
assert.equal(done.length, 1);
assert.deepEqual(done[0].item, call);
const visible = items.filter(row=>row.role !== 'user');
assert.equal(done[0].output_index, visible.findIndex(row=>row.id===call.id));
if (plan.refresh) {
  const options = {vault_id:vault.id};
  const after = await credentials.retrieve(credential.id,options);
  assert.ok(Date.parse(after.auth.expires_at) > Date.now());
  assert.equal(after.auth.refresh.scope, plan.refresh.scope);
  assert.ok(!JSON.stringify(after).includes(plan.secret));
  await credentials.update(credential.id, {...options, auth:{type:'mcp_oauth',
    expires_at:'2000-01-01T00:00:00Z'}});
  await failure(() => sessions.create(req(tool)), 502);
  await failure(() => sessions.create(req(tool)), 409);
}
await credentials.delete(credential.id, {vault_id:vault.id});
await failure(() => sessions.create(req({...tool, credential_id:credential.id})), 404);
await failure(() => sessions.create(req(tool)), 404);
await failure(() => other.beta.agents.sessions.retrieve(id), 404);
const chat = await owner.chat.completions.create({model:'qwen3.8-flash-next',temperature:1,top_p:0,
  n:2, seed:444, max_tokens:40, chat_template_kwargs:{enable_thinking:false},messages:[{role:'user',content:'Reply with only the number 42.'}]});
assert.deepEqual(chat.choices.map(c=>[c.index,c.message.content.trim()]),[[0,'42'],[1,'42']]);
await sessions.delete(id);
await vaults.delete(vault.id);
process.stdout.write(JSON.stringify({sdk:version, calls:1, citation:plan.citation, events:events.length,
  public_index:done[0].output_index, wrong_tenant:404, wrong_target:404, ambiguous:409, revoked:404, chat_choices:[0,1], chat:'42'})+'\n');
