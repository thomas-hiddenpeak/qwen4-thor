import assert from 'node:assert/strict';
import { readFileSync, realpathSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const root = realpathSync(process.argv[2]);
const plan = JSON.parse(readFileSync(0, 'utf8'));
const metadata = JSON.parse(readFileSync(resolve(root, 'package.json')));
assert.equal(metadata.name, 'openai');
assert.equal(plan.sdk, 'node-' + metadata.version);
const { default: OpenAI, toFile } = await import(pathToFileURL(resolve(root, 'index.mjs')));
const requests = [];
const client = new OpenAI({baseURL:plan.base, apiKey:plan.key, maxRetries:0, timeout:90000,
  fetch:async (url,opts) => {
    if (url === 'data:,') return fetch(url,opts);
    const req = new Request(url,opts);
    const path = new URL(req.url);
    assert.equal(path.origin,new URL(plan.base).origin);
    const response = await fetch(req);
    requests.push({method:req.method, path:path.pathname,status:response.status,trace:response.headers.get('x-request-id')});
    return response;
  }});
const file = () => toFile(readFileSync(plan.audio),'mixed.wav',{type:'audio/wav'});
const unary = await client.audio.transcriptions.create({...plan.request,file:await file()});
const events = [];
for await (const event of await client.audio.transcriptions.create({...plan.request,stream:true,file:await file()})) {
  events.push(event);
}
console.log(JSON.stringify({sdk:plan.sdk,requests,unary,events}));
