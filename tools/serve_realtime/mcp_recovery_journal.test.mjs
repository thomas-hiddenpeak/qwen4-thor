import assert from 'node:assert/strict';
import test from 'node:test';
import { publicEvent } from './mcp_recovery_journal.mjs';
const secret = 'private-token-for-cpu-fixture-only';
test('client authorization redaction preserves its original outbound value', () => {
  const event = { type: 'session.update', session: { tools: [
    { type: 'mcp', authorization: secret, server_label: 'project' }] } };
  const row = publicEvent(event, 'client', [secret]);
  assert.equal(row.session.tools[0].authorization, '<redacted>');
  assert.equal(event.session.tools[0].authorization, secret);
});
test('server credentials are rejected before journal write', () => {
  assert.throws(() => publicEvent({ type: 'session.updated', authorization: secret },
    'server', [secret]), /credential/);
});
test('credentials hidden in other client fields cannot escape redaction checks', () => {
  assert.throws(() => publicEvent({ type: 'session.update', instructions: secret },
    'client', [secret]), /credential/);
});
