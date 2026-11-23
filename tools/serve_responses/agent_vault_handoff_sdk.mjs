// Primary creates a scoped credential. Light receives only its ID and URL.
import assert from 'node:assert/strict';
import { loadSdk } from '../serve_protocol/sdk.mjs';
let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) < 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base), target = new URL(plan.url);
assert.ok(base.protocol === 'https:' && base.hostname === '127.0.0.1' && base.pathname === '/v1');
assert.ok(target.protocol === 'https:' && target.hostname === 'localhost' && target.pathname === '/mcp');
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const api = key => new OpenAI({baseURL:base.href, apiKey:key, maxRetries:0, timeout:80000}).beta.agents;
const owner = api(plan.key), other = api(plan.other);
const vaults = owner.vaults, credentials = vaults.credentials;
const denied = e => e instanceof OpenAI.APIError && e.status === 404;
let result;
if (plan.profile === 'primary') {
  const vault = await vaults.create({name:'Profile handoff',metadata:{project:'q4t'}});
  const auth = {type:'mcp_oauth',access_token:plan.secret,
    expires_at:plan.refresh ? '2000-01-01T00:00:00Z' : '2099-01-01T00:00:00Z',mcp_server_url:target.href};
  if (plan.refresh) auth.refresh = plan.refresh;
  const saved = await credentials.create(vault.id,{name:'records',auth});
  assert.ok(!JSON.stringify(saved).includes(plan.secret));
  if (plan.refresh) {
    assert.equal(saved.auth.refresh.token_endpoint,plan.refresh.token_endpoint);
    for (const secret of [plan.refresh.refresh_token,plan.refresh.token_endpoint_auth.client_secret])
      assert.ok(!JSON.stringify(saved).includes(secret));
  }
  await assert.rejects(other.vaults.credentials.retrieve(saved.id,{vault_id:vault.id}),denied);
  result = {vault:vault.id,credential:saved.id,created:true,foreign:404};
} else {
  assert.equal(plan.profile,'light');
  assert.ok(!('secret' in plan) && !('refresh' in plan));
  const {vault,credential} = plan.prior;
  const saved = await credentials.retrieve(credential,{vault_id:vault});
  assert.equal(saved.auth.type,'mcp_oauth');
  assert.equal(saved.auth.mcp_server_url,target.href);
  assert.equal(saved.vault_id,vault);
  if (plan.renewal) assert.ok(Date.parse(saved.auth.expires_at) < Date.now());
  const tool = {type:'mcp',credential_id:credential,server_label:'project',connection_origin:'service',
    required:true,allowed_tools:['read_project_record'],transport:{type:'http',server_url:target.href}};
  const request = {environment:{type:'none'},vault_ids:[vault],agent:{model:'qwen3.8-flash-next',
    reasoning:{effort:'none'},tools:[tool], instructions:'For this query, call read_project_record exactly once. Reply with its exact citation.'},
    input:'Use read_project_record to read docs/MODEL.md, start_line 1, line_count 3. Give its returned citation.'};
  const stream=(await owner.sessions.create({...request,stream:true})).withResultCollection();
  const calls=[];
  for await (const e of stream)
    if (e.type==='agent.session.turn.item.done' && e.item.type==='mcp_call') calls.push(e.item);
  const final = await stream.finalResult();
  assert.equal(final.turn.status,'completed');
  assert.equal(calls.length,1);
  assert.equal(calls[0].status,'completed');
  assert.equal(calls[0].output.structuredContent.source.citation,plan.citation);
  assert.ok(final.output_text.includes(plan.citation));
  if (plan.renewal) {
    const renewed = await credentials.retrieve(credential,{vault_id:vault});
    assert.ok(Date.parse(renewed.auth.expires_at) > Date.now());
    assert.deepEqual(renewed.auth.refresh,saved.auth.refresh);
  }
  assert.equal((await vaults.delete(vault)).deleted,true);
  await assert.rejects(async()=>await owner.sessions.create(request),denied);
  await assert.rejects(other.sessions.retrieve(final.session_id),denied);
  assert.deepEqual((await owner.sessions.retrieve(final.session_id)).vault_ids,[vault]);
  result = {vault,credential,session:final.session_id,call:calls[0].id,
    citation:plan.citation,archived_bind:404,foreign:404};
}
process.stdout.write(JSON.stringify({sdk:version,...result})+'\n');
