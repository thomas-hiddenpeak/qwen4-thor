// Observe expiry at the actual hosted prewarm deadline, with no read to trigger it.
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
    assert.equal(req.url,'/prewarm');
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
  const sub = await owner.webhooks.create({name:'Prewarm deadlines',
    url:'https://127.0.0.1:'+p.subscriberPort+'/prewarm',
    event_types:['agent.environment.ready','agent.environment.expired']});
  secret = sub.signing_secret;
  const config = {environment:{type:'openai_hosted'},'Idempotency-Key':'unused release workspace'};
  const before = Math.floor(Date.now()/1000);
  const unused = await environments.create(config);
  assert.equal(unused.status,'ready');
  const after = Math.floor(Date.now()/1000);
  const bound = await environments.create({environment:{type:'openai_hosted'}});
  assert.notEqual(bound.id,unused.id);
  const agent = {model:p.model,reasoning:{effort:'none'}};
  const attach = id => sessions.create({agent,environment:{type:'openai_hosted',environment_id:id}});
  const attached = await attach(bound.id);
  assert.equal(attached.environment.id,bound.id);
  const deadline = Date.now()+328000;
  while (!observed.some(e=>e.type==='agent.environment.expired') && Date.now()<deadline) {
    if (failure) throw failure;
    await delay(200);
  }
  if (failure) throw failure;
  assert.equal(observed.length,3);
  const ready = observed.filter(e=>e.type==='agent.environment.ready');
  assert.deepEqual(new Set(ready.map(e=>e.data.id)),new Set([unused.id,bound.id]));
  const [expired] = observed.filter(e=>e.type==='agent.environment.expired');
  assert.deepEqual(expired.data,{id:unused.id});
  assert.ok(expired.created_at >= before + 300 && expired.created_at <= after + 300);
  const arrival = Math.floor(Date.now()/1000);
  assert.ok(arrival >= expired.created_at);
  assert.equal((await environments.retrieve(unused.id)).status,'expired');
  assert.equal((await environments.create(config)).status,'expired');
  await assert.rejects(attach(unused.id),e=>e.status===409);
  assert.equal((await environments.retrieve(bound.id)).status,'connected');
  await assert.rejects(peer.beta.agents.environments.retrieve(unused.id),e=>e.status===404);
  const chat = await peer.chat.completions.create({model:p.model,temperature:0,max_tokens:16,
    messages:[{role:'user',content:'Return only the integer 6 times 7.'}],
    chat_template_kwargs:{enable_thinking:false}});
  const answer = chat.choices[0].message.content.trim();
  assert.equal(answer,'42');
  assert.equal((await sessions.delete(attached.id)).deleted,true);
  assert.equal((await owner.webhooks.delete(sub.id)).deleted,true);
  process.stdout.write(JSON.stringify({sdk:version,peer:404,independent_chat:answer,
    expired:unused.id,connected:bound.id,attachment:409,deadline_seconds:300,
    delivery_lag_seconds:arrival-expired.created_at,observed:observed.map(e=>e.type),wire:observed}));
} finally {
  receiver.closeAllConnections();
  receiver.close();
}
