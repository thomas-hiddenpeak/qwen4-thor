/** Replay the recorded native 503 and WAV with an installed official Node SDK. */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const directory = resolve(process.argv[2]);
const { default: OpenAI } = await import(pathToFileURL(resolve(directory, 'index.mjs')).href);
const version = JSON.parse(readFileSync(resolve(directory, 'package.json'), 'utf8')).version;
const capture = JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-sdk-method-matrix-20261127/recording-download-retries.json',
  import.meta.url), 'utf8'));
const expected = capture.clients.find(row => row.language === 'node' && row.sdk === version);
assert.ok(expected, 'unrecorded Node SDK version');
const packets = capture.packets.slice(1, 3);
const times = [];
const client = new OpenAI({
  apiKey: 'fixture', baseURL: 'http://sdk.invalid/v1', maxRetries: 1,
  fetch: async (url, options) => {
    assert.equal(options.method, 'GET');
    assert.equal(String(url), 'http://sdk.invalid/v1/live/sessions/live_recorded/content');
    const packet = packets[times.length];
    times.push(performance.now());
    return new Response(Buffer.from(packet.body, 'base64'), {
      status: packet.status, headers: packet.headers,
    });
  },
});
const request = expected.mode === 'resource' ? client.live.sessions.downloadRecording('live_recorded')
  : client.get('/live/sessions/live_recorded/content');
const response = await request.asResponse();
assert.equal(times.length, 2);
assert.equal(response.status, 200);
assert.equal(response.headers.get('x-request-id'), expected.trace);
assert.equal(createHash('sha256').update(Buffer.from(await response.arrayBuffer())).digest('hex'),
             expected.sha256);
const delay = (times[1] - times[0]) / 1000;
assert.ok(delay >= Number(packets[0].headers['Retry-After']) - 0.01);
console.log(JSON.stringify({ ...expected, wait_seconds: Number(delay.toFixed(3)) }));
