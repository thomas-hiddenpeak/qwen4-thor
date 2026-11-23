// Verify immutable copies of actual hosted workspace files through the SDK.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
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
const client = apiKey => new OpenAI({apiKey,baseURL:base.href,maxRetries:0,timeout:45000});
const {environments,sessions} = client(plan.key).beta.agents;
const other = client(plan.other).beta.agents.sessions.artifacts;
const {artifacts} = sessions;
const denied = code => err => err instanceof OpenAI.APIError && err.status === code;
const bytes = readFileSync(new URL('../../docs/evidence/serve-agent-workspaces-20261124/delivery.json',import.meta.url));
const {publication} = JSON.parse(bytes);
const path = '/workspace/outputs/release.json';
const file = (path,bytes) => ({type:'inline',path,data:bytes.toString('base64')});
const inputFiles = [file(path,bytes),file('/workspace/outputs/empty',Buffer.alloc(0)),
                    file('/workspace/input',Buffer.from('private input'))];
const tools = [{type:'function',name:'read_release_record',
  description:'Look up the current actual q4t publication receipt.',
  parameters:{type:'object',properties:{},required:[],additionalProperties:false}}];
const agent = {model:'qwen3.8-flash-next',reasoning:{effort:'none'},tools,
  instructions:'You are a q4t records assistant. When asked about the release, call read_release_record. After the tool returns, answer with the publication revision from its result.'};
const session = await sessions.create({agent,environment:{type:'openai_hosted',files:inputFiles},
  input:'Call read_release_record now with {} to get the actual publication revision. After the tool returns, reply with the revision number.'});
const id = session.id, env = session.environment.id;
const owner = {session_id:id};
async function settle() {
  const until = Date.now()+45000;
  do {
    const state = await sessions.retrieve(id);
    if (state.status !== 'in_progress') return state;
    await new Promise(resolve=>setTimeout(resolve,150));
  } while (Date.now()<until);
  throw Error('Model step exceeded deadline');
}
const waiting = await settle();
assert.equal(waiting.status,'requires_action');
const [action] = waiting.required_actions;
assert.equal(action.name,'read_release_record');
const pending = await artifacts.list(id);
assert.deepEqual(pending.data,[]);
assert.deepEqual(pending.body,{object:'list',data:[],first_id:null,last_id:null,has_more:false});
await sessions.events.create(id,{events:[{type:'agent.session.input.tool_result',
  call_id:action.call_id,turn_id:action.turn_id,success:true,
  output:JSON.stringify({publication_revision:publication.publication_revision})}]});
assert.equal((await settle()).status,'idle');
const messages = (await sessions.turns.items.list(action.turn_id,owner)).data;
const answer = messages.filter(i=>i.role==='assistant').flatMap(i=>i.content).map(p=>p.text).join('').trim();
assert.ok(answer.includes(String(publication.publication_revision)),answer);
const saved = [];
for await (const row of artifacts.list(id,{order:'asc',limit:1,environment_id:env})) saved.push(row);
assert.equal(saved.length,2);
assert.ok(saved.every(row=>row.session_id===id && row.turn_id===action.turn_id));
const old = saved.find(row=>row.path===path), empty = saved.find(row=>row.size_bytes===0);
assert.equal(old.size_bytes,bytes.length);
assert.deepEqual(await artifacts.retrieve(old.id,owner),old);
const read = async (artifact,from=owner) => {
  const content = await artifacts.content(artifact,from);
  assert.match(content.headers.get('content-type'),/application\/octet-stream/i);
  return Buffer.from(await content.arrayBuffer());
};
assert.deepEqual(await read(old.id),bytes);
assert.deepEqual(await read(empty.id),Buffer.alloc(0));
const secondSession = await sessions.create({agent,environment:{type:'openai_hosted'}});
await assert.rejects(artifacts.retrieve(old.id,{session_id:secondSession.id}),denied(404));
await assert.rejects(other.content(old.id,owner),denied(404));
await assert.rejects(other.delete(old.id,owner),denied(404));
assert.equal((await sessions.delete(secondSession.id)).deleted,true);
assert.deepEqual((await artifacts.list(id,{environment_id:'env_other'})).data,[]);
await assert.rejects(artifacts.list(id,{after:'artifact_missing'}),denied(400));
const nextBytes = Buffer.from('q4t\0'+answer);
await environments.files.create(env,file(path,nextBytes));
assert.deepEqual(await read(old.id),bytes);
await sessions.update(id,{agent:{tools:[]}});
await sessions.events.create(id,{events:[{type:'agent.session.input.message',input:[
  {role:'user',content:[{type:'input_text',text:'Return exactly the number 42.'}]}]}]});
assert.equal((await settle()).status,'idle');
const rows = (await artifacts.list(id)).data;
assert.equal(rows.length,4);
const next = rows.find(row=>row.path===path && row.turn_id!==old.turn_id);
assert.ok(next && next.id!==old.id);
assert.deepEqual(await read(next.id),nextBytes);
assert.deepEqual(await artifacts.delete(next.id,owner),{id:next.id,object:'agent.session.artifact.deleted',deleted:true});
await assert.rejects(read(next.id),denied(404));
assert.equal((await environments.files.list(env)).data.find(f=>f.path===path).size_bytes,nextBytes.length);
assert.deepEqual(await read(old.id),bytes);
assert.equal((await sessions.delete(id)).deleted,true);
await assert.rejects(artifacts.content(old.id,owner),denied(404));
process.stdout.write(JSON.stringify({sdk:version,input_bytes:bytes.length,
  input_sha256:createHash('sha256').update(bytes).digest('hex'),pending:pending.data.length,
  first_turn:saved.length,second_turn:rows.length,release_answer:answer,
  binary_and_empty:true,older_copy_unchanged:true,cross_session:404,cross_tenant:404,
  after_delete:404,source_survived:true})+'\n');
