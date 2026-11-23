// Carry one already-published artifact through replacement of the managed service.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {setTimeout as delay} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const url = new URL(plan.base);
assert.ok(['http:', 'https:'].includes(url.protocol) && url.hostname === '127.0.0.1' &&
          url.pathname === '/v1' && !url.username && !url.password);
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const client = key => new OpenAI({apiKey:key, baseURL:url.href, timeout:40000, maxRetries:0});
const owner = client(plan.key);
const sessions = owner.beta.agents.sessions;
const peer = client(plan.other).beta.agents.sessions;
const path = '/workspace/outputs/recovery.bin';
const receipt = readFileSync(new URL('../../docs/evidence/serve-agent-workspaces-20261124/delivery.json', import.meta.url));
const bytes = Buffer.alloc(384*1024, receipt);
bytes[0] = 0;
bytes[bytes.length-1] = 255;
const sha256 = createHash('sha256').update(bytes).digest('hex');
const missing = e => e instanceof OpenAI.APIError && e.status === 404;

async function verify(session, artifact) {
  const scope = {session_id:session};
  assert.deepEqual(await sessions.artifacts.retrieve(artifact.id,scope), artifact);
  const file = await sessions.artifacts.content(artifact.id,scope);
  assert.deepEqual(Buffer.from(await file.arrayBuffer()),bytes);
  await assert.rejects(peer.artifacts.retrieve(artifact.id,scope),missing);
}

async function before() {
  const source = await owner.files.create({file:new File([bytes],'recovery.bin'),purpose:'user_data'});
  const ready = await owner.beta.agents.environments.create({environment:{type:'openai_hosted',
    files:[{type:'file_id',file_id:source.id,path}]}});
  assert.equal((await owner.files.delete(source.id)).deleted,true);
  const session = await sessions.create({agent:{model:'qwen3.8-flash-next',reasoning:{effort:'none'}},
    environment:{type:'openai_hosted',environment_id:ready.id},input:'Compute 17+25. Reply only with the number.'});
  const until = Date.now()+45000;
  let state = session;
  while (state.status === 'in_progress' && Date.now() < until) {
    await delay(200);
    state = await sessions.retrieve(session.id);
  }
  assert.equal(state.status,'idle');
  const turns = (await sessions.turns.list(session.id)).data;
  assert.equal(turns.length,1);
  assert.equal(turns[0].status,'completed');
  const items = (await sessions.turns.items.list(turns[0].id,{session_id:session.id})).data;
  const answer = items.filter(i=>i.role==='assistant').flatMap(i=>i.content).map(c=>c.text).join('').trim();
  assert.equal(answer,'42');
  const artifacts = (await sessions.artifacts.list(session.id)).data;
  assert.equal(artifacts.length,1);
  const [artifact] = artifacts;
  assert.equal(artifact.path,path);
  assert.equal(artifact.size_bytes,bytes.length);
  assert.equal(artifact.turn_id,turns[0].id);
  await verify(session.id,artifact);
  const replaced = await owner.beta.agents.environments.files.create(ready.id,{type:'inline',path,data:''});
  assert.equal(replaced.size_bytes,0);
  return {session:session.id,environment:ready.id,source:source.id,artifact,answer,sha256,bytes:bytes.length};
}

async function after() {
  const {session,source,environment,artifact} = plan.prior;
  assert.equal(plan.prior.sha256,sha256);
  assert.equal(plan.prior.bytes,bytes.length);
  assert.equal((await sessions.retrieve(session)).status,'idle');
  const rows = (await owner.beta.agents.environments.files.list(environment)).data;
  assert.deepEqual(rows.map(f=>[f.path,f.size_bytes]),[[path,0]]);
  await assert.rejects(owner.files.retrieve(source),missing);
  assert.deepEqual((await sessions.artifacts.list(session)).data,[artifact]);
  await verify(session,artifact);
  assert.equal((await sessions.delete(session)).deleted,true);
  await assert.rejects(sessions.artifacts.retrieve(artifact.id,{session_id:session}),missing);
  return {session,artifact:artifact.id,sha256,bytes:bytes.length,replaced_bytes:0,foreign:404,deleted:404};
}

assert.ok(['before','after'].includes(plan.phase));
const result = plan.phase === 'before' ? await before() : await after();
process.stdout.write(JSON.stringify(result)+'\n');
