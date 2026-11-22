// Independent HTTPS subscriber running the pinned SDK verifier on the wire body.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import https from 'node:https';
import path from 'node:path';
import readline from 'node:readline';
import { pathToFileURL } from 'node:url';

const { default: OpenAI } = await import(pathToFileURL(path.resolve(process.argv[2], 'index.mjs')));
const input = readline.createInterface({ input: process.stdin });
let start, release;
const configuration = new Promise(resolve => { start = resolve; });
input.on('line', line => {
  if (line === 'release') release?.();
  else start(JSON.parse(line));
});
const config = await configuration;
const sdk = new OpenAI({ apiKey: 'unused-local-verifier', webhookSecret: config.secret });
const emit = value => console.log(JSON.stringify(value));
let liveId;
async function rejectCall(id, realtime) {
  const client = apiKey => new OpenAI({ apiKey, baseURL: config.api.baseURL,
    maxRetries: 0, timeout: 8000 });
  const owner = client(config.api.ownerKey);
  const other = client(config.api.otherKey);
  async function refused(request, expected) {
    try { await request; } catch (error) {
      assert.equal(error.status, expected);
      return error.status;
    }
    throw new Error('unexpected success');
  }
  const calls = client => realtime ? client.realtime.calls : client.live.sessions;
  const foreign = await refused(calls(other).reject(id, {status_code: 486}), 404);
  const invalid = await refused(calls(owner).reject(id, {status_code: 299}), 400);
  const controls = {};
  if (config.api.controlContract) {
    assert.ok(realtime);
    controls.null_status = await refused(calls(owner).reject(id, {status_code:null}), 400);
    controls.invalid_refer = await refused(calls(owner).refer(id, {target_uri:'sips:a%0@desk'}), 400);
    controls.pending_refer = await refused(calls(owner).refer(id, {target_uri:'tel:+12025550101'}), 409);
  }
  const operation = realtime
    ? (config.api.controlContract ? calls(owner).reject(id, null) : calls(owner).reject(id))
    : calls(owner).reject(id, {status_code: 486});
  const {data, response} = await operation.withResponse();
  assert.equal(response.status, 200);
  assert.equal(data, undefined);
  const retired = await refused(owner.live.sessions.reject(liveId, {status_code: 486}), 404);
  emit({control: {foreign, invalid, owner: response.status, empty: true, retired, ...controls}});
}
const server = https.createServer({
  cert: fs.readFileSync(config.cert), key: fs.readFileSync(config.key),
}, async (req, res) => {
  try {
    assert.equal(req.url, '/calls');
    assert.equal(req.method, 'POST');
    assert.equal(req.headers['content-type'], 'application/json');
    let body = '';
    req.setEncoding('utf8');
    for await (const chunk of req) {
      body += chunk;
      assert.ok(body.length <= (1 << 20));
    }
    const event = await sdk.webhooks.unwrap(body, req.headers);
    assert.equal(event.id, req.headers['webhook-id']);
    await assert.rejects(sdk.webhooks.unwrap(body + ' ', req.headers));
    await assert.rejects(sdk.webhooks.unwrap(body, req.headers, 'another-local-key'));
    await assert.rejects(sdk.webhooks.unwrap(body, {...req.headers, 'webhook-id': 'evt_other'}));
    await assert.rejects(sdk.webhooks.unwrap(body, {...req.headers, 'webhook-timestamp': '1'}));
    const action = config.responses.shift();
    assert.ok(action);
    let waiting;
    if (action === 'hold') waiting = new Promise(resolve => { release = resolve; });
    emit({ event, digest: createHash('sha256').update(body).digest('hex') });
    if (event.type === 'live.transport.incoming') liveId = event.data.session_id;
    if (config.api && event.type === (config.api.eventType ?? 'live.transport.incoming'))
      await rejectCall(event.data.call_id ?? event.data.session_id, !!event.data.call_id);
    if (waiting) await waiting;
    if (action === 307) res.setHeader('Location', '/other');
    res.writeHead(action === 'hold' ? 204 : action, { 'Content-Length': '0' });
    res.end();
  } catch (error) {
    emit({error: error.name, status: error.status, actual: error.actual,
      expected: error.expected});
    res.writeHead(400, {'Content-Length': '0'}).end();
  }
});
server.listen(0, '127.0.0.1', () => emit({port: server.address().port}));
