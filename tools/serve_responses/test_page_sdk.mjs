// Replay actual native pages through the official SDK and acceptance bridges.
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { readFileSync } from 'node:fs';
import http from 'node:http';
import test from 'node:test';
import { execute } from './sdk.mjs';
import { execute as background } from './background_sdk.mjs';

const actual = JSON.parse(readFileSync(process.env.Q4T_RESPONSES_PAGE_FIXTURE, 'utf8'));
assert.ok(actual.first_id && actual.last_id && actual.data.length);
const empty = { object: 'list', data: [], first_id: null, last_id: null, has_more: false };
for (const [bridge, run, action] of [['foreground', execute, 'input_items'],
  ['foreground', execute, 'conversation_items'], ['background', background, 'input_items']]) {
  for (const [label, wanted] of [['actual', actual], ['empty', empty]]) {
    test(`${bridge} ${action} preserves ${label} cursor metadata`, async () => {
      let requests = 0;
      const server = http.createServer((_req, response) => {
        ++requests;
        response.setHeader('content-type', 'application/json');
        const bytes = Buffer.from(JSON.stringify(wanted));
        for (let i = 0; i < bytes.length; i += 13) response.write(bytes.subarray(i, i + 13));
        response.end();
      });
      server.listen(0, '127.0.0.1');
      await once(server, 'listening');
      try {
        const result = await run({ action, id: 'page',
          base_url: `http://127.0.0.1:${server.address().port}/v1`,
          sdk_directory: process.env.Q4T_NODE_SDK_DIR, timeout_ms: 2000 });
        assert.equal(result.ok, true, JSON.stringify(result.error));
        assert.deepEqual(result.result, wanted);
        assert.equal(requests, 1);
      } finally {
        server.closeAllConnections();
        await new Promise(resolve => server.close(resolve));
      }
    });
  }
}
