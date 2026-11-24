// Attach to two real, file-backed prewarms while their bytes are being installed.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {setTimeout as delay} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const part of process.stdin) input += part;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
assert.equal(version, '7.47.0');
const connect = apiKey => new OpenAI({apiKey, baseURL:p.base, maxRetries:0, timeout:45000});
const owner = connect(p.key), peer = connect(p.other);
const {environments, sessions} = owner.beta.agents;
const model = p.model;
const agent = {model, reasoning:{effort:'none'}};
const release = readFileSync(new URL('../../docs/evidence/serve-webhooks-20261124/environment-delivery.json', import.meta.url));
const goals = readFileSync(p.projectGoals);
assert.equal(JSON.parse(release).publication.publication_revision,474);
assert.ok(goals.length > 256*1024);
const upload = (bytes,name) => owner.files.create({file:new File([bytes],name,{type:'application/json'}),purpose:'user_data'});
const published = await upload(release,'release.json');
const large = await upload(goals,'serve-goals.json');
const wire = [];
async function prepare(first, second, key) {
  const request = {environment:{type:'openai_hosted', files:[
    {type:'file_id',file_id:first,path:'/workspace/records/goals.json'},
    {type:'file_id',file_id:second,path:'/workspace/outputs/published.json'}]},
    'Idempotency-Key':key};
  const queued = await environments.create(request);
  assert.equal(queued.status,'pending');
  assert.deepEqual(queued.files,[]);
  const binding = {type:'openai_hosted',environment_id:queued.id};
  const stream = await sessions.create({agent,environment:binding,stream:true});
  const events = [];
  for await (const event of stream) {
    events.push(event);
    if (event.type === 'agent.session.environment.pending' && key === 'copy') {
      assert.equal((await owner.files.delete(published.id)).deleted,true);
      assert.equal((await owner.files.delete(large.id)).deleted,true);
    }
  }
  const changes = events.filter(e=>e.type.startsWith('agent.session.environment.'));
  wire.push(...changes);
  const sid = events.find(e=>e.type === 'agent.session.created').session.id;
  assert.ok(changes.every(e=>e.session_id===sid && e.environment.id===queued.id && e.turn_id===null));
  assert.deepEqual(await environments.create(request),await environments.retrieve(queued.id));
  return {id:queued.id,sid,changes:changes.map(e=>e.environment.status)};
}
const good = await prepare(large.id,published.id,'copy');
assert.deepEqual(good.changes,['pending','ready','connected']);
const installed = (await environments.retrieve(good.id)).files;
assert.deepEqual(installed.map(f=>f.size_bytes),[goals.length,release.length]);
await sessions.events.create(good.sid,{events:[{type:'agent.session.input.message',
  input:[{role:'user',content:[{type:'input_text',text:'Return only the integer 17 plus 25.'}]}]}]});
const deadline = Date.now()+40000;
while ((await sessions.retrieve(good.sid)).status==='in_progress') {
  assert.ok(Date.now()<deadline,'turn timeout');
  await delay(200);
}
const [turn] = (await sessions.turns.list(good.sid)).data;
assert.equal(turn.status,'completed');
const items = (await sessions.turns.items.list(turn.id,{session_id:good.sid})).data;
const answer = items.filter(i=>i.role==='assistant').flatMap(i=>i.content).map(c=>c.text).join('').trim();
assert.equal(answer,'42');
const [artifact] = (await sessions.artifacts.list(good.sid)).data;
assert.equal(artifact.path,'/workspace/outputs/published.json');
const response = await sessions.artifacts.content(artifact.id,{session_id:good.sid});
assert.deepEqual(Buffer.from(await response.arrayBuffer()),release);
const second = await upload(goals,'second-goals.json');
const bad = await prepare(second.id,p.handoff.damaged,'checksum');
assert.deepEqual(bad.changes,['pending','failed']);
const failed = (await sessions.retrieve(bad.sid));
assert.equal(failed.status,'failed');
assert.equal(wire.at(-1).environment.error.code,'workspace_source_invalid');
await assert.rejects(peer.beta.agents.environments.retrieve(good.id),err=>err.status===404);
await assert.rejects(peer.beta.agents.sessions.retrieve(bad.sid),err=>err.status===404);
const chat = await peer.chat.completions.create({model,temperature:0,max_tokens:16,
  messages:[{role:'user',content:'Return only the integer 6 times 7.'}],chat_template_kwargs:{enable_thinking:false}});
const independent = chat.choices[0].message.content.trim();
assert.equal(independent,'42');
process.stdout.write(JSON.stringify({sdk:version,peer:404,independent_chat:independent,answer,
  success:good.changes,failure:bad.changes,bytes:release.length,large_source_bytes:goals.length,
  release_sha256:createHash('sha256').update(release).digest('hex'),wire}));
