// Two independent histories share one environment and the same release file.
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
const connect = apiKey => new OpenAI({apiKey,baseURL:p.base,timeout:35000,maxRetries:0});
const owner=connect(p.key), peer=connect(p.other);
const {environments,sessions}=owner.beta.agents;
const agent={model:p.model,reasoning:{effort:'none'}};
const warm = await environments.create({environment:{type:'openai_hosted'}});
const binding = {type:'openai_hosted',environment_id:warm.id};
const attach = () => sessions.create({agent,environment:binding});
const one = await attach(), two = await attach();
assert.notEqual(one.id,two.id);
assert.equal(one.environment.id,two.environment.id);
await assert.rejects(peer.beta.agents.sessions.create({agent,environment:binding}),e=>e.status===404);
const bytes = readFileSync(new URL('../../docs/evidence/serve-webhooks-20261124/delivery.json',import.meta.url));
const path = '/workspace/outputs/release.json';
const file = await environments.files.create(warm.id,{type:'inline',path,data:bytes.toString('base64')});
assert.equal(file.size_bytes,bytes.length);
for (const id of [one.id,two.id]) {
  const session = await sessions.retrieve(id);
  assert.equal(session.environment.files.find(f=>f.path===path).size_bytes, bytes.length);
}
assert.equal((await sessions.delete(one.id)).deleted,true);
assert.equal((await environments.retrieve(warm.id)).status,'connected');
await sessions.events.create(two.id,{events:[{type:'agent.session.input.message',
  input:[{role:'user',content:[{type:'input_text',text:'Return only the integer 17 plus 25.'}]}]}]});
const deadline=Date.now()+35000;
while ((await sessions.retrieve(two.id)).status==='in_progress') {
  assert.ok(Date.now()<deadline,'Timed out waiting for the shared workspace turn');
  await delay(250);
}
const turns = (await sessions.turns.list(two.id)).data;
assert.equal(turns.length,1);
assert.equal(turns[0].status,'completed');
const items = (await sessions.turns.items.list(turns[0].id,{session_id:two.id})).data;
const answer = items.filter(i=>i.role==='assistant').flatMap(i=>i.content).map(p=>p.text).join('').trim();
assert.equal(answer,'42');
const artifacts = (await sessions.artifacts.list(two.id)).data;
assert.equal(artifacts.length,1);
const artifact=artifacts[0];
assert.equal(artifact.path,path);
assert.equal(artifact.environment_id,warm.id);
const content=await sessions.artifacts.content(artifact.id,{session_id:two.id});
assert.deepEqual(Buffer.from(await content.arrayBuffer()),bytes);
await assert.rejects(peer.beta.agents.sessions.artifacts.content(artifact.id,{session_id:two.id}),e=>e.status===404);
assert.equal((await sessions.delete(two.id)).deleted,true);
assert.equal((await environments.retrieve(warm.id)).status,'disconnected');
const three=await attach();
assert.equal((await sessions.turns.list(three.id)).data.length,0);
assert.equal(three.environment.files.find(f=>f.path===path).size_bytes,bytes.length);
assert.equal((await sessions.delete(three.id)).deleted,true);
const chat=await peer.chat.completions.create({model:p.model,temperature:0,max_tokens:16,
  messages:[{role:'user',content:'Return only the integer 6 times 7.'}],chat_template_kwargs:{enable_thinking:false}});
const independent=chat.choices[0].message.content.trim();
assert.equal(independent,'42');
process.stdout.write(JSON.stringify({sdk:version,peer:404,independent_chat:independent,
  session_answer:answer,environment:warm.id,sessions:[one.id,two.id,three.id],
  bytes:bytes.length,sha256:createHash('sha256').update(bytes).digest('hex'),
  revision:JSON.parse(bytes).publication.publication_revision,retained_on_detach:true,
  wire:{warm,file,artifact}}));
