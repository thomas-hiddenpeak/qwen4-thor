import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import https from 'node:https';
import {once} from 'node:events';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const p = JSON.parse(input);
const {OpenAI, version} = loadSdk(p.sdk);
assert.equal(version, '7.47.0');
const options = {baseURL: p.base, maxRetries: 0, timeout: 20000};
const owner = new OpenAI({...options, apiKey: p.key});
const peer = new OpenAI({...options, apiKey: p.other});
const hooks = owner.webhooks;
const observed = [];
let status = 202;
let secrets = [];
let rejectKey;
let began, signal;
let stalled = false;
const receiver = https.createServer({
  key: readFileSync(p.tlsKey), cert: readFileSync(p.tlsCert),
}, async (req, res) => {
  try {
    assert.equal(req.method, 'POST');
    assert.equal(req.url, '/events');
    let raw = '';
    for await (const chunk of req) raw += chunk;
    const headers = new Headers(req.headers);
    const event = await hooks.unwrap(raw, headers, secrets[0]);
    for (const key of secrets.slice(1)) {
      await hooks.verifySignature(raw, headers, key);
    }
    if (rejectKey) {
      await assert.rejects(hooks.verifySignature(raw, headers, rejectKey));
    }
    assert.equal(event.object, 'event');
    assert.equal(event.type, 'response.completed');
    assert.ok(event.id.startsWith('evt_'));
    assert.ok(event.data.id.startsWith('resp_test_'));
    observed.push({type: event.type, eventId: event.id, signatures: secrets.length, status});
    if (stalled) {
      signal();
      return;
    }
    res.writeHead(status).end();
  } catch (error) {
    res.writeHead(500).end();
    process.stderr.write('Receiver verification failed: ' + error.name + '\n');
    process.exitCode = 1;
  }
});
const addresses = [];
const hidden = error => error instanceof OpenAI.APIError && error.status === 404;
const wire = {};
try {
  receiver.listen(p.subscriberPort, '127.0.0.1');
  await once(receiver, 'listening');
  const url = 'https://127.0.0.1:' + p.subscriberPort + '/events';
  wire.event_types = await hooks.eventTypes.list();
  assert.ok(wire.event_types.data.includes('response.completed'));
  const created = await hooks.create({name: 'Thor release notifications', url, event_types: ['response.completed']});
  addresses.push(created.id);
  secrets = [created.signing_secret];
  assert.ok(secrets[0].startsWith('whsec_'));
  wire.retrieved = await hooks.retrieve(created.id);
  assert.equal(wire.retrieved.signing_secret, undefined);
  assert.equal(wire.retrieved.url, url);
  assert.ok(!JSON.stringify(wire.retrieved).includes(secrets[0]));
  await assert.rejects(peer.webhooks.retrieve(created.id), hidden);
  assert.equal((await peer.webhooks.list()).data.length, 0);
  const another = await hooks.create({name: 'Second subscriber', url, event_types: ['batch.failed']});
  addresses.push(another.id);
  const page = await hooks.list({limit: 1});
  const next = await page.getNextPage();
  assert.deepEqual(new Set([page.data[0].id, next.data[0].id]), new Set(addresses));
  wire.no_change = await hooks.update(created.id, {name: created.name});
  assert.equal(wire.no_change.updated_at, created.updated_at);
  wire.updated = await hooks.update(created.id, {name: 'Release receipts'});
  assert.equal(wire.updated.name, 'Release receipts');
  wire.test = await hooks.test(created.id, {event_type:'response.completed'});
  assert.equal(wire.test.status_code, 202);
  const old = secrets[0];
  const rollover = await hooks.rotateSecret(created.id, {keep_old_secret_active_for_24_hours: true});
  secrets = [rollover.signing_secret, old];
  status = 429;
  wire.rejected = await hooks.test(created.id, {event_type:'response.completed'});
  assert.equal(wire.rejected.success, true);
  assert.equal(wire.rejected.status_code, 429);
  const rotated = await hooks.rotateSecret(created.id);
  rejectKey = rollover.signing_secret;
  secrets = [rotated.signing_secret, old];
  status = 200;
  wire.rotated = await hooks.test(created.id, {event_type:'response.completed'});
  assert.equal(wire.rotated.status_code, 200);
  began = new Promise(resolve => { signal = resolve; });
  stalled = true;
  const timeout = assert.rejects(hooks.test(created.id, {event_type:'response.completed'}),
    error => error instanceof OpenAI.APIError && [502, 504].includes(error.status));
  const watchdog = AbortSignal.timeout(8000);
  await Promise.race([began, once(watchdog, 'abort').then(() => { throw new Error('No outbound request'); })]);
  const chat = await peer.chat.completions.create({
    model: p.model, messages:[{role:'user',content:'Return only the integer 6 times 7.'}],
    temperature:0, max_tokens:16, chat_template_kwargs:{enable_thinking:false},
  });
  assert.equal(chat.choices[0].message.content.trim(), '42');
  await timeout;
  assert.equal(observed.length, 4);
  assert.equal(new Set(observed.map(e => e.eventId)).size, 4);
  for (const id of addresses) {
    const row = await hooks.delete(id);
    assert.equal(row.deleted, true);
    await assert.rejects(hooks.retrieve(id), hidden);
    wire.deleted = row;
  }
  process.stdout.write(JSON.stringify({sdk:version, wire, observed, peer:404, independent_chat:'42'}));
} finally {
  receiver.closeAllConnections();
  receiver.close();
}
