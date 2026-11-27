// Optional transport acceptance with an installed OpenAI Node SDK.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';

const lines = createInterface({ input: process.stdin })[Symbol.asyncIterator]();
const settings = JSON.parse((await lines.next()).value);
const dir = resolve(process.argv[2]);
const { default: OpenAI, APIError } = await import(pathToFileURL(resolve(dir, 'index.mjs')));
const attempts = [];
let restoring = false;
const client = new OpenAI({
  apiKey: settings.key, baseURL: settings.base, timeout: 5000, maxRetries: 1,
  fetch: async (input, init) => {
    const response = await fetch(input, init);
    attempts.push(response.status);
    if (restoring && response.status === settings.status) {
      console.log(JSON.stringify({ rejected: response.status }));
      if ((await lines.next()).value !== 'ready') throw Error('peer did not recover');
    }
    return response;
  },
});
const body = { model: 'cpu-contract', messages: [{ role: 'user', content: 'one request' }] };
const accepted = settings.accepted;
const version = JSON.parse(readFileSync(resolve(dir, 'package.json'))).version;
let error;
try {
  await client.chat.completions.create(body, { maxRetries: accepted ? 1 : 0 });
  throw Error('expected an API error');
} catch (exc) {
  if (!(exc instanceof APIError)) throw exc;
  error = {
    status: exc.status, name: exc.constructor.name, code: exc.code, type: exc.type,
    param: exc.param, trace: exc.requestID, headerTrace: exc.headers.get('x-request-id'),
    delay: exc.headers.get('retry-after'), retry: exc.headers.get('x-should-retry'),
  };
}
let reply = null;
if (!accepted) {
  restoring = true;
  const response = await client.chat.completions.create(body);
  reply = response.choices[0].message.content;
}
console.log(JSON.stringify({ version, attempts, error, reply }));
await lines.return();
