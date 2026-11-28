// Read two Python-created Batches and their actual output with the official SDK.
import assert from 'node:assert/strict';
import {readFileSync, writeFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {setTimeout as sleep} from 'node:timers/promises';
import {pathToFileURL} from 'node:url';

const cfg = JSON.parse(readFileSync(0, 'utf8'));
const root = resolve(process.argv[2]);
const {name, version} = JSON.parse(readFileSync(resolve(root, 'package.json')));
assert.equal(name, 'openai');
const {default: OpenAI} = await import(pathToFileURL(resolve(root, 'index.mjs')));
const base = new URL(cfg.base);
assert.equal(base.protocol, cfg.managed_loopback === true ? 'http:' : 'https:');
assert.ok(base.port && !base.username && !base.password && !base.search && !base.hash);
assert.equal(base.pathname, '/v1');
assert.equal(base.hostname, '127.0.0.1');
const requests = [];
const remaining = cfg.max_requests - cfg.writer.requests.length;
assert.ok(Number.isInteger(remaining) && remaining > 0 && remaining <= 55);
function client(key) {
  return new OpenAI({apiKey: key, baseURL: cfg.base, maxRetries: 0, timeout: 45000,
    fetch: async (input, options) => {
      if (input === 'data:,') return fetch(input, options);
      assert.ok(requests.length < remaining, 'Batch HTTP request budget reached');
      const request = new Request(input, options);
      const url = new URL(request.url);
      assert.equal(url.origin, base.origin);
      if (cfg.managed_loopback === true) await sleep(250);
      const response = await fetch(request);
      requests.push({method: request.method, path: url.pathname, query: url.search.slice(1),
        status: response.status, trace: response.headers.get('x-request-id')});
      return response;
    }});
}
const owner = client(cfg.key);
const peer = client(cfg.peer);
async function terminal(original, expected) {
  for (let attempt = 0; attempt < 18; ++attempt) {
    const job = await owner.batches.retrieve(original.id);
    assert.equal(job.id, original.id);
    assert.equal(job.input_file_id, original.input);
    assert.deepEqual(job.metadata, original.metadata);
    if (['completed','cancelled','failed','expired'].includes(job.status)) {
      assert.equal(job.status, expected);
      return job;
    }
    await sleep(1500);
  }
  throw new Error('Batch exceeded its bounded completion window');
}

try {
  const {completed, pending, cancel, revision} = cfg.writer;
  const stopped = await terminal(pending, 'cancelled');
  const repeated = await owner.batches.cancel(pending.id);
  assert.equal(repeated.status, 'cancelled');
  assert.equal(repeated.cancelling_at, cancel[0].cancelling_at);
  assert.equal(repeated.cancelled_at, stopped.cancelled_at);
  assert.equal(stopped.cancelling_at, cancel[0].cancelling_at);
  const finished = await terminal(completed, 'completed');
  assert.equal(finished.request_counts.total, 1);
  assert.equal(finished.request_counts.completed, 1);
  assert.equal(finished.request_counts.failed, 0);
  await assert.rejects(owner.batches.cancel(completed.id),
    error => error.status === 409 && error.code === 'batch_already_terminal');
  await assert.rejects(peer.batches.retrieve(completed.id), error => error.status === 404);
  const first = await owner.batches.list({limit: 1});
  assert.equal(first.data.length, 1);
  assert.ok(first.hasNextPage());
  const second = await first.getNextPage();
  assert.equal(second.data.length, 1);
  assert.equal(second.hasNextPage(), false);
  assert.deepEqual([first.data[0].id, second.data[0].id].sort(), [finished.id, stopped.id].sort());
  const response = await owner.files.content(finished.output_file_id);
  const outputText = await response.text();
  const lines = outputText.trimEnd().split('\n');
  assert.equal(lines.length, 1);
  const row = JSON.parse(lines[0]);
  assert.equal(row.custom_id, 'publication-0');
  assert.equal(row.error, null);
  assert.equal(row.response.status_code, 200);
  assert.ok(row.response.request_id);
  assert.equal(row.response.body.choices[0].message.content.trim(), String(revision));
  const chat = await owner.chat.completions.create({model:cfg.model, temperature:0,
    reasoning_effort:'none', max_completion_tokens:24,
    messages:[{role:'user',content:'What is 6 times 7? Answer with just the integer.'}]});
  assert.equal(chat.choices[0].message.content.trim(),'42');
  for (const id of new Set([pending.input,completed.input,finished.output_file_id,
    stopped.output_file_id,stopped.error_file_id].filter(Boolean))) {
    assert.equal((await owner.files.delete(id)).deleted,true);
  }
  console.log(JSON.stringify({sdk:'node-'+version, stopped, finished, result:row, output_text:outputText,
    repeated:{id:repeated.id,status:repeated.status,cancelled_at:repeated.cancelled_at,cancelling_at:repeated.cancelling_at},
    page_ids:[first.data[0].id,second.data[0].id], independent_chat:'42', requests}));
} finally {
  if (cfg.capture) writeFileSync(cfg.capture,JSON.stringify(requests),{flag:'wx'});
}
