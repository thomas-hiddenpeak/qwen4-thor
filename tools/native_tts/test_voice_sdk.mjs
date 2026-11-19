// Actual pinned SDK serialization/consumption; CPU fixtures, no model evidence.
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import fs from 'node:fs';
import https from 'node:https';
import path from 'node:path';
import test from 'node:test';
import { execute, REQUEST_LIMIT, RESPONSE_LIMIT } from './voice_sdk.mjs';

const sdk = process.env.Q4T_NODE_SDK_DIR;
assert.ok(sdk, 'explicit pinned SDK directory required');
const sha = b => createHash('sha256').update(b).digest('hex');
const consent = { id: 'cons_actual', object: 'audio.voice_consent',
  name: 'test', language: 'en', created_at: 1 };
const voice = { id: 'voice_actual', object: 'audio.voice',
  name: 'test', type: 'audio_sample', created_at: 1 };
const config = (action, request = {}) => ({ action, request,
  sdk_directory: sdk, base_url: 'https://127.0.0.1:18490/voice-test/v1',
  api_key: 'owned-cpu-test' });

function temporary(t) {
  const root = path.resolve('.q4t-work/voice-sdk-js-cpu');
  fs.mkdirSync(root, { recursive: true });
  const directory = fs.mkdtempSync(root + '/case-');
  t.after(() => fs.rmSync(directory, { recursive: true }));
  return directory;
}

function sample(directory) {
  const filename = path.join(directory, '样本.wav');
  const data = Buffer.from('RIFF CPU fixture, no model evidence');
  fs.writeFileSync(filename, data);
  return { path: filename, sha256: sha(data) };
}

async function call(value, response, status = 200) {
  const seen = [];
  const output = await execute(value, { fetchImpl: async request => {
    const raw = Buffer.from(await request.arrayBuffer());
    seen.push({ method: request.method, url: new URL(request.url), raw,
      content_type: request.headers.get('content-type') });
    return new Response(JSON.stringify(response), { status,
      headers: { 'content-type': 'application/json' } });
  } });
  assert.equal(seen.length, 1);
  assert.equal(output.result.version, '7.47.0');
  assert.equal(output.result.exchanges.length, 1);
  assert.deepEqual(JSON.parse(output.wire), response);
  assert.deepEqual(output.request, seen[0].raw);
  return { ...output, seen: seen[0] };
}

test('official voice and consent create preserve multipart bytes and fields', async t => {
  const audio = sample(temporary(t));
  for (const [action, request, answer, field, route] of [
    ['consent_create', { name: 'test', language: 'en' }, consent,
      'recording', '/audio/voice_consents'],
    ['voice_create', { name: 'test', consent: consent.id, type: 'audio_sample' },
      voice, 'audio_sample', '/audio/voices'],
  ]) {
    const output = await call({ ...config(action, request), audio }, answer);
    assert.equal(output.result.ok, true);
    assert.deepEqual(output.result.result, answer);
    assert.equal(output.seen.url.pathname, '/voice-test/v1' + route);
    assert.equal(output.seen.method, 'POST');
    assert.match(output.seen.content_type, /^multipart\/form-data; boundary=/);
    assert.ok(output.request.includes(Buffer.from(`name="${field}"`)));
    assert.ok(output.request.includes(fs.readFileSync(audio.path)));
    for (const [key, value] of Object.entries(request)) {
      assert.ok(output.request.includes(Buffer.from(`name="${key}"`)));
      assert.ok(output.request.includes(Buffer.from(value)));
    }
  }
});

test('metadata uses original SDK IDs and verbs without automatic pagination', async () => {
  const list = { object: 'list', data: [consent], has_more: true,
    first_id: consent.id, last_id: consent.id };
  for (const [action, request, answer, method, route] of [
    ['consent_retrieve', { consent_id: 'cons/a' }, consent, 'GET',
      '/audio/voice_consents/cons%2Fa'],
    ['consent_update', { consent_id: consent.id, name: 'renamed' },
      { ...consent, name: 'renamed' }, 'POST', '/audio/voice_consents/cons_actual'],
    ['consent_list', { limit: 1 }, list, 'GET', '/audio/voice_consents'],
    ['consent_delete', { consent_id: consent.id }, { id: consent.id, deleted: true },
      'DELETE', '/audio/voice_consents/cons_actual'],
    ['voice_delete', { voice_id: voice.id }, { id: voice.id, deleted: true },
      'DELETE', '/audio/voices/voice_actual'],
  ]) {
    const output = await call(config(action, request), answer);
    assert.equal(output.result.ok, true);
    assert.deepEqual(output.result.result, answer);
    assert.equal(output.seen.method, method);
    assert.equal(output.seen.url.pathname, '/voice-test/v1' + route);
    if (action === 'consent_list') assert.equal(output.seen.url.search, '?limit=1');
    if (action === 'consent_update') {
      assert.deepEqual(JSON.parse(output.request), { name: 'renamed' });
    }
  }
});

test('error bodies and status survive the SDK without retries', async () => {
  for (const [status, code] of [[400, 'voice_consent_mismatch'],
    [404, 'object_not_found'], [429, 'client_tpm_exhausted'],
    [503, 'capacity_exhausted']]) {
    const output = await call(config('consent_retrieve', { consent_id: consent.id }),
      { error: { message: 'CPU negative', type: 'invalid_request_error', code,
        param: null } }, status);
    assert.equal(output.result.ok, false);
    assert.equal(output.result.error.status, status);
    assert.equal(output.result.error.code, code);
  }
});

function fragmented(data, contentType) {
  let offset = 0;
  return new Response(new ReadableStream({ pull(controller) {
    if (offset === data.length) { controller.close(); return; }
    const end = Math.min(offset + 3, data.length);
    controller.enqueue(data.subarray(offset, end)); offset = end;
  } }), { headers: { 'content-type': contentType } });
}

test('speech binary and SSE preserve every response byte with custom voice ID', async () => {
  for (const [data, type] of [[Buffer.from([82, 73, 70, 70, 0, 255]), 'audio/wav'],
    [Buffer.from('data: {"type":"speech.audio.delta","audio":"YQ=="}\n\n' +
      'data: {"type":"speech.audio.done"}\n\n'), 'text/event-stream']]) {
    const request = { model: 'base', input: 'Hello', voice: { id: voice.id },
      response_format: 'wav', stream_format: type === 'audio/wav' ? 'audio' : 'sse' };
    const output = await execute(config('speech', request), {
      fetchImpl: async actual => {
        assert.deepEqual(await actual.json(), request);
        return fragmented(data, type);
      },
    });
    assert.equal(output.result.ok, true);
    assert.deepEqual(output.wire, data);
    assert.deepEqual(output.result.result, { bytes: data.length, sha256: sha(data) });
  }
});

test('chat SDK consumes split audio SSE and retains history and extensions', async () => {
  const request = { model: 'cpu', messages: [{ role: 'user', content: 'Hello' }],
    modalities: ['text', 'audio'], audio: { voice: { id: voice.id }, format: 'wav' },
    stream: true, enable_thinking: false, request_id: 'cpu', cancel_token: 'cpu' };
  const event = { id: 'chatcmpl_cpu', object: 'chat.completion.chunk', created: 1,
    model: 'cpu', choices: [{ index: 0, delta: { content: '你好',
      audio: { id: 'audio_actual', data: 'YQ==', transcript: '你好' } },
      finish_reason: 'stop' }] };
  const data = Buffer.from('data: ' + JSON.stringify(event) + '\n\ndata: [DONE]\n\n');
  const output = await execute(config('chat', request), { fetchImpl: async actual => {
    assert.deepEqual(await actual.json(), request);
    return fragmented(data, 'text/event-stream');
  } });
  assert.equal(output.result.ok, true);
  assert.deepEqual(output.result.result.events, [event]);
  assert.deepEqual(output.wire, data);
  const saved = { id: 'chatcmpl_cpu', choices: [{ message: { role: 'assistant',
    content: '你好', audio: { id: 'audio_actual', transcript: '你好' } } }] };
  const history = await call(config('chat_retrieve', { completion_id: 'chatcmpl_cpu' }), saved);
  assert.equal(history.result.ok, true);
  assert.equal(history.seen.url.pathname, '/voice-test/v1/chat/completions/chatcmpl_cpu');
  assert.deepEqual(history.result.result, saved);
});

test('ASR uses full fixed input without text hints; bad hash prevents transport', async t => {
  const audio = sample(temporary(t));
  const value = { ...config('transcribe', { model: 'asr', response_format: 'json' }), audio };
  const output = await call(value, { text: 'hello' });
  assert.equal(output.result.ok, true);
  assert.equal(output.seen.url.pathname, '/voice-test/v1/audio/transcriptions');
  for (const field of ['text', 'prompt', 'language']) {
    assert.ok(!output.request.includes(Buffer.from(`name="${field}"`)));
  }
  let count = 0;
  await assert.rejects(execute({ ...value, audio: { ...audio, sha256: '0'.repeat(64) } },
    { fetchImpl: async () => { ++count; } }), /identity drift/);
  assert.equal(count, 0);
});

test('request and response byte bounds fail without hidden replay', async () => {
  let requests = 0, cancelled = false;
  const value = config('consent_retrieve', { consent_id: consent.id });
  const output = await execute(value, { fetchImpl: async () => {
    ++requests;
    return new Response(new ReadableStream({ start(controller) {
      controller.enqueue(new Uint8Array(RESPONSE_LIMIT + 1));
    }, cancel() { cancelled = true; } }));
  } });
  assert.equal(requests, 1); assert.equal(cancelled, true);
  assert.equal(output.result.ok, false); assert.equal(output.wire.length, 0);
  requests = 0;
  const excessive = await execute(config('speech', { model: 'base', voice: 'alloy',
    input: 'a'.repeat(REQUEST_LIMIT + 1) }), {
    fetchImpl: async () => { ++requests; throw new Error('must not send'); },
  });
  assert.equal(excessive.result.ok, false); assert.equal(requests, 0);
});

test('endpoint, symlink input and TLS bypass controls fail before transport', async t => {
  let requests = 0;
  const fetchImpl = async () => { ++requests; };
  for (const base_url of ['https://api.openai.com/v1', 'https://user@127.0.0.1/v1',
    'https://127.0.0.1/v1?query=yes', 'file:///tmp/voice']) {
    await assert.rejects(execute({ ...config('consent_list'), base_url },
      { fetchImpl }), /loopback/);
  }
  const directory = temporary(t), audio = sample(directory);
  const link = path.join(directory, 'link.wav'); fs.symlinkSync(audio.path, link);
  await assert.rejects(execute({ ...config('voice_create'),
    audio: { ...audio, path: link } }, { fetchImpl }), /fixed SDK input/);
  const old = process.env.NODE_TLS_REJECT_UNAUTHORIZED;
  try {
    process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';
    await assert.rejects(execute(config('consent_list'), { fetchImpl }), /verification/);
  } finally {
    if (old === undefined) delete process.env.NODE_TLS_REJECT_UNAUTHORIZED;
    else process.env.NODE_TLS_REJECT_UNAUTHORIZED = old;
  }
  assert.equal(requests, 0);
});

async function cli(config, ca) {
  const env = { ...process.env };
  delete env.NODE_TLS_REJECT_UNAUTHORIZED; delete env.NODE_EXTRA_CA_CERTS;
  if (ca) env.NODE_EXTRA_CA_CERTS = ca;
  const child = spawn(process.execPath, ['tools/native_tts/voice_sdk.mjs'],
    { env, stdio: ['pipe', 'pipe', 'pipe'], timeout: 10000 });
  let stdout = '', stderr = '';
  child.stdout.on('data', b => { stdout += b; });
  child.stderr.on('data', b => { stderr += b; });
  child.stdin.end(JSON.stringify(config));
  const [code, signal] = await once(child, 'close');
  assert.equal(signal, null, stderr);
  return { code, stdout, stderr };
}

test('real TLS CLI verifies trusted certificate and rejects untrusted peer', async t => {
  const directory = temporary(t), key = path.join(directory, 'key.pem');
  const ca = path.join(directory, 'cert.pem');
  const generated = spawnSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048',
    '-nodes', '-keyout', key, '-out', ca, '-days', '1', '-subj', '/CN=localhost',
    '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost'], { timeout: 10000 });
  assert.equal(generated.status, 0, generated.stderr.toString());
  let requests = 0;
  const server = https.createServer({ key: fs.readFileSync(key), cert: fs.readFileSync(ca) },
    (_request, response) => {
      ++requests; response.writeHead(200, { 'content-type': 'application/json' });
      response.end(JSON.stringify(consent));
    });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  try {
    const value = { ...config('consent_retrieve', { consent_id: consent.id }),
      base_url: `https://127.0.0.1:${server.address().port}/v1`,
      output: path.join(directory, 'trusted.wire'), ca };
    const accepted = await cli(value, ca);
    assert.equal(accepted.code, 0, accepted.stderr);
    const receipt = JSON.parse(accepted.stdout);
    assert.equal(receipt.ok, true); assert.equal(requests, 1);
    assert.deepEqual(JSON.parse(fs.readFileSync(value.output)), consent);
    const rejected = await cli({ ...value, ca: undefined,
      output: path.join(directory, 'untrusted.wire') });
    assert.equal(rejected.code, 1, rejected.stderr);
    assert.equal(JSON.parse(rejected.stdout).ok, false);
    assert.equal(requests, 1, 'untrusted TLS must not reach HTTP');
  } finally { server.closeAllConnections(); await new Promise(r => server.close(r)); }
});
