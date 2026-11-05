// File/configuration checks only; no HTTP listener or connection.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, symlinkSync, mkdtempSync } from 'node:fs';
import { join } from 'node:path';
import { test } from 'node:test';
import { nodeFetch, readCa } from './node_transport.mjs';

const directory = mkdtempSync(join(process.env.Q4T_CLIENT_TEST_DIR, 'node-ca-'));
const pem = readFileSync(process.env.Q4T_CLIENT_TEST_CA);
function file(name, value) {
  const path = join(directory, name); writeFileSync(path, value); return path;
}

test('explicit CA is read once and transport retains the validated snapshot', () => {
  const path = file('valid.pem', pem);
  assert.equal(readCa(path), pem.toString('ascii'));
  const fetch = nodeFetch('https://q4t.internal.test/v1', { caFile: path, connectHost: '127.0.0.1' });
  writeFileSync(path, 'changed after construction');
  assert.equal(typeof fetch, 'function');
  assert.throws(() => readCa(path), /PEM/);
});

test('CA reader rejects high-bit aliases, links, empty and oversized input', () => {
  const high = Buffer.from(pem); high[0] |= 128;
  for (const [name, bytes] of [['high.pem', high], ['empty.pem', Buffer.alloc(0)],
    ['large.pem', Buffer.alloc(65537)], ['extra.pem', Buffer.concat([pem, Buffer.from('extra')])]])
    assert.throws(() => readCa(file(name, bytes)));
  const original = file('original.pem', pem), link = join(directory, 'link.pem');
  symlinkSync(original, link); assert.throws(() => readCa(link));
});

test('transport rejects foreign origins, credentials and invalid request shapes before IO', async () => {
  const fetch = nodeFetch('https://q4t.internal.test/v1', { connectHost: '127.0.0.1' });
  for (const url of ['https://elsewhere.test/v1/models', 'http://q4t.internal.test/v1/models',
    'https://secret@q4t.internal.test/v1/models']) await assert.rejects(fetch(url));
  for (const init of [{ method: 'PUT' }, { method: 'POST', body: {} },
    { method: 'POST', body: Buffer.alloc(16 * 1024 * 1024 + 1) }])
    await assert.rejects(fetch('https://q4t.internal.test/v1/models', init));
});
