// Exercise the caller-owned template and Files APIs through the native TLS entry.
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { loadSdk } from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.equal(base.protocol, 'https:');
assert.equal(base.hostname, '127.0.0.1');
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const client = key => new OpenAI({apiKey:key, baseURL:base.href, maxRetries:0, timeout:30000});
const owner = client(plan.key), other = client(plan.other);
const templates = owner.beta.agents.environments.templates;
const peer = other.beta.agents.environments.templates;
const fails = status => error => error instanceof OpenAI.APIError && error.status === status;
const text = 'Project test fixture for the native environment template.';
const source = await owner.files.create({
  file:new File([text], 'project-record.txt', {type:'text/plain'}), purpose:'user_data',
});
const secret = randomBytes(24).toString('hex');
const inline = Buffer.from(text).toString('base64');
const file = {type:'file_id',path:'/workspace/project-record.txt',file_id:source.id};
const {data:result, response:created} = await templates.create({
  name:'Project documents',env:{PROJECT_TOKEN:secret},
  setup_commands:[{command:'echo '+secret}],
  files:[file,{type:'inline',path:'/workspace/inline.txt',data:inline}],
  network:{access:'restricted',allowed_domains:['example.org']},
}).withResponse();
assert.equal(created.status, 201);
assert.deepEqual(result.files, [file,{type:'inline',path:'/workspace/inline.txt',
                                     size_bytes:Buffer.byteLength(text)}]);
const safe = row => {
  const raw = JSON.stringify(row);
  for (const hidden of [secret,inline,'setup_commands','PROJECT_TOKEN']) assert.ok(!raw.includes(hidden));
};
safe(result);
const defaults = await templates.create();
assert.equal(defaults.network.access,'disabled');
const ids = [];
for await (const row of templates.list({limit:1, order:'asc'})) { safe(row); ids.push(row.id); }
assert.deepEqual(ids.sort(),[defaults.id,result.id].sort());
assert.deepEqual((await peer.list()).data,[]);
await assert.rejects(peer.retrieve(result.id),fails(404));
await assert.rejects(peer.create({files:[file]}),fails(404));
const {data:patch,response:updated} = await templates.update(
  result.id,{desktop:{enabled:true},name:'Patched project'}).withResponse();
assert.equal(updated.status, 200);
assert.equal(patch.desktop.enabled,true);
assert.deepEqual(patch.files,result.files);
safe(await templates.retrieve(result.id));
await assert.rejects(templates.update(result.id,{files:[{type:'inline',path:'/outside',data:inline}]}),fails(400));
for (const network of [
  {access:'restricted',allowed_domains:['example.org'],blocked_domains:['private.example']},
  {access:'enabled',blocked_domains:['example.org']},
  {access:'restricted',blocked_domains:['*.example.org']},
]) await assert.rejects(templates.update(result.id,{network}), fails(400));
const bounded = [
  {name:'界'.repeat(257)}, {name:''}, {env:{'':'test'}},
  {setup_commands:Array.from({length:17}, () => ({command:''}))},
  {network:{access:'restricted',blocked_domains:Array.from({length:101}, (_,i) => 'd'+i+'.example')}},
  {files:[{type:'inline',path:'/workspace/a',data:''},
          {type:'inline',path:'/workspace/a/b',data:''}]},
];
for (const request of bounded)
  await assert.rejects(templates.update(result.id,request),fails(400));
assert.deepEqual(await templates.retrieve(result.id),patch);
const blocked = await templates.update(result.id,{network:{
  access:'restricted',blocked_domains:['private.example']}});
assert.deepEqual(blocked.network,{access:'restricted',allowed_domains:[]});
safe(blocked);
const cleared = await templates.update(result.id,{files:null,env:null,setup_commands:null,network:null});
assert.equal(cleared.network.access,'disabled');
assert.deepEqual(cleared.files,[]);
await assert.rejects(peer.delete(result.id),fails(404));
for (const id of ids) assert.equal((await templates.delete(id)).deleted,true);
await assert.rejects(templates.retrieve(result.id),fails(404));
assert.equal((await owner.files.delete(source.id)).deleted,true);
// Check the actual HTTP status even where generated SDK helpers accept any 2xx.
const model = 'qwen3.8-flash-next';
const agents = owner.beta.agents, vaults = agents.vaults;
const registration = await agents.create({model,reasoning:{effort:'none'}}).withResponse();
assert.equal(registration.response.status,201);
const vault = await vaults.create({name:'HTTP status validation'}).withResponse();
assert.equal(vault.response.status,201);
const credential = await vaults.credentials.create(vault.data.id,{name:'HTTP status',
  auth:{type:'static_bearer',token:secret,mcp_server_url:'https://records.example.test/mcp'},
}).withResponse();
assert.equal(credential.response.status,201);
const sessionRequest = {agent_id:registration.data.id,environment:{type:'none'},
                        input:'Reply with only the number 42.'};
const session = await agents.sessions.create(sessionRequest).withResponse();
assert.equal(session.response.status,201);
const waitUntil = Date.now()+30000;
let settled = session.data;
while (settled.status === 'in_progress' && Date.now() < waitUntil) {
  await new Promise(resolve => setTimeout(resolve,250));
  settled = await agents.sessions.retrieve(session.data.id);
}
assert.equal(settled.status,'idle');
const streamed = await fetch(new URL('/v1/agents/sessions',base),{method:'POST',
  headers:{Authorization:'Bearer '+plan.key,'Content-Type':'application/json',
           'OpenAI-Beta':'agents=v1'},body:JSON.stringify({...sessionRequest,stream:true})});
assert.equal(streamed.status,201);
assert.match(streamed.headers.get('content-type'),/text\/event-stream/);
const entries = (await streamed.text()).split(/\r?\n/)
  .filter(line => line.startsWith('data: ')).map(line => JSON.parse(line.slice(6)));
assert.equal(entries[0]?.type,'agent.session.created');
const streamId = entries[0].session.id;
assert.match(streamId,/^agent_session_/);
assert.ok(entries.every(e => (e.session_id ?? e.session?.id) === streamId));
assert.equal(entries.at(-1).type,'agent.session.idle');
assert.ok(entries.some(e => e.type === 'agent.session.turn.completed'));
for (const id of [session.data.id,streamId]) await agents.sessions.delete(id);
await vaults.delete(vault.data.id);
await agents.delete(registration.data.id);
const chat = await owner.chat.completions.create({model:'qwen3.8-flash-next',
  messages:[{role:'user',content:'What is six times seven? Respond with only the number.'}],
  temperature:0,max_tokens:24,chat_template_kwargs:{enable_thinking:false}});
assert.equal(chat.choices[0].message.content.trim(),'42');
process.stdout.write(JSON.stringify({sdk:version,operations:5,templates:2,pages:2,
  inline_bytes:Buffer.byteLength(text),owned_file:true,foreign_file:404,private_fields_omitted:true,
  foreign_read:404,foreign_delete:404,deleted_get:404,invalid_update:400,
  agent_create:registration.response.status,vault_create:vault.response.status,
  credential_create:credential.response.status,session_create:session.response.status,
  session_stream_create:streamed.status,session_events:entries.length,
  create_status:created.status,update_status:updated.status,rejected_bounds_and_paths:bounded.length,
  rejected_network_policies:3,exact_blocked_domain:true,chat:'42'})+'\n');
