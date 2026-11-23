import test from 'node:test';
import assert from 'node:assert/strict';
import { clientHandoff } from './client_wait.mjs';
const event = {type:'session.delegation.created',delegation:{target:'client',id:'actual_id'}};
test('actual handoff identity returned', () => {
  assert.equal(clientHandoff([event],10,20),event);
});
test('audio output alone never fabricates handoff', () => {
  const output = [{type:'session.output_transcript.delta',delta:'明白。'}];
  assert.equal(clientHandoff(output,10,20),null);
  assert.throws(()=>clientHandoff(output,20,20),/absent/);
  assert.equal(output[0].delta,'明白。');
});
test('duplicate or wrong owner fails', () => {
  assert.throws(()=>clientHandoff([event,event],10,20),/ambiguous/);
  assert.throws(()=>clientHandoff([{...event,delegation:{target:'responses',id:'other'}}],10,20),/identity/);
  assert.throws(()=>clientHandoff([{...event,delegation:{target:'client',id:''}}],10,20),/identity/);
});
test('invalid clock cannot produce an unbounded wait', () => {
  assert.throws(()=>clientHandoff([],0,Infinity),/finite/);
});
