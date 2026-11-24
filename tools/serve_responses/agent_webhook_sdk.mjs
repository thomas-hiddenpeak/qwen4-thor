// Verify signed notifications against actual hosted environment and session IDs.
import assert from 'node:assert/strict';
import {once} from 'node:events';
import {readFileSync} from 'node:fs';
import https from 'node:https';
import {setTimeout as delay} from 'node:timers/promises';
import {loadSdk} from '../serve_protocol/sdk.mjs';
import {releaseAgent, turnTypes, verifyTurns} from './agent_webhook_turns.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
assert.equal(version, '7.47.0');
const client = apiKey => new OpenAI({apiKey, baseURL:p.base, timeout:35000, maxRetries:0});
const owner = client(p.key), peer = client(p.other);
const {environments, sessions} = owner.beta.agents;
const hidden = error => error instanceof OpenAI.APIError && error.status === 404;
const types = ['agent.environment.ready', 'agent.session.created'];
if (p.turns) types.push(...turnTypes);
const observed = [];
let secret;
let failure;
const receiver = https.createServer({
  key:readFileSync(p.tlsKey), cert:readFileSync(p.tlsCert),
}, async (req, res) => {
  try {
    assert.equal(req.method, 'POST');
    assert.equal(req.url, '/agents');
    let raw = '';
    for await (const part of req) raw += part;
    const headers = new Headers(req.headers);
    const event = await owner.webhooks.unwrap(raw, headers, secret);
    assert.equal(event.id, headers.get('webhook-id'));
    assert.equal(event.object, 'event');
    assert.ok(types.includes(event.type));
    observed.push(event);
    res.writeHead(202).end();
  } catch (error) {
    failure = error;
    res.writeHead(500).end();
  }
});
try {
  receiver.listen(p.subscriberPort, '127.0.0.1');
  await once(receiver, 'listening');
  const available = (await owner.webhooks.eventTypes.list()).data;
  assert.ok(types.every(type => available.includes(type)));
  const sub = await owner.webhooks.create({name:'Hosted agent notices',
    url:'https://127.0.0.1:' + p.subscriberPort + '/agents', event_types:types});
  secret = sub.signing_secret;
  const config = {environment:{type:'openai_hosted'}, 'Idempotency-Key':'project release prewarm'};
  const env = await environments.create(config);
  assert.equal(env.status, 'ready');
  assert.deepEqual(await environments.create(config), env);
  await assert.rejects(peer.beta.agents.environments.retrieve(env.id), hidden);
  const session = await sessions.create({
    agent:{model:p.model,reasoning:{effort:'none'},...(p.turns ? releaseAgent : {})},
    environment:{type:'openai_hosted',environment_id:env.id},
  });
  assert.equal(session.environment.id, env.id);
  assert.equal((await environments.retrieve(env.id)).status, 'connected');
  assert.equal((await sessions.retrieve(session.id)).status, 'idle');
  await assert.rejects(peer.beta.agents.sessions.retrieve(session.id), hidden);
  await assert.rejects(peer.webhooks.retrieve(sub.id), hidden);
  const chat = await peer.chat.completions.create({model:p.model,temperature:0,max_tokens:16,
    messages:[{role:'user',content:'Return only the integer 6 times 7.'}],
    chat_template_kwargs:{enable_thinking:false}});
  const answer = chat.choices[0].message.content.trim();
  assert.equal(answer, '42');
  const deadline = Date.now() + 20000;
  while (observed.length < 2 && !failure && Date.now() < deadline) await delay(100);
  if (failure) throw failure;
  const business = p.turns ? await verifyTurns(sessions, session, observed, () => failure, p.cancelTurn) : undefined;
  assert.equal(observed.length, p.cancelTurn ? 6 : p.turns ? 7 : 2);
  const byType = Object.fromEntries(observed.map(event => [event.type, event]));
  assert.deepEqual(byType[types[0]].data, {id:env.id});
  assert.deepEqual(byType[types[1]].data, {
    id:session.id, environment_type:'openai_hosted', environment_id:env.id,
  });
  assert.notEqual(byType[types[0]].id, byType[types[1]].id);
  assert.equal((await sessions.delete(session.id)).deleted, true);
  assert.equal((await environments.retrieve(env.id)).status, 'disconnected');
  assert.equal((await owner.webhooks.delete(sub.id)).deleted, true);
  process.stdout.write(JSON.stringify({sdk:version, independent_chat:answer, peer:404,
    environment:env.id, session:session.id, business, observed:observed.map(e=>e.type),
    wire:p.turns ? observed : byType}));
} finally {
  receiver.closeAllConnections();
  receiver.close();
}
