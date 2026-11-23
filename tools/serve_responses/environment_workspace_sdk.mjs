// Exercise the public workspace lifecycle using a real release record.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.ok(base.protocol === 'https:' && base.hostname === '127.0.0.1' &&
          base.pathname === '/v1' && !base.username && !base.password);
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const client = apiKey => new OpenAI({apiKey, baseURL:base.href, maxRetries:0, timeout:45000});
const owner = client(plan.key), peer = client(plan.other);
const {environments, sessions} = owner.beta.agents;
const denied = status => error => error instanceof OpenAI.APIError && error.status === status;
const release = readFileSync(new URL('../../docs/evidence/serve-agent-environments-20261124/delivery.json', import.meta.url));
const file = await owner.files.create({file:new File([release], 'release.json', {type:'text/plain'}), purpose:'user_data'});
const path = '/workspace/records/release.json';
const template = await environments.templates.create({name:'Project files',
  files:[{type:'file_id',file_id:file.id,path}],env:{PROJECT:'q4t'}});
const config = {environment:{type:'openai_hosted',environment_template_id:template.id},
  'Idempotency-Key':'workspace 01, release'};
const {data:warm,response} = await environments.create(config).withResponse();
assert.equal(response.status,201);
assert.equal(warm.status,'ready');
const env = warm.id;
assert.deepEqual(warm.files.map(({file_id,path:p,size_bytes})=>[file_id,p,size_bytes]), [[file.id,path,release.length]]);
assert.deepEqual((await environments.list({type:'openai_hosted',limit:1})).data,[warm]);
assert.deepEqual(await environments.create(config),warm);
await assert.rejects(environments.create({...config,environment:{type:'openai_hosted'}}),denied(409));
await assert.rejects(peer.beta.agents.environments.retrieve(env),denied(404));
await assert.rejects(environments.files.list(env),denied(409));
await owner.files.delete(file.id);
await environments.templates.delete(template.id);

const agent = {model:'qwen3.8-flash-next',reasoning:{effort:'none'}};
const binding = {type:'openai_hosted',environment_id:env};
await assert.rejects(peer.beta.agents.sessions.create({agent,environment:binding}),denied(404));
const {data:stream,response:streamResponse} =
  await sessions.create({agent,environment:binding,stream:true}).withResponse();
assert.equal(streamResponse.status,201);
const events = [];
for await (const event of stream) events.push(event);
assert.deepEqual(events.map(e=>e.type),['agent.session.created','agent.session.idle']);
const session = events[0].session;
assert.deepEqual(session.environment.files,warm.files);
assert.equal(session.environment.id,env);
assert.equal((await environments.create(config)).status,'connected');
await assert.rejects(sessions.create({agent,environment:binding}),denied(409));
assert.deepEqual((await sessions.turns.list(session.id)).data,[]);
const name = '/workspace/records/notes.txt';
const write = data => environments.files.create(env,{type:'inline',data,path:name});
const added = await write(Buffer.from('release').toString('base64')).withResponse();
assert.equal(added.response.status,201);
assert.equal(added.data.size_bytes,7);
assert.equal((await write('')).size_bytes,0);
await assert.rejects(environments.files.create(env,{type:'inline',data:'',path:'/workspace/records'}),denied(400));
const page = await environments.files.list(env,{path:'/workspace/records',order:'asc',limit:1});
assert.deepEqual(page.data.map(f=>f.path),[name]);
assert.deepEqual((await page.getNextPage()).data.map(f=>f.path),[path]);
await assert.rejects(environments.files.list(env,{order:'desc',limit:1,page:page.next}),denied(400));
assert.equal((await sessions.retrieve(session.id)).environment.files.length,2);
await sessions.events.create(session.id,{events:[{type:'agent.session.input.message',
  input:[{role:'user',content:[{type:'input_text',text:'Compute 17+25. Respond with just the number.'}]}]}]});
let status;
const until = Date.now()+30000;
do {
  status = (await sessions.retrieve(session.id)).status;
  if (status === 'idle') break;
  assert.equal(status,'in_progress');
  await new Promise(resolve=>setTimeout(resolve,200));
} while (Date.now()<until);
assert.equal(status,'idle');
const items = (await sessions.items.list(session.id)).data;
const answer = items.filter(i=>i.role==='assistant').flatMap(i=>i.content).map(p=>p.text).join('').trim();
assert.equal(answer,'42');
assert.equal((await sessions.delete(session.id)).deleted,true);
assert.equal((await environments.retrieve(env)).status,'disconnected');
await assert.rejects(environments.files.list(env),denied(409));
process.stdout.write(JSON.stringify({sdk:version,source_bytes:release.length,
  states:['ready','connected','disconnected'],copied_before_source_deletion:true,
  create:201,stream_create:201,idle_events:events.map(e=>e.type),file_create:201,
  replaced_bytes:0,pages:2,tenant:404,key_conflict:409,second_attach:409,
  turn_answer:answer,deleted:true})+'\n');
