// Run against a private TLS fixture with two independent client identities.
import assert from 'node:assert/strict';
import { loadSdk } from '../serve_protocol/sdk.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.ok(base.protocol === 'https:' && base.hostname === '127.0.0.1' &&
          base.pathname === '/v1' && !base.username && !base.password);
const { OpenAI, version } = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const client = key => new OpenAI({baseURL: base.href, apiKey: key, maxRetries: 0, timeout: 35000});
const owner = client(plan.key), peer = client(plan.other);
const vaults = owner.beta.agents.vaults;
const failed = status => err => err instanceof OpenAI.APIError && err.status === status;
const capability = (await owner.get('/capabilities')).agent_vaults;
assert.equal(capability.endpoint, '/v1/vaults');
await assert.rejects(owner.get('/vaults'), failed(400));
const vault = await vaults.create({name: '　 发布资料　', metadata: {team: '开发', stage: '444'}});
assert.equal(vault.name, '发布资料');
assert.deepEqual(Object.keys(vault).sort(), ['created_at','id','metadata','name','object']);
const empty = await fetch(new URL('/v1/vaults', base), {method: 'POST', headers: {
  Authorization: 'Bearer ' + plan.key, 'OpenAI-Beta': 'agents=v1'}});
assert.equal(empty.status, 200);
const second = await empty.json();
assert.equal(second.name, null);
const ids = [];
for await (const item of vaults.list({limit: 1, order: 'asc'})) {
  ids.push(item.id);
  assert.ok(ids.length <= 2);
}
assert.deepEqual(ids.sort(), [second.id, vault.id].sort());
assert.equal((await peer.beta.agents.vaults.list()).data.length, 0);
await assert.rejects(peer.beta.agents.vaults.retrieve(vault.id), failed(404));
const update = await vaults.update(vault.id, {name: null});
assert.equal(update.name, null);
assert.deepEqual(update.metadata, vault.metadata);
const filtered = await vaults.list({metadata: vault.metadata, status: 'active'});
assert.deepEqual(filtered.data.map(v => v.id), [vault.id]);

const request = { agent: {model: 'qwen3.8-flash-next', reasoning: {effort:'none'}},
  environment: {type: 'none'}, input: 'Reply with only the number 42.', vault_ids: [vault.id] };
const sessions = owner.beta.agents.sessions;
await assert.rejects(peer.beta.agents.sessions.create(request), failed(404));
let session = await sessions.create(request);
assert.deepEqual(session.vault_ids, [vault.id]);
const end = Date.now() + 45000;
while (session.status === 'in_progress' && Date.now() < end) {
  await new Promise(resolve => setTimeout(resolve, 250));
  session = await sessions.retrieve(session.id);
}
assert.equal(session.status, 'idle');
const items = (await sessions.items.list(session.id, {order:'asc'})).data;
const answer = items.filter(item => item.role === 'assistant').flatMap(item => item.content)
                    .map(item => item.text).join('').trim();
assert.equal(answer, '42');

assert.deepEqual(await vaults.delete(vault.id), {id:vault.id, object:'vault.deleted', deleted:true});
assert.deepEqual((await vaults.list({status:['archived']})).data.map(item => item.id), [vault.id]);
assert.deepEqual((await vaults.list({status:['active','archived']})).data.map(item => item.id).sort(), ids);
await assert.rejects(vaults.update(vault.id, {name:'new'}), failed(409));
await assert.rejects(sessions.create(request), failed(404));
assert.deepEqual((await sessions.retrieve(session.id)).vault_ids, [vault.id]);
await sessions.delete(session.id);
await vaults.delete(second.id);
process.stdout.write(JSON.stringify({sdk:version, pages:2, foreign_read:404, foreign_bind:404,
  archived_update:409, archived_bind:404, history_preserved:true, answer}) + '\n');
