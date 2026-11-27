// Replay the installed gateway's captured errors through a stock Node SDK.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {routeArguments} from './sdk_route_arguments.mjs';

const root = resolve(process.argv[2]);
const {default: OpenAI} = await import(pathToFileURL(resolve(root, 'index.mjs')));
const {version} = JSON.parse(readFileSync(resolve(root, 'package.json')));
const raw = readFileSync(process.argv[3]);
const {packets} = JSON.parse(raw);
const catalog = JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-sdk-routes-20261126/methods.json', import.meta.url)));
const target = path => path.replace(/\{[^}]+\}/g, 'q4tfixture');
const seen = [];
const client = new OpenAI({
  apiKey: 'fixture', baseURL: 'https://sdk.invalid/v1', maxRetries: 0,
  fetch: async (input, options = {}) => {
    const uri = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
    assert.equal(uri.origin, 'https://sdk.invalid');
    const selectors = uri.searchParams.getAll('beta').map(value => 'beta=' + value);
    const key = (options.method ?? input.method ?? 'GET') + ' ' + uri.pathname +
      (selectors.length ? '?' + selectors.join('&') : '');
    const packet = packets[key];
    assert.ok(packet, key);
    seen.push(key);
    return Response.json(packet.body, {status: packet.status, headers: packet.headers});
  }
});
const rows = [];
for (const [verb, path, , accessor] of catalog.rows) {
  const names = accessor.split('.');
  const resource = names.slice(0, -1).reduce((obj, key) => obj?.[key], client);
  const method = resource?.[names.at(-1)];
  let invocation = typeof method === 'function' ? 'resource' : 'generic';
  seen.length = 0;
  await assert.rejects(async () => {
    if (invocation === 'resource') await method.apply(resource, routeArguments(method, path));
    else await client[verb.toLowerCase()](target(path));
  }, error => {
    assert.equal(seen.length, 1, accessor);
    const {headers, body} = packets[seen[0]];
    assert.deepEqual([error.status, error.constructor.name, error.type, error.code, error.param,
                      error.requestID, error.headers?.get('www-authenticate')],
                     [401, 'AuthenticationError', body.error.type, body.error.code, body.error.param,
                      headers['x-request-id'], headers['www-authenticate']]);
    return true;
  });
  if (seen[0] !== verb + ' /v1' + target(path)) {
    assert.equal(invocation, 'resource');
    assert.equal(accessor, 'evals.runs.cancel');
    assert.equal(seen[0], verb + ' /v1' + target(catalog.legacy_cancel_path));
    invocation = 'legacy_cancel';
  }
  rows.push([verb, path, invocation]);
}
console.log(JSON.stringify({language: 'node', version, rows,
                            capture_sha256: createHash('sha256').update(raw).digest('hex')}));
