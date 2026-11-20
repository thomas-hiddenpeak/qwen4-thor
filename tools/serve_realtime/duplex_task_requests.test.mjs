import assert from 'node:assert/strict';
import test from 'node:test';
import { bargeInResponse } from './duplex_task_requests.mjs';

test('the actual barge-in request overrides an out-of-band audio base', () => {
  const base = Object.freeze({ conversation: 'none', output_modalities: ['text'],
    tools: [], tool_choice: 'none', max_output_tokens: 128 });
  const request = bargeInResponse(base);
  assert.equal(request.conversation, 'auto');
  assert.deepEqual(request.output_modalities, ['audio']);
  assert.equal(base.conversation, 'none');
  assert.equal(request.tool_choice, 'none');
  assert.equal(request.max_output_tokens, 3457);
});
