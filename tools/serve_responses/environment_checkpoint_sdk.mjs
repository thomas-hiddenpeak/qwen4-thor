// Resume the original session from its private idle checkpoint through TLS.
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
import {once} from 'node:events';
import {readFileSync} from 'node:fs';
import https from 'node:https';
import {setTimeout as delay} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const part of process.stdin) input += part;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
assert.equal(version, '7.47.0');
const connect = apiKey => new OpenAI({apiKey, baseURL:p.base, maxRetries:0, timeout:25000});
const owner = connect(p.key), peer = connect(p.other);
const {environments, sessions} = owner.beta.agents;
const observed = [];
let secret, failure;
const receiver = https.createServer({
  key:readFileSync(p.tlsKey), cert:readFileSync(p.tlsCert),
}, async (req, res) => {
  try {
    assert.equal(req.method,'POST');
    assert.equal(req.url,'/checkpoint');
    let wire = '';
    for await (const part of req) wire += part;
    const headers = new Headers(req.headers);
    const event = await owner.webhooks.unwrap(wire, headers, secret);
    assert.equal(event.id, headers.get('webhook-id'));
    observed.push(event);
    res.writeHead(202).end();
  } catch (error) {
    failure = error;
    res.writeHead(500).end();
  }
});
try {
  receiver.listen(p.subscriberPort, '127.0.0.1');
  await once(receiver,'listening');
  const sub = await owner.webhooks.create({name:'Hosted checkpoint',
    url:'https://127.0.0.1:'+p.subscriberPort+'/checkpoint',
    event_types:['agent.environment.suspended']});
  secret = sub.signing_secret;
  const {environment,session,idle_deadline} = p.handoff;
  assert.ok(Math.floor(Date.now()/1000)<idle_deadline,'missed subscription window');
  while (observed.length === 0 && Date.now() < (idle_deadline+12)*1000) {
    if (failure) throw failure;
    await delay(100);
  }
  if (failure) throw failure;
  assert.equal(observed.length,1);
  const [event] = observed;
  assert.equal(event.type,'agent.environment.suspended');
  assert.deepEqual(event.data,{id:environment});
  assert.equal(event.created_at,idle_deadline);
  const suspended = await environments.retrieve(environment);
  assert.equal(suspended.status,'suspended');
  await assert.rejects(peer.beta.agents.environments.retrieve(environment),e=>e.status===404);
  await sessions.events.create(session,{events:[{type:'agent.session.input.message',
    input:[{role:'user',content:[{type:'input_text',text:'Return only the integer 17 plus 25.'}]}]}]});
  const finish = Date.now()+35000;
  while ((await sessions.retrieve(session)).status==='in_progress') {
    assert.ok(Date.now()<finish,'turn timeout');
    await delay(250);
  }
  const resumed = await environments.retrieve(environment);
  assert.equal(resumed.status,'connected');
  const turns = (await sessions.turns.list(session)).data;
  assert.equal(turns.length,1);
  assert.equal(turns[0].status,'completed');
  const items = (await sessions.turns.items.list(turns[0].id,{session_id:session})).data;
  const answer = items.filter(i=>i.role==='assistant').flatMap(i=>i.content).map(p=>p.text).join('').trim();
  assert.equal(answer,'42');
  const artifacts = (await sessions.artifacts.list(session)).data;
  assert.equal(artifacts.length,1);
  const [artifact] = artifacts;
  assert.equal(artifact.path,'/workspace/outputs/release.txt');
  assert.equal(artifact.environment_id,environment);
  const res = await sessions.artifacts.content(artifact.id,{session_id:session});
  const bytes = Buffer.from(await res.arrayBuffer());
  assert.deepEqual(bytes,Buffer.from('472\n'));
  await assert.rejects(peer.beta.agents.sessions.artifacts.content(artifact.id,{session_id:session}),e=>e.status===404);
  const chat = await peer.chat.completions.create({model:p.model,temperature:0,max_tokens:16,
    messages:[{role:'user',content:'Return only the integer 6 times 7.'}],
    chat_template_kwargs:{enable_thinking:false}});
  const independent = chat.choices[0].message.content.trim();
  assert.equal(independent,'42');
  assert.equal((await owner.webhooks.delete(sub.id)).deleted,true);
  process.stdout.write(JSON.stringify({sdk:version,peer:404,independent_chat:independent,
    answer,artifact_bytes:bytes.length,artifact_sha256:createHash('sha256').update(bytes).digest('hex'),
    deadline:idle_deadline,occurred:event.created_at,observed:observed.map(e=>e.type),
    wire:{event,suspended,resumed,artifact}}));
} finally {
  receiver.closeAllConnections();
  receiver.close();
}
