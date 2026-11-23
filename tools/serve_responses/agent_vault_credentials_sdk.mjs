// Use fresh synthetic secrets; stdout contains only the public acceptance receipt.
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { loadSdk } from '../serve_protocol/sdk.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.ok(base.protocol === 'https:' && base.hostname === '127.0.0.1' &&
          base.pathname === '/v1' && !base.username && !base.password);
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const client = key => new OpenAI({baseURL:base.href, apiKey:key, maxRetries:0, timeout:35000});
const owner = client(plan.key), peer = client(plan.other);
const vaults = owner.beta.agents.vaults;
const api = vaults.credentials;
const status = code => err => err instanceof OpenAI.APIError && err.status === code;
const secrets = Array.from({length:8}, () => randomBytes(32).toString('hex'));
function publicOnly(value) {
  const wire = JSON.stringify(value);
  assert.ok(secrets.every(secret => !wire.includes(secret)), 'A synthetic secret appeared in a public resource');
  return value;
}
const v = await vaults.create({name:'project records'});
const vault = {vault_id:v.id};
const other = await vaults.create({name:'separate'});
const dest = 'https://records.example.test/mcp';
const bearer = publicOnly(await api.create(v.id, {name:'bearer', metadata:{kind:'http'},
  auth:{type:'static_bearer',token:secrets[0],mcp_server_url:dest}}));
assert.deepEqual(bearer.auth, {type:'static_bearer',mcp_server_url:dest});
const oauth = publicOnly(await api.create(v.id, {name:'oauth', metadata:{kind:'http'},
  auth:{type:'mcp_oauth',access_token:secrets[1],mcp_server_url:dest,expires_at:'2027-03-01T00:00:00Z',
    refresh:{client_id:'project-synthetic',refresh_token:secrets[2],token_endpoint:'https://records.example.test/token',
      token_endpoint_auth:{type:'client_secret_basic',client_secret:secrets[3]}}}}));
assert.deepEqual(oauth.auth.refresh.token_endpoint_auth,{type:'client_secret_basic'});
const env = publicOnly(await api.create(v.id, {name:'env',auth:{type:'environment_variable',
  secret_value:secrets[4],secret_name:'RECORD_KEY',networking:{type:'limited',allowed_hosts:['RECORDS.EXAMPLE.TEST']}}}));
assert.deepEqual(env.auth.networking.allowed_hosts, ['records.example.test']);
await assert.rejects(peer.beta.agents.vaults.credentials.retrieve(bearer.id,vault),status(404));
await assert.rejects(api.retrieve(bearer.id,{vault_id:other.id}),status(404));
const rotated = publicOnly(await api.update(oauth.id,{...vault,auth:{type:'mcp_oauth',access_token:secrets[5],
  refresh:{refresh_token:null,scope:'records.read',token_endpoint_auth:{type:'client_secret_basic',client_secret:secrets[6]}}}}));
assert.equal(rotated.auth.expires_at, null);
assert.equal(rotated.auth.refresh.scope,'records.read');
publicOnly(await api.update(bearer.id,{...vault,auth:{type:'static_bearer',token:secrets[7]}}));
await assert.rejects(api.update(bearer.id,{...vault,auth:{type:'environment_variable',secret_value:secrets[7]}}),status(400));
const seen=[];
for await (const row of api.list(v.id,{limit:1,order:'asc',metadata:{kind:'http'}})) {
  seen.push(publicOnly(row).id);
  assert.ok(seen.length <= 2);
}
assert.deepEqual(seen,[bearer.id,oauth.id]);
await api.delete(env.id,vault);
assert.deepEqual(publicOnly((await api.list(v.id,{status:'archived'})).data).map(x=>x.id),[env.id]);
await vaults.delete(v.id);
assert.equal((await api.list(v.id,{status:'active'})).data.length,0);
assert.equal(publicOnly((await api.list(v.id,{status:['archived']})).data).length,3);
await assert.rejects(api.update(oauth.id,{...vault,metadata:{a:'b'}}),status(409));
await vaults.delete(other.id);
const chat=await owner.chat.completions.create({model:'qwen3.8-flash-next',temperature:0,max_tokens:40,
  chat_template_kwargs:{enable_thinking:false},messages:[{role:'user',content:'Reply with only the number 42.'}]});
assert.equal(chat.choices[0].message.content.trim(),'42');
process.stdout.write(JSON.stringify({sdk:version,types:[bearer.auth.type,oauth.auth.type,env.auth.type],
  secret_projection_checks:true,scoped_read:404,wrong_vault:404,method_change:400,
  filtered_pages:seen.length,archived_after_cascade:3,archived_update:409,chat:'42'})+'\n');
