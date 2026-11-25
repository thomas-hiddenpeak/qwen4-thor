// Exercise the official Videos client against an authenticated native origin.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const client = apiKey => new OpenAI({baseURL: plan.base, apiKey, maxRetries: 0});
const owner = client(plan.key), peer = client(plan.other);
const origin = new URL(plan.base);
assert.equal(origin.protocol, 'https:');
const root = new URL(plan.base + '/videos/characters');
const bytes = readFileSync(plan.clip);
const clip = () => new File([bytes], 'reference.mp4', {type: 'video/mp4'});
const name = '项目视频角色';
const wire = [];
async function capture(call, method, path, url) {
  const response = await call.asResponse();
  assert.equal(response.url, url);
  assert.equal(response.status, 200);
  const value = await response.clone().json();
  assert.deepEqual(await call, value);
  assert.deepEqual(Object.keys(value).sort(), ['created_at', 'id', 'name']);
  assert.match(value.id, /^char_[a-f0-9]{32}$/);
  assert.equal(value.name, name);
  assert.ok(Number.isSafeInteger(value.created_at) && value.created_at > 0);
  wire.push({method, path, status: 200, value});
  return value;
}
const resource = plan.previous ?? await capture(
  owner.videos.createCharacter({name, video: clip()}), 'post', '/videos/characters', root.href);
const get = await capture(owner.videos.getCharacter(resource.id), 'get',
  '/videos/characters/{character_id}', root.href + '/' + resource.id);
assert.deepEqual(get, resource);
await assert.rejects(peer.videos.getCharacter(resource.id), err => err.status === 404);
await assert.rejects(owner.videos.createCharacter({name, video:
  new File(['unplayable'], 'reference.mp4', {type:'video/mp4'})}), err => err.status === 400);
await assert.rejects(owner.videos.createCharacter({name: '影'.repeat(81), video: clip()}),
  err => err.status === 400);
const chat = await peer.chat.completions.create({model: plan.model, temperature: 0,
  max_tokens: 32, reasoning_effort: 'none',
  messages: [{role:'user',content:'Return just the integer product of 6 and 7.'}]});
const independent = chat.choices[0].message.content.trim();
assert.equal(independent, '42');
process.stdout.write(JSON.stringify({sdk:version, resource, wire, independent,
  clip_sha256:createHash('sha256').update(bytes).digest('hex'), foreign:404, corrupt:400, long_name:400}));
