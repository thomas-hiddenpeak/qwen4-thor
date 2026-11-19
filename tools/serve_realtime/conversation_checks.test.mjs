import assert from 'node:assert/strict';
import test from 'node:test';
import { terminal, usage, functionCall, streamAgrees } from './conversation_checks.mjs';

const response = () => ({ id: 'resp_real', status: 'completed',
  output: [{ id: 'item_real', type: 'message', status: 'completed',
    content: [{ type: 'output_text', text: '42' }] }],
  usage: { input_tokens: 12, output_tokens: 2, total_tokens: 14 } });
const call = () => ({ ...response(), output: [{ id: 'item_call',
  type: 'function_call', status: 'completed', call_id: 'call_real',
  name: 'lookup_project_record', arguments: '{"record_key":"release"}' }] });

test('missing, repeated and wrong-state terminal cannot pass', () => {
  const row = response(), event = { type: 'response.done', response: row };
  assert.equal(terminal([event], row.id, 'completed'), row);
  for (const rows of [[], [event, event]])
    assert.throws(() => terminal(rows, row.id, 'completed'));
  assert.throws(() => terminal([event], row.id, 'cancelled'));
});

test('missing, fabricated and inconsistent usage cannot pass', () => {
  assert.equal(usage(response()), 14);
  for (const change of [undefined, {}, { input_tokens: 0, output_tokens: 0,
    total_tokens: 0 }, { input_tokens: 12, output_tokens: 2, total_tokens: 13 },
    { input_tokens: 12, output_tokens: 0.5, total_tokens: 12.5 }])
    assert.throws(() => usage({ ...response(), usage: change }));
});

test('only the completed allowlisted actual tool call is executable', () => {
  assert.equal(functionCall(call()).call_id, 'call_real');
  for (const change of [{ status: 'incomplete' }, { status: 'cancelled' }])
    assert.throws(() => functionCall({ ...call(), ...change }));
  for (const change of [{ status: 'incomplete' }, { name: 'run_shell' },
    { call_id: '' }, { arguments: '{"record_key":"other"}' },
    { arguments: '{"record_key":"release","command":"rm"}' }]) {
    const row = call(); Object.assign(row.output[0], change);
    assert.throws(() => functionCall(row));
  }
});

test('text terminal cannot hide missing, foreign or changed deltas', () => {
  const row = response();
  const delta = { type: 'response.output_text.delta',
    response_id: row.id, item_id: 'item_real', delta: '42' };
  streamAgrees([delta], row);
  for (const events of [[], [{ ...delta, delta: '41' }],
    [{ ...delta, response_id: 'foreign' }]])
    assert.throws(() => streamAgrees(events, row));
});

test('function terminal cannot hide partial or foreign arguments', () => {
  const row = call();
  const delta = { type: 'response.function_call_arguments.delta',
    response_id: row.id, item_id: 'item_call', delta: row.output[0].arguments };
  streamAgrees([delta], row);
  for (const events of [[], [{ ...delta, delta: '{' }],
    [{ ...delta, item_id: 'foreign' }]])
    assert.throws(() => streamAgrees(events, row));
});
