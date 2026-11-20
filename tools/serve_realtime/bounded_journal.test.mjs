import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { test } from 'node:test';
import { openJournal } from './bounded_journal.mjs';

const root = process.env.Q4T_TEST_OUTPUT_DIR;
assert(root && path.isAbsolute(root), 'private CPU artifact directory required');
const directory = fs.mkdtempSync(path.join(root, 'journal-'));

test('every original event is readable before close', () => {
  const file = path.join(directory, 'immediate.jsonl'), journal = openJournal(file);
  const event = { type: 'response.output_text.delta', delta: '原始回答' };
  journal.append('server', event);
  assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')).event, event);
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  journal.close();
});

test('capacity rejection retains the exact prior bytes', () => {
  const file = path.join(directory, 'bound.jsonl'), journal = openJournal(file, 200);
  journal.append('client', { type: 'response.create' });
  const original = fs.readFileSync(file);
  assert.throws(() => journal.append('server', { type: 'response.done', text: 'x'.repeat(200) }), /byte bound/);
  assert.deepEqual(fs.readFileSync(file), original);
  assert.throws(() => journal.append('unknown', { type: 'response.done' }), /invalid/);
  journal.close();
});

test('existing evidence and symlink targets cannot be overwritten', () => {
  const file = path.join(directory, 'prior.jsonl');
  fs.writeFileSync(file, 'original');
  const link = path.join(directory, 'link.jsonl'); fs.symlinkSync(file, link);
  assert.throws(() => openJournal(file)); assert.throws(() => openJournal(link));
  assert.equal(fs.readFileSync(file, 'utf8'), 'original');
});

test('SIGKILL preserves the unfinished actor event journal', async () => {
  const file = path.join(directory, 'killed.jsonl');
  const source = `import { openJournal } from ${JSON.stringify(new URL('./bounded_journal.mjs', import.meta.url).href)};
    const journal = openJournal(process.argv[1]);
    journal.append('server', { type: 'response.output_text.delta', delta: 'before kill' });
    process.stdout.write('ready'); setInterval(() => {}, 1000);`;
  const child = spawn(process.execPath, ['--input-type=module', '-e', source, file],
    { stdio: ['ignore', 'pipe', 'pipe'] });
  const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
  try {
    const [data] = await once(child.stdout, 'data');
    assert.equal(data.toString(), 'ready');
    const closed = once(child, 'close'); child.kill('SIGKILL');
    const [code, signal] = await closed;
    assert.equal(code, null); assert.equal(signal, 'SIGKILL');
    const row = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.equal(row.event.delta, 'before kill');
    assert.equal(row.event.type, 'response.output_text.delta');
  } finally { clearTimeout(timer); child.kill('SIGKILL'); }
});
