// Validate the actual acceptance transport against a native CPU-only fixture.
import assert from 'node:assert/strict';
import test from 'node:test';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { existsSync, mkdirSync, readFileSync, openSync, closeSync } from 'node:fs';
import { resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { execute, responseTerminal } from './sdk.mjs';

test('replay oracle rejects gaps, identity changes, premature EOF and Chat DONE', () => {
  const response = { id: 'resp_a', status: 'completed', output: [] };
  const events = [{ type: 'response.created', sequence_number: 0, response: { ...response, status: 'in_progress' } },
    { type: 'response.completed', sequence_number: 1, response }];
  assert.deepEqual(responseTerminal(events, ''), response);
  const bad = structuredClone(events); bad[1].sequence_number = 2;
  assert.throws(() => responseTerminal(bad, ''), /sequence/);
  const foreign = structuredClone(events); foreign[1].response.id = 'other';
  assert.throws(() => responseTerminal(foreign, ''), /identity/);
  assert.throws(() => responseTerminal(events.slice(0, 1), ''), /terminal/);
  assert.throws(() => responseTerminal([...events, events[1]], ''), /after terminal/);
  assert.throws(() => responseTerminal(events, 'data: [DONE]'), /Chat/);
  assert.deepEqual(responseTerminal(events.slice(1), '', 0), response);
});

test('bounded official SDK bridge executes actual state HTTP without retries', async () => {
  assert.ok(process.env.Q4T_HOST_TESTS && process.env.Q4T_TEST_OUTPUT_DIR);
  const root = resolve(process.env.Q4T_TEST_OUTPUT_DIR, 'native-bridge');
  mkdirSync(root, { recursive: true });
  const log = openSync(resolve(root, 'fixture.log'), 'wx');
  const child = spawn(resolve(process.env.Q4T_HOST_TESTS), ['--responses-api-fixture', root],
    { stdio: ['ignore', log, log] });
  closeSync(log);
  const exited = once(child, 'exit');
  let base;
  try {
    const ready = resolve(root, 'ready.json');
    const deadline = Date.now() + 5000;
    while (!existsSync(ready) && child.exitCode === null && Date.now() < deadline) await delay(10);
    assert.ok(existsSync(ready));
    const info = JSON.parse(readFileSync(ready)); assert.equal(info.pid, child.pid);
    base = `http://127.0.0.1:${info.port}`;
    const invoke = (action, fields = {}) => execute({ action, api_key: 'cpu-a',
      base_url: base + '/v1', timeout_ms: 3000, ...fields });
    const first = await invoke('create', { request: { model: 'cpu', input: 'one',
      reasoning: { effort: 'none' }, stream: true } });
    assert.equal(first.ok, true, JSON.stringify(first.error));
    assert.equal(first.exchanges.length, 1);
    assert.ok(Buffer.from(first.wire_base64, 'base64').includes(Buffer.from('response.completed')));
    const id = first.result.id;
    const saved = await invoke('retrieve', { id });
    assert.equal(saved.ok, true); assert.equal(saved.result.status, 'completed');
    const replay = await invoke('retrieve', { id, query: { stream: true, starting_after: 1 } });
    assert.equal(replay.ok, true); assert.equal(replay.chunks[0].sequence_number, 2);
    const items = await invoke('input_items', { id, query: { order: 'asc' } });
    assert.equal(items.ok, true); assert.equal(items.result.data[0].content[0].text, 'one');
    assert.equal((await invoke('delete', { id })).result.deleted, true);
    const absent = await invoke('retrieve', { id });
    assert.equal(absent.ok, false); assert.equal(absent.error.status, 404);
    assert.equal(absent.exchanges.length, 1);
  } finally {
    try {
      if (base && child.exitCode === null)
        await fetch(base + '/__fixture/stop', { method: 'POST', headers: { Authorization: 'Bearer cpu-a' }, signal: AbortSignal.timeout(2000) });
      const timer = new AbortController();
      try {
        assert.deepEqual(await Promise.race([exited, delay(5000, undefined, { signal: timer.signal }).then(() => { throw new Error('fixture exit timeout'); })]), [0, null]);
      } finally { timer.abort(); }
    } finally { if (child.exitCode === null) child.kill('SIGKILL'); }
  }
});
