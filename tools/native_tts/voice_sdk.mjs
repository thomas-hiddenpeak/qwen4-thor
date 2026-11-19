// One pinned official SDK exchange; test client, never a runtime dependency.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import { basename, dirname, isAbsolute, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { loadSdk } from '../serve_protocol/sdk.mjs';

export const REQUEST_LIMIT = (10 << 20) + 65536;
export const RESPONSE_LIMIT = 4 << 20;
const sha = data => createHash('sha256').update(data).digest('hex');

async function boundedBody(body, capacity) {
  if (!body) return Buffer.alloc(0);
  const reader = body.getReader(), chunks = [];
  let count = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      count += value.byteLength;
      if (count > capacity) throw new Error('Voice SDK request byte bound');
      chunks.push(Buffer.from(value));
    }
  } catch (error) {
    // The clone may be a tee; cancellation completes when its peer closes.
    void reader.cancel().catch(() => {});
    throw error;
  } finally { reader.releaseLock(); }
  return Buffer.concat(chunks);
}

export async function execute(config, { fetchImpl = globalThis.fetch } = {}) {
  const endpoint = new URL(config.base_url);
  assert.ok(['127.0.0.1', '[::1]'].includes(endpoint.hostname) &&
    ['http:', 'https:'].includes(endpoint.protocol) && !endpoint.username &&
    !endpoint.password && !endpoint.search && !endpoint.hash,
    'owned loopback endpoint required');
  assert.notEqual(process.env.NODE_TLS_REJECT_UNAUTHORIZED, '0',
    'TLS verification must remain enabled');
  if (config.ca && fetchImpl === globalThis.fetch) {
    assert.equal(process.env.NODE_EXTRA_CA_CERTS, config.ca,
      'explicit CA must be installed at Node process startup');
  }
  const actions = ['consent_create', 'consent_list', 'consent_retrieve',
    'consent_update', 'consent_delete', 'voice_create', 'voice_delete',
    'speech', 'chat', 'chat_retrieve', 'transcribe'];
  assert.ok(actions.includes(config.action), 'unknown Voice SDK action');
  const body = { ...config.request };
  if (['consent_create', 'voice_create', 'transcribe'].includes(config.action)) {
    const { path, sha256 } = config.audio;
    assert.ok(isAbsolute(path), 'absolute fixed SDK input required');
    const st = fs.lstatSync(path);
    assert.ok(st.isFile() && st.size > 0 && st.size <= 10 << 20,
      'invalid fixed SDK input');
    const bytes = fs.readFileSync(path);
    assert.equal(sha(bytes), sha256, 'Voice SDK input identity drift');
    const field = { consent_create: 'recording', voice_create: 'audio_sample',
      transcribe: 'file' }[config.action];
    body[field] = new File([bytes], basename(path), { type: 'audio/wav' });
  }
  const { OpenAI, version } = loadSdk(config.sdk_directory);
  const exchanges = [], captured = [], requestWire = [];
  let responseBytes = 0;
  const client = new OpenAI({ baseURL: config.base_url, apiKey: config.api_key,
    maxRetries: 0, timeout: 180000,
    fetch: async (url, options) => {
      assert.equal(exchanges.length, 0, 'Voice SDK automatic replay forbidden');
      const request = new Request(url, { ...options, redirect: 'error' });
      assert.equal(new URL(request.url).origin, endpoint.origin,
        'SDK request escaped owned endpoint');
      let raw;
      try { raw = await boundedBody(request.clone().body, REQUEST_LIMIT); }
      catch (error) {
        if (request.body) await request.body.cancel();
        throw error;
      }
      requestWire.push(raw);
      const row = { method: request.method, url: request.url,
        content_type: request.headers.get('content-type') ?? '',
        request_bytes: raw.length, request_sha256: sha(raw) };
      exchanges.push(row);
      const response = await fetchImpl(request);
      row.status = response.status;
      row.response_headers = Object.fromEntries(response.headers);
      const source = response.body?.getReader();
      const stream = source && new ReadableStream({
        async pull(controller) {
          try {
            const { value, done } = await source.read();
            if (done) { controller.close(); source.releaseLock(); return; }
            responseBytes += value.byteLength;
            if (responseBytes > RESPONSE_LIMIT) {
              await source.cancel();
              throw new Error('Voice SDK response byte bound');
            }
            captured.push(Buffer.from(value));
            controller.enqueue(value);
          } catch (error) { controller.error(error); }
        },
        async cancel(reason) { await source.cancel(reason); },
      });
      return new Response(stream ?? null, { status: response.status,
        statusText: response.statusText, headers: response.headers });
    },
  });
  const result = { ok: false, version, exchanges };
  try {
    let answer;
    switch (config.action) {
      case 'consent_create':
        answer = await client.audio.voiceConsents.create(body); break;
      case 'consent_list':
        // Await one actual SDK page; don't invoke automatic pagination.
        answer = (await client.audio.voiceConsents.list(body)).body; break;
      case 'consent_retrieve':
        answer = await client.audio.voiceConsents.retrieve(body.consent_id); break;
      case 'consent_update': {
        const { consent_id, ...fields } = body;
        answer = await client.audio.voiceConsents.update(consent_id, fields); break;
      }
      case 'consent_delete':
        answer = await client.audio.voiceConsents.delete(body.consent_id); break;
      case 'voice_create':
        answer = await client.audio.voices.create(body); break;
      case 'voice_delete':
        answer = await client.audio.voices.delete(body.voice_id); break;
      case 'speech': {
        const response = await client.audio.speech.create(body), chunks = [];
        for await (const part of response.body) chunks.push(Buffer.from(part));
        const data = Buffer.concat(chunks);
        assert.ok(data.equals(Buffer.concat(captured)), 'SDK changed speech bytes');
        result.status = response.status;
        result.headers = Object.fromEntries(response.headers);
        answer = { bytes: data.length, sha256: sha(data) }; break;
      }
      case 'chat': {
        const response = await client.chat.completions.create(body);
        if (body.stream) {
          const events = [];
          for await (const event of response) events.push(event);
          answer = { events };
        } else answer = response;
        break;
      }
      case 'chat_retrieve':
        answer = await client.chat.completions.retrieve(body.completion_id); break;
      case 'transcribe':
        answer = await client.audio.transcriptions.create(body); break;
    }
    assert.equal(exchanges.length, 1, 'expected one actual SDK exchange');
    result.result = answer;
    result.ok = true;
  } catch (error) {
    result.error = { type: error.name, message: error.message,
      status: error.status ?? null, code: error.code ?? null };
  }
  return { result, wire: Buffer.concat(captured), request: Buffer.concat(requestWire) };
}

async function main() {
  const input = [];
  let count = 0;
  for await (const chunk of process.stdin) {
    count += chunk.length;
    assert.ok(count <= 65536, 'SDK configuration byte bound');
    input.push(chunk);
  }
  const config = JSON.parse(Buffer.concat(input).toString('utf8'));
  const work = fs.realpathSync(resolve(dirname(fileURLToPath(import.meta.url)),
    '../../.q4t-work'));
  assert.ok(isAbsolute(config.output) &&
    fs.realpathSync(dirname(config.output)).startsWith(work + sep),
    'private SDK output required');
  const { result, wire, request } = await execute(config);
  fs.writeFileSync(config.output, wire, { flag: 'wx', mode: 0o600 });
  fs.writeFileSync(config.output + '.request', request, { flag: 'wx', mode: 0o600 });
  Object.assign(result, { wire_bytes: wire.length, wire_sha256: sha(wire) });
  process.stdout.write(JSON.stringify(result) + '\n');
  return result.ok ? 0 : 1;
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  process.exitCode = await main();
}
