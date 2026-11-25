// Verify binary MIME negotiation and the explicit SSE body on the official SDK.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const piece of process.stdin) {
  input += piece;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const seen = [];
const client = new OpenAI({baseURL:plan.base, apiKey:plan.key, timeout:90000, maxRetries:0,
  fetch: (url, options) => {
    seen.push(new Headers(options.headers).get('accept'));
    return fetch(url, options);
  }});
const request = {model:plan.model, voice:'vivian', input:'Hello.', response_format:'opus'};
const cases = [
  {name:'sdk', type:'application/octet-stream'},
  {name:'opus', accept:'audio/opus', type:'audio/opus'},
  {name:'wildcard', accept:'audio/*', type:'audio/ogg'},
  {name:'events', type:'text/event-stream', sse:true},
];
const rows = [];
for (const item of cases) {
  const args = item.accept ? {headers:{Accept:item.accept}} : {};
  const body = item.sse ? {...request, stream_format:'sse'} : request;
  const response = await client.audio.speech.create(body, args);
  assert.equal(response.status, 200);
  const mime = response.headers.get('content-type').split(';')[0];
  assert.equal(mime, item.type);
  assert.equal(seen.at(-1), item.accept ?? 'application/octet-stream');
  let data = Buffer.from(await response.arrayBuffer());
  assert.ok(data.length > 0 && data.length <= 2 * 1024 * 1024);
  if (item.sse) {
    const frames = data.toString('utf8').split(/\r?\n\r?\n/).filter(Boolean)
      .map(event => JSON.parse(event.split(/\r?\n/).find(line => line.startsWith('data: ')).slice(6)));
    assert.equal(frames.at(-1).type, 'speech.audio.done');
    const deltas = frames.slice(0, -1);
    assert.ok(deltas.length && deltas.every(e => e.type === 'speech.audio.delta'));
    data = Buffer.concat(deltas.map(e => Buffer.from(e.audio, 'base64')));
  }
  assert.equal(data.subarray(0, 4).toString(), 'OggS');
  assert.ok(data.includes(Buffer.from('OpusHead')));
  writeFileSync(join(plan.output, item.name+'.opus'), data, {flag:'wx',mode:0o600});
  rows.push({name:item.name, accept:seen.at(-1), mime, bytes:data.length,
    sha256:createHash('sha256').update(data).digest('hex')});
}
for (const [accept, status, code] of [['audio/wav',406,'not_acceptable'],['audio/opus;q=bad',400,'invalid_request']]) {
  await assert.rejects(client.audio.speech.create(request,{headers:{Accept:accept}}),
    e => e.status === status && e.code === code && e.type === 'invalid_request_error');
}
process.stdout.write(JSON.stringify({sdk:version, rows, negative:[406,400]}));
