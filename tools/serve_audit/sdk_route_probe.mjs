// Exercise the installed SDK through an in-process fetch implementation.
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {resolve} from 'node:path';
import {routeArguments} from './sdk_route_arguments.mjs';

const catalog = JSON.parse(readFileSync(new URL(
  '../../docs/evidence/serve-sdk-routes-20261126/methods.json', import.meta.url)));
const sdk = resolve(process.argv[2], 'node_modules/openai/package.json');
const {version} = JSON.parse(readFileSync(sdk));
if (!catalog.versions.node.includes(version)) throw new Error('Unknown SDK version ' + version);
const {OpenAI} = createRequire(sdk)('openai');
const seen = [];
const client = new OpenAI({apiKey: 'fixture', baseURL: 'https://sdk.invalid/v1', maxRetries: 0,
  fetch: async (input, options = {}) => {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
    if (url.hostname !== 'sdk.invalid') throw new Error('Unexpected mock destination');
    const selectors = url.searchParams.getAll('beta').map(value => 'beta=' + value);
    const route = url.pathname + (selectors.length ? '?' + selectors.join('&') : '');
    seen.push([options.method ?? input.method ?? 'GET', route]);
    return Response.json({error: {message: 'offline probe'}}, {status: 418});
  }});

for (const [verb, path, , accessor] of catalog.rows) {
  const keys = accessor.split('.');
  const resource = keys.slice(0, -1).reduce((object, key) => object[key], client);
  const method = resource[keys.at(-1)];
  const args = routeArguments(method, path);
  seen.length = 0;
  try {
    await method.apply(resource, args);
  } catch (error) {
    if (error.status !== 418) throw error;
  }
  const route = '/v1' + path.replace(/\{[^}]+\}/g, 'q4tfixture');
  if (seen.length !== 1 || seen[0][0] !== verb || seen[0][1] !== route) {
    throw new Error(accessor + ' emitted ' + JSON.stringify(seen) + ', expected ' + verb + ' ' + route);
  }
}
console.log(JSON.stringify({version, canonical: catalog.rows.length}));
