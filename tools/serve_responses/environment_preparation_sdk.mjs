// Verify an actual failed prewarm and an independent healthy tenant over TLS.
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
    assert.equal(req.url,'/preparation');
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
  const sub = await owner.webhooks.create({name:'Workspace preparation',
    url:'https://127.0.0.1:'+p.subscriberPort+'/preparation',
    event_types:['agent.environment.failed']});
  secret = sub.signing_secret;
  const path = '/workspace/release';
  const request = {environment:{type:'openai_hosted',files:[
    {type:'file_id',file_id:p.handoff.source,path}]},
    'Idempotency-Key':'business-preparation-failure'};
  const other = peer.beta.agents.environments;
  await assert.rejects(other.create(request), error => error.status === 404);
  const start = Math.floor(Date.now()/1000);
  const {data:failed,response} = await environments.create(request).withResponse();
  assert.equal(response.status,201);
  assert.deepEqual(failed,{id:failed.id,object:'agent.environment',
    type:'openai_hosted',status:'failed',files:[],plugins:[],skills:[]});
  assert.deepEqual(await environments.create(request),failed);
  assert.deepEqual(await environments.retrieve(failed.id),failed);
  const binding = {type:'openai_hosted',environment_id:failed.id};
  await assert.rejects(sessions.create({agent:{model:p.model},environment:binding}),
    error => error.status === 409);
  await assert.rejects(other.retrieve(failed.id), error => error.status === 404);
  const ready = await environments.create({environment:{type:'openai_hosted'}});
  assert.equal(ready.status,'ready');
  const list = (await environments.list({type:'openai_hosted'})).data;
  assert.deepEqual(list.map(item => item.id),[ready.id]);
  const deadline = Date.now()+12000;
  while (!observed.length && Date.now()<deadline) {
    if (failure) throw failure;
    await delay(100);
  }
  if (failure) throw failure;
  assert.equal(observed.length,1);
  const [event] = observed;
  assert.equal(event.type,'agent.environment.failed');
  assert.deepEqual(event.data,{id:failed.id});
  assert.ok(event.created_at >= start && event.created_at <= Math.floor(Date.now()/1000));
  const chat = await peer.chat.completions.create({model:p.model,temperature:0,max_tokens:16,
    messages:[{role:'user',content:'Return only the integer 6 times 7.'}],
    chat_template_kwargs:{enable_thinking:false}});
  const independent = chat.choices[0].message.content.trim();
  assert.equal(independent,'42');
  assert.equal((await owner.webhooks.delete(sub.id)).deleted,true);
  process.stdout.write(JSON.stringify({sdk:version,peer:404,independent_chat:independent,
    denied_attachment:409, replayed:true, healthy_environment:ready.status,
    observed:observed.map(e=>e.type), wire:{event,failed,ready}}));
} finally {
  receiver.closeAllConnections();
  receiver.close();
}
