// Official SDK + native HTTP/state/job ownership. Deltas are explicit CPU
// fixtures; the crash case kills only this test's own CPU fixture process.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { readFileSync, existsSync, mkdirSync, rmSync, openSync, closeSync, appendFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import test from 'node:test';
import { loadSdk } from '../serve_protocol/sdk.mjs';

const { OpenAI } = loadSdk(process.env.Q4T_NODE_SDK_DIR);
assert.ok(process.env.Q4T_HOST_TESTS && process.env.Q4T_RESPONSES_BACKGROUND_OUTPUT);
const executable = resolve(process.env.Q4T_HOST_TESTS);
const output = resolve(process.env.Q4T_RESPONSES_BACKGROUND_OUTPUT);
let launches = 0;
async function peer(name, work) {
  const root = resolve(output, name);
  mkdirSync(root, { recursive: true });
  const ready = resolve(root, 'ready.json');
  rmSync(ready, { force: true });
  const log = openSync(resolve(root, `process-${++launches}.log`), 'w');
  const child = spawn(executable, ['--responses-jobs-fixture', root], { stdio: ['ignore', log, log] });
  closeSync(log);
  const exited = once(child, 'exit');
  let base, crashed = false;
  const traces = [];
  try {
    const until = Date.now() + 5000;
    while (!existsSync(ready) && child.exitCode === null && Date.now() < until) await delay(10);
    assert.ok(existsSync(ready), 'native background fixture readiness');
    const { port, pid } = JSON.parse(readFileSync(ready, 'utf8'));
    assert.equal(pid, child.pid);
    base = `http://127.0.0.1:${port}`;
    const sdk = key => new OpenAI({ apiKey: key, baseURL: base + '/v1', maxRetries: 0, timeout: 5000,
      fetch: async (url, init) => {
        const response = await fetch(url, init);
        const row = { url: String(url), method: init?.method, request: init?.body, status: response.status };
        // Cloning an SSE body would retain a second subscriber after abort.
        if (response.headers.get('content-type')?.includes('text/event-stream')) {
          appendFileSync(resolve(root, 'wire.jsonl'), JSON.stringify(row) + '\n');
        } else {
          traces.push(response.clone().text().then(body => appendFileSync(resolve(root, 'wire.jsonl'), JSON.stringify({ ...row, body }) + '\n')));
        }
        return response;
      } });
    const crash = async () => {
      crashed = true;
      child.kill('SIGKILL');
      assert.deepEqual(await exited, [null, 'SIGKILL']);
    };
    return await work(sdk('cpu-a'), sdk('cpu-b'), { root, crash });
  } finally {
    try {
      await Promise.all(traces);
      if (!crashed) {
        if (base && child.exitCode === null)
          await fetch(base + '/__fixture/stop', { method: 'POST', headers: { Authorization: 'Bearer cpu-a' }, signal: AbortSignal.timeout(3000) });
        assert.deepEqual(await Promise.race([exited, delay(5000).then(() => { throw new Error('fixture exit timeout'); })]), [0, null]);
      }
    } finally {
      if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
    }
  }
}
const input = { model: 'cpu', input: 'background CPU question', background: true, reasoning: { effort: 'none' } };
async function status(sdk, id, wanted) {
  const until = Date.now() + 5000;
  let value;
  do {
    value = await sdk.responses.retrieve(id);
    if (wanted.includes(value.status)) return value;
    await delay(20);
  } while (Date.now() < until);
  assert.fail(`response ${id} stayed ${value.status}, wanted ${wanted}`);
}
function executions(root) {
  const path = resolve(root, 'executed.jsonl');
  return existsSync(path) ? readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map(line => JSON.parse(line).id) : [];
}

test('official temporary background JSON survives the submitting request', async () => {
  await peer('json', async sdk => {
    const accepted = await sdk.responses.create({ ...input, store: false });
    assert.equal(accepted.status, 'queued');
    assert.equal(accepted.store, false);
    const done = await status(sdk, accepted.id, ['completed']);
    assert.equal(done.output_text, 'CPU background answer');
    assert.equal(done.usage.total_tokens, 8);
    await assert.rejects(sdk.responses.retrieve(done.id, { stream: true }), error => error.status === 400 && error.param === 'stream');
    await sdk.responses.delete(done.id);
    await assert.rejects(sdk.responses.retrieve(done.id), error => error.status === 404);
  });
});

test('official empty POST cancellation handles queued and running jobs idempotently', async () => {
  await peer('cancel', async (sdk, other, { root }) => {
    const running = await sdk.responses.create({ ...input, metadata: { fixture: 'hold' } });
    await status(sdk, running.id, ['in_progress']);
    const queued = await sdk.responses.create(input);
    assert.equal((await sdk.responses.retrieve(queued.id)).status, 'queued');
    await assert.rejects(other.responses.cancel(running.id), error => error.status === 404);
    for (const id of [queued.id, running.id]) {
      const cancelled = await sdk.responses.cancel(id);
      assert.equal(cancelled.status, 'cancelled');
      assert.equal(cancelled.error, null);
      assert.deepEqual(await sdk.responses.cancel(id), cancelled);
    }
    assert.deepEqual(executions(root), [running.id]);
    const next = await sdk.responses.create(input);
    assert.equal((await status(sdk, next.id, ['completed'])).output_text, 'CPU background answer');
  });
});

test('official background stream can disconnect and resume original sequence', async () => {
  await peer('stream', async (sdk, other, { root }) => {
    const original = [];
    const stream = await sdk.responses.create({ ...input, stream: true,
      stream_options: { include_obfuscation: false }, metadata: { fixture: 'hold' } });
    let id;
    for await (const event of stream) {
      original.push(event);
      if (event.type === 'response.created') id = event.response.id;
      if (event.type === 'response.output_text.delta') break;
    }
    assert.ok(stream.controller.signal.aborted);
    assert.equal((await sdk.responses.retrieve(id)).status, 'in_progress');
    await assert.rejects(other.responses.retrieve(id), error => error.status === 404);
    const replay = [];
    for await (const event of await sdk.responses.retrieve(id, { stream: true, starting_after: 1, include_obfuscation: false })) {
      replay.push(event);
      if (event.type === 'response.output_text.delta') break;
    }
    assert.deepEqual(replay, original.filter(event => event.sequence_number > 1));
    const cancelled = await sdk.responses.cancel(id);
    assert.equal(cancelled.status, 'cancelled');
    assert.equal(cancelled.output[0].status, 'in_progress');
    const terminalReplay = [];
    for await (const event of await sdk.responses.retrieve(id, { stream: true, include_obfuscation: false })) terminalReplay.push(event);
    assert.deepEqual(terminalReplay, original);
    assert.ok(!terminalReplay.some(event => ['response.completed', 'response.cancelled'].includes(event.type)));
    appendFileSync(resolve(root, 'events.jsonl'), [original, replay, terminalReplay].map(events => JSON.stringify(events)).join('\n') + '\n');
  });
});

test('actual CPU process crash resumes queued work and never replays started work', async () => {
  let started, queued;
  await peer('crash', async (sdk, other, { crash }) => {
    started = (await sdk.responses.create({ ...input, metadata: { fixture: 'hold' } })).id;
    await status(sdk, started, ['in_progress']);
    queued = (await sdk.responses.create(input)).id;
    assert.equal((await sdk.responses.retrieve(queued)).status, 'queued');
    await crash();
  });
  await peer('crash', async (sdk, other, { root }) => {
    const interrupted = await status(sdk, started, ['failed']);
    assert.equal(interrupted.error.code, 'background_execution_interrupted');
    assert.equal(interrupted.output_text, 'CPU background answer');
    assert.equal((await status(sdk, queued, ['completed'])).output_text, 'CPU background answer');
    assert.deepEqual(executions(root).sort(), [started, queued].sort());
    await assert.rejects(other.responses.retrieve(queued), error => error.status === 404);
  });
});
