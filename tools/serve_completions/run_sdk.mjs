import { execute } from './sdk.mjs';
const input = [];
let bytes = 0;
for await (const chunk of process.stdin) {
  bytes += chunk.length;
  if (bytes > 16 * 1024 * 1024) throw new Error('bridge input limit');
  input.push(chunk);
}
const result = await execute(JSON.parse(Buffer.concat(input).toString('utf8')));
process.stdout.write(JSON.stringify(result) + '\n');
if (!result.ok) process.exitCode = 1;
