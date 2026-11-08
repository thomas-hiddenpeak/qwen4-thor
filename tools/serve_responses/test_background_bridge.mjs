import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdirSync, existsSync, readFileSync, openSync, closeSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import test from 'node:test';
import { execute, prefix } from './background_sdk.mjs';

test('prefix oracle rejects gaps, false cancelled events and completed prefixes', () => {
  const events = [{ type: 'response.created', sequence_number: 0, response: { id: 'resp_a' } },
    { type: 'response.output_text.delta', sequence_number: 1, delta: 'x' }];
  assert.deepEqual(prefix(events), { identity: 'resp_a', terminal: false, last_sequence: 1 });
  assert.throws(() => prefix([]), /empty/);
  assert.throws(() => prefix(events, 1), /gap/);
  assert.throws(() => prefix(events, -1, 'resp_b'), /identity/);
  assert.throws(() => prefix([{ type: 'response.cancelled', sequence_number: 0 }]), /unexpected/);
  assert.throws(() => prefix([{ type: 'response.completed', sequence_number: 0 },
    { type: 'response.output_text.delta', sequence_number: 1 }]), /after terminal/);
});

test('single-consumer SDK bridge aborts real native SSE and cancels queued/running ownership', async () => {
  assert.ok(process.env.Q4T_HOST_TESTS && process.env.Q4T_TEST_OUTPUT_DIR);
  const root = resolve(process.env.Q4T_TEST_OUTPUT_DIR, 'background-bridge');
  mkdirSync(root, { recursive: true });
  const log = openSync(resolve(root, 'fixture.log'), 'wx');
  const child = spawn(resolve(process.env.Q4T_HOST_TESTS), ['--responses-jobs-fixture', root],
    { stdio: ['ignore', log, log] });
  closeSync(log);
  const exited = once(child, 'exit');
  let base, call = 0;
  try {
    const ready = resolve(root, 'ready.json');
    const end = Date.now()+5000;
    while (!existsSync(ready) && child.exitCode === null && Date.now() < end) await delay(10);
    assert.ok(existsSync(ready), 'native fixture readiness');
    const info = JSON.parse(readFileSync(ready, 'utf8'));
    assert.equal(info.pid, child.pid);
    base = `http://127.0.0.1:${info.port}`;
    const invoke = async (action, fields = {}) => {
      const result = await execute({ action, api_key: 'cpu-a', base_url: base+'/v1',
        timeout_ms: 3000, ...fields });
      writeFileSync(resolve(root, `call-${++call}.json`), JSON.stringify(result, null, 2)+'\n');
      assert.ok(result.ok, JSON.stringify(result.error));
      assert.equal(result.exchanges.length, 1);
      return result;
    };
    const input = { model: 'cpu', input: 'question', background: true, reasoning: { effort: 'none' } };
    const stream = await invoke('create', { request: { ...input, stream: true,
      stream_options: { include_obfuscation: false }, metadata: { fixture: 'hold' } },
      stream_mode: 'first_text_delta' });
    const id = stream.result.id;
    assert.equal(stream.result.subscriber_aborted, true);
    assert.equal((await invoke('retrieve', { id })).result.status, 'in_progress');
    const queued = (await invoke('create', { request: input })).result.id;
    assert.equal((await invoke('retrieve', { id: queued })).result.status, 'queued');
    const cancel = await invoke('cancel', { id: queued });
    assert.equal(cancel.result.status, 'cancelled');
    assert.ok(!cancel.exchanges[0].body, 'official cancel must send empty body');
    const ended = await invoke('cancel', { id });
    assert.equal(ended.result.status, 'cancelled');
    assert.ok(ended.result.output.length);
    const replay = await invoke('retrieve', { id, query: { stream: true, include_obfuscation: false },
      stream_mode: 'cancelled_replay' });
    assert.equal(replay.result.terminal_event, false);
    assert.deepEqual(replay.chunks.slice(0, stream.chunks.length), stream.chunks);
    assert.ok(replay.chunks.filter(e => e.type === 'response.output_text.delta').every(e => Array.isArray(e.logprobs)));
    const executions = readFileSync(resolve(root, 'executed.jsonl'), 'utf8').trim().split('\n').map(line => JSON.parse(line).id);
    assert.deepEqual(executions, [id]);
  } finally {
    try {
      if (base && child.exitCode === null) await fetch(base+'/__fixture/stop', {
        method: 'POST', headers: { Authorization: 'Bearer cpu-a' }, signal: AbortSignal.timeout(2000) });
      assert.deepEqual(await Promise.race([exited,
        delay(5000).then(() => { throw new Error('fixture exit timeout'); })]), [0, null]);
    } finally {
      if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
    }
  }
});
