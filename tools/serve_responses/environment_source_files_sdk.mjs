// Copy a Files source at the documented decoded-byte boundary and publish it.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(input.length <= 16384);
}
const plan = JSON.parse(input), base = new URL(plan.base);
assert.ok(base.protocol==='https:' && base.hostname==='127.0.0.1' &&
          base.pathname==='/v1' && !base.password && !base.username);
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version,'7.47.0');
const client = key=>new OpenAI({apiKey:key,baseURL:base.href,maxRetries:0,timeout:110000});
const owner = client(plan.key), peer = client(plan.other);
const {environments,sessions} = owner.beta.agents;
const limit = 50*1024*1024;
const release = readFileSync(new URL('../../docs/evidence/serve-agent-workspaces-20261124/methods.json',import.meta.url));
const bytes = Buffer.alloc(limit+1);
bytes.fill(release);
bytes[0]=0; bytes[limit-1]=255;
const upload = data=>owner.files.create({file:new File([data],'release-matrix.bin'),
                                        purpose:'user_data'});
const file = file_id=>({type:'file_id',file_id,path:'/workspace/outputs/matrix.bin'});
const hosted = files=>({type:'openai_hosted',files});
const agent = {model:'qwen3.8-flash-next',reasoning:{effort:'none'}};
const denied = (status,param)=>error=>error instanceof OpenAI.APIError &&
  error.status===status && (!param || error.error.param===param);
const over = await upload(bytes);
assert.equal(over.bytes,limit+1);
await assert.rejects(environments.create({environment:hosted([file(over.id)])}),
                     denied(400,'environment.files[0].file_id'));
const idle = await sessions.create({agent,environment:hosted([])});
await assert.rejects(environments.files.create(idle.environment.id,file(over.id)),denied(400,'file_id'));
assert.deepEqual((await environments.files.list(idle.environment.id)).data,[]);
assert.equal((await sessions.delete(idle.id)).deleted,true);
await owner.files.delete(over.id);
const original = bytes.subarray(0,limit);
const source = await upload(original);
const files = [file(source.id),{type:'inline',path:'/workspace/small',data:'eA=='}];
await assert.rejects(peer.beta.agents.environments.create({environment:hosted(files)}),denied(404));
const warm = await environments.create({environment:hosted(files)});
assert.equal(warm.files[0].size_bytes,limit);
assert.equal(warm.files[1].size_bytes,1);
assert.equal((await owner.files.delete(source.id)).deleted,true);
await assert.rejects(owner.files.retrieve(source.id),denied(404));
const session = await sessions.create({agent,environment:{type:'openai_hosted',environment_id:warm.id},
  input:'Compute 17+25. Respond with just the number.'});
const until = Date.now()+105000;
let state;
do {
  state = await sessions.retrieve(session.id);
  if (state.status!=='in_progress') break;
  await new Promise(resolve=>setTimeout(resolve,300));
} while(Date.now()<until);
assert.equal(state.status,'idle');
const turn = (await sessions.turns.list(session.id)).data[0];
assert.equal(turn.status,'completed');
const items = (await sessions.turns.items.list(turn.id,{session_id:session.id})).data;
const answer=items.filter(i=>i.role==='assistant').flatMap(i=>i.content).map(c=>c.text).join('').trim();
assert.equal(answer,'42');
const [artifact] = (await sessions.artifacts.list(session.id)).data;
assert.equal(artifact.size_bytes,limit);
assert.equal(artifact.path,files[0].path);
const response = await sessions.artifacts.content(artifact.id,{session_id:session.id});
const hash = createHash('sha256');
let count=0;
for await (const chunk of response.body) { count+=chunk.length; hash.update(chunk); }
const digest = hash.digest('hex');
assert.equal(count,limit);
assert.equal(digest,createHash('sha256').update(original).digest('hex'));
assert.equal((await sessions.delete(session.id)).deleted,true);
await assert.rejects(sessions.artifacts.retrieve(artifact.id,{session_id:session.id}),denied(404));
process.stdout.write(JSON.stringify({sdk:version,file_bytes:limit,workspace_source_bytes:limit+1,
  over_source_bytes:over.bytes,prewarm_rejected:400,connected_rejected:400,foreign_source:404,
  deleted_source:404,download_bytes:count,download_sha256:digest,answer,deleted_session:404})+'\n');
