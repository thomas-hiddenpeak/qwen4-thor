import assert from 'node:assert/strict';
import test from 'node:test';
import { recoverTrace } from './tracing_poll.mjs';

const unavailable = () => Object.assign(new Error('worker backoff'), { status: 503 });
test('read recovery retains backoff then waits for the requested durable state', async () => {
  const calls = [], waits = [];
  const result = await recoverTrace(async (i) => {
    calls.push(i);
    if (i < 2) throw unavailable();
    return { data: i === 2 ? [] : [{ revision: i }] };
  }, (record) => record.revision === 4, async (ms) => { waits.push(ms); });
  assert.deepEqual(result, { revision: 4 });
  assert.deepEqual(calls, [0, 1, 2, 3, 4]);
  assert.deepEqual(waits, [1000, 1000, 1000, 1000]);
});
test('permanent backoff consumes exactly the bounded read attempts', async () => {
  let calls = 0, waits = 0;
  await assert.rejects(recoverTrace(async () => {
    ++calls; throw unavailable();
  }, () => true, async (ms) => { assert.equal(ms, 1000); ++waits; }),
  /within recovery bound/);
  assert.equal(calls, 70); assert.equal(waits, 70);
});
test('transport, authorization and malformed responses are not retried', async () => {
  for (const status of [undefined, 400, 401, 403, 404, 429, 500]) {
    let calls = 0;
    const failure = Object.assign(new Error('original failure'), { status });
    await assert.rejects(recoverTrace(async () => { ++calls; throw failure; },
      () => true, async () => assert.fail('unexpected retry')), (e) => e === failure);
    assert.equal(calls, 1);
  }
  await assert.rejects(recoverTrace(async () => ({}), () => true,
    async () => assert.fail('malformed response retried')), TypeError);
});
