// Stage or read the same tenant's resources around an online retention sweep.
import assert from 'node:assert/strict';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const part of process.stdin) {
  input += part;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.ok(base.protocol === 'https:' && base.hostname === '127.0.0.1' &&
          base.pathname === '/v1' && !base.username && !base.password);
const {OpenAI, version} = loadSdk(plan.sdk);
assert.equal(version, '7.47.0');
const owner = new OpenAI({apiKey:plan.key, baseURL:base.href, maxRetries:0, timeout:45000});
const {environments, sessions} = owner.beta.agents;
const size = 5 * 1024 * 1024;
const path = '/workspace/large.txt';
const file = {type:'inline', path, data:Buffer.alloc(size, 0x71).toString('base64')};
let result;
if (plan.phase === 'prepare') {
  const conversation = await owner.conversations.create({
    items:[{type:'message', role:'user', content:'Project acceptance marker'}],
  });
  const items = await owner.conversations.items.list(conversation.id);
  assert.equal(items.data.length, 1);
  assert.equal((await owner.conversations.delete(conversation.id)).deleted, true);
  const template = await environments.templates.create({name:'Retained project file', files:[file]});
  const warm = await environments.create({environment:{type:'openai_hosted',
    environment_template_id:template.id}});
  const session = await sessions.create({environment:{type:'openai_hosted', environment_id:warm.id},
    agent:{model:'qwen3.8-flash-next'}});
  assert.equal(session.status,'idle');
  result = {template:template.id, environment:warm.id, session:session.id,
            orphan:items.data[0].id, source_bytes:size};
} else {
  assert.equal(plan.phase,'verify');
  const {template,environment,session} = plan.prepared;
  const retrieved = await environments.templates.retrieve(template);
  assert.equal(retrieved.files[0].size_bytes, size);
  // The original sealed template must still instantiate, and replacing an
  // installed file opens and authenticates the original sealed workspace.
  const fresh = await environments.create({environment:{type:'openai_hosted',
    environment_template_id:template}});
  assert.equal(fresh.files[0].size_bytes, size);
  assert.equal((await environments.files.list(environment)).data[0].size_bytes,size);
  const replaced = await environments.files.create(environment,{path,type:'inline',data:'cTR0'});
  assert.equal(replaced.size_bytes,3);
  assert.equal((await sessions.delete(session)).deleted,true);
  assert.equal((await environments.templates.delete(template)).deleted,true);
  result = {template_reopened:true, source_bytes:size, workspace_reopened:true, replaced_bytes:3,
            disconnected:(await environments.retrieve(environment)).status === 'disconnected'};
  assert.equal(result.disconnected,true);
}
process.stdout.write(JSON.stringify(result)+'\n');
