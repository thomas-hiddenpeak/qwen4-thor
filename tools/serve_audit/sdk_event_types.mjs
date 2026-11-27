// Fetch the real event list, including from SDKs released before this resource.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const config = JSON.parse(readFileSync(0, 'utf8'));
const sdk = resolve(process.argv[2]);
const {version} = JSON.parse(readFileSync(resolve(sdk, 'package.json')));
const {default: OpenAI} = await import(pathToFileURL(resolve(sdk, 'index.mjs')));
const client = new OpenAI({baseURL: config.base, apiKey: config.key, maxRetries: 0, timeout: 8000});
const resource = client.webhooks?.eventTypes;
const result = resource ? await resource.list() : await client.get('/webhook_event_types');
assert.equal(result.object, 'list');
console.log(JSON.stringify({language: 'node', version, mode: resource ? 'resource' : 'generic',
                            data: result.data}));
