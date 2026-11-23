import assert from 'node:assert/strict';

// The public turn includes input messages and submitted tool results. Neither
// consumes an output index. An assistant preamble before a tool call does.
export async function outputPrefix(sessions, id, action) {
  const prefix = [];
  for await (const item of sessions.turns.items.list(action.turn_id, { session_id: id, order: 'asc' })) {
    if (item.role === 'user' || item.type === 'function_call_output') continue;
    assert.ok(!item.type.startsWith('tool_search'));
    prefix.push(item);
  }
  assert.deepEqual(prefix.filter(i => i.type === 'function_call').map(i => i.call_id), [action.call_id]);
  return prefix.map(i => i.id);
}

export function continuationIndices(prefix) {
  const ids = [...prefix];
  assert.equal(new Set(ids).size, ids.length);
  const indices = [];
  return { indices, observe(event) {
    if (event.type === 'agent.session.turn.item.added') {
      assert.ok(typeof event.item.id === 'string' && event.item.id.length);
      assert.ok(!ids.includes(event.item.id));
      assert.equal(event.output_index, ids.length);
      ids.push(event.item.id);
      indices.push(event.output_index);
    } else if (event.type === 'agent.session.turn.output_text.delta') {
      assert.ok(Number.isInteger(event.output_index) && event.output_index >= prefix.length &&
        event.output_index < ids.length);
      assert.equal(ids[event.output_index], event.item_id);
    }
  }};
}
