import assert from 'node:assert/strict';

// Compare the session-scoped browser view with the project SDK's item IDs.
// The caller starts with five real stored items, including a completed tool call.
export async function verifyPageDefaults({thread, threads, browser, followUp}) {
  for (let i = 0; i < 8; ++i) await followUp();
  const all = await threads.listItems(thread, {limit: 21, order: 'desc'});
  const ids = all.data.map(item => item.id);
  assert.equal(new Set(ids).size, 21);
  assert.equal(ids.length, 21);

  const itemPage = params => browser('items.list', {thread_id: thread, ...params});
  const threadPage = params => browser('threads.list', params);
  const first = await itemPage({});
  assert.deepEqual(first.data.map(item => item.id), ids.slice(0, 20));
  assert.equal(first.has_more, true);
  assert.equal(first.after, ids[19]);
  const listed = await threadPage({});
  assert.deepEqual(listed.data.map(row => row.id), [thread]);
  for (const limit of [null, 0, 20]) {
    assert.deepEqual(await itemPage({limit}), first);
    assert.deepEqual(await threadPage({limit}), listed);
  }
  const next = await itemPage({after: first.after, limit: 0});
  assert.deepEqual(next.data.map(item => item.id), ids.slice(20));
  assert.equal(next.has_more, false);
  const opened = await browser('threads.get_by_id', {thread_id: thread});
  const ascending = ids.toReversed();
  assert.deepEqual(opened.items.data.map(item => item.id), ascending.slice(0, 20));
  assert.equal(opened.items.has_more, true);
  assert.equal(opened.items.after, ascending[19]);
  const tail = await itemPage({after: opened.items.after, order: 'asc', limit: 0});
  assert.deepEqual(tail.data.map(item => item.id), ascending.slice(20));
  for (const [op, params, field] of [
    ['threads.list', {order: null}, 'order'],
    ['items.list', {thread_id: 'cthr_absent', limit: 1.5}, 'limit'],
    ['items.list', {}, 'thread_id'],
  ]) {
    const failure = await browser(op, params, 400);
    assert.equal(failure.error.param, field);
  }
  for (const query of [params => threads.list(params),
                       params => threads.listItems(thread, params)]) {
    const empty = await (await query({limit: 0}).asResponse()).json();
    assert.deepEqual(empty.data, []);
    assert.equal(empty.has_more, true);
    await assert.rejects(query({before: 'absent'}),
      error => error.status === 400 && error.error.param === 'before');
  }
  return {thread, total: ids.length, first: first.data.length,
    cursor: first.after, next: next.data.map(item => item.id),
    browser_limits: [null, 0, 20], management_zero: 0,
    invalid_fields: ['order', 'limit', 'thread_id', 'before']};
}
